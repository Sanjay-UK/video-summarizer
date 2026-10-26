"""Classify YouTube download failures into stable codes with user-facing messages.

Only messages that clearly match a category are classified. Anything else becomes YOUTUBE_DOWNLOAD_FAILED, which
keeps the retry behaviour it had before. Technical text is sanitized before it is stored or logged: URLs, cookie
values, and authorization headers are removed.
"""

from __future__ import annotations

import re

from src.log_safety import redact

YOUTUBE_AUTH_REQUIRED = "YOUTUBE_AUTH_REQUIRED"
YOUTUBE_PRIVATE = "YOUTUBE_PRIVATE"
YOUTUBE_UNAVAILABLE = "YOUTUBE_UNAVAILABLE"
YOUTUBE_RATE_LIMITED = "YOUTUBE_RATE_LIMITED"
YOUTUBE_NETWORK = "YOUTUBE_NETWORK"
YOUTUBE_DOWNLOAD_FAILED = "YOUTUBE_DOWNLOAD_FAILED"

# Codes that will not succeed on a retry. The application stops immediately for these.
NON_RETRYABLE = frozenset({YOUTUBE_AUTH_REQUIRED, YOUTUBE_PRIVATE, YOUTUBE_UNAVAILABLE})

# Codes where the user must act, so an HTTP 422 (not 500) describes the job's result.
USER_ACTIONABLE = frozenset({YOUTUBE_AUTH_REQUIRED, YOUTUBE_PRIVATE, YOUTUBE_UNAVAILABLE})

USER_MESSAGES = {
    YOUTUBE_AUTH_REQUIRED: (
        "This YouTube video requires sign-in and could not be downloaded. "
        "Please upload the video file using the Upload Video tab instead."
    ),
    YOUTUBE_PRIVATE: (
        "This YouTube video is private and could not be downloaded. "
        "Please upload the video file using the Upload Video tab instead."
    ),
    YOUTUBE_UNAVAILABLE: (
        "This YouTube video is unavailable or has been removed. "
        "Check the link, or upload the video file using the Upload Video tab."
    ),
    YOUTUBE_RATE_LIMITED: (
        "YouTube is temporarily limiting downloads. Wait a few minutes and try again, "
        "or upload the video file using the Upload Video tab."
    ),
    YOUTUBE_NETWORK: (
        "The YouTube download failed because of a network problem. Check the connection and try again."
    ),
    YOUTUBE_DOWNLOAD_FAILED: (
        "The YouTube download failed. Try again later, or upload the video file using the Upload Video tab."
    ),
}

# Sign-in and anti-bot messages. A bare mention of cookies is not enough: the phrases below name the requirement.
_AUTH = re.compile(
    r"sign in to confirm|confirm your age|age[- ]restricted|requires? (?:a )?(?:sign[- ]?in|login)|"
    r"log ?in to (?:watch|view)|members[- ]only|use --cookies-from-browser|"
    r"cookies? (?:are )?(?:required|needed) for the authentication|"
    r"page needs to be reloaded|\bsabr\b|not a bot",
    re.IGNORECASE,
)
_PRIVATE = re.compile(r"private video|this video is private", re.IGNORECASE)
_UNAVAILABLE = re.compile(
    r"video (?:is )?unavailable|has been removed|this video is not available|"
    r"video has been terminated|account has been terminated|not available in your country",
    re.IGNORECASE,
)
_RATE = re.compile(r"http error 429|too many requests|rate[- ]?limit", re.IGNORECASE)
_NETWORK = re.compile(
    r"timed out|timeout|temporary failure|connection (?:reset|refused|aborted)|urlopen error|"
    r"network is unreachable|name or service not known|remote end closed",
    re.IGNORECASE,
)

_BEARER = re.compile(r"(?i)bearer\s+\S+")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)(cookie[s]?\s*[:=]\s*\S+|authorization\s*[:=]\s*\S+|bearer\s+\S+|--cookies\S*\s+\S+)"
)


def sanitize_technical(text: str, limit: int = 600) -> str:
    """Remove URLs, cookie values, and authorization headers. Keep the rest for server logs."""
    cleaned = redact(text or "")
    cleaned = _SECRET.sub("[redacted]", cleaned)
    cleaned = _URL.sub("[url]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def is_anti_bot_detail(detail: str) -> bool:
    """True for YouTube's anti-bot / SABR responses. These need the user to upload the file instead."""
    return bool(re.search(r"page needs to be reloaded|\bsabr\b|not a bot", detail or "", re.IGNORECASE))


def classify_youtube_error(message: str) -> str:
    """Return a stable code for a yt-dlp message. Unknown messages map to YOUTUBE_DOWNLOAD_FAILED."""
    text = message or ""
    if _AUTH.search(text):
        return YOUTUBE_AUTH_REQUIRED
    if _PRIVATE.search(text):
        return YOUTUBE_PRIVATE
    if _UNAVAILABLE.search(text):
        return YOUTUBE_UNAVAILABLE
    if _RATE.search(text):
        return YOUTUBE_RATE_LIMITED
    if _NETWORK.search(text):
        return YOUTUBE_NETWORK
    return YOUTUBE_DOWNLOAD_FAILED


class YouTubeDownloadError(RuntimeError):
    """A YouTube download failed. str() is the user-facing message; technical text stays in `detail`."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = sanitize_technical(detail)
        super().__init__(USER_MESSAGES.get(code, USER_MESSAGES[YOUTUBE_DOWNLOAD_FAILED]))

    @property
    def retryable(self) -> bool:
        return self.code not in NON_RETRYABLE

    @property
    def user_actionable(self) -> bool:
        return self.code in USER_ACTIONABLE


# Precedence when several player clients fail. A non-retryable error (sign-in, private, unavailable) describes the
# video itself, so it wins over any transient error from another client. Within each group the later message wins.
_PRECEDENCE = (YOUTUBE_AUTH_REQUIRED, YOUTUBE_PRIVATE, YOUTUBE_UNAVAILABLE)


def pick_error_text(messages: list[str]) -> str:
    """Choose the message that describes the failure best, across all attempted clients."""
    usable = [m for m in messages if m]
    if not usable:
        return ""
    classified = [(classify_youtube_error(m), m) for m in usable]
    for code in _PRECEDENCE:
        matches = [m for c, m in classified if c == code]
        if matches:
            return matches[-1]
    return classified[-1][1]
