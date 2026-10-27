"""Text that users may see: progress log lines, failure messages, and highlight status messages.

Infrastructure details (the GPU provider, workers, endpoints, pods, SSH/Jupyter transport, storage buckets) are not shown
to users. Console output and the code keep the full detail, so debugging is unchanged. The functions here only decide
what reaches the browser.
"""

from __future__ import annotations

import re

GENERIC_FAILURE = "Video processing failed. Please try again."
GENERIC_HIGHLIGHT_FAILURE = "The highlight video could not be created. Please try again."

# Words that name infrastructure. Any text that contains one of them is replaced or hidden for users.
_INFRA = re.compile(
    r"runpod|\bgpu\b|worker|endpoint|serverless|\bpods?\b|jupyter|\bssh\b|\bscp\b|\bs3\b|\baws\b|bucket|"
    r"presign|signed link|\[capacity\]|job id|\battempt \d+|\bhttps?://|\bproxy\b",
    re.IGNORECASE,
)

# Internal step names mapped to the labels the progress screen already uses.
_STEP_LABELS = {
    "downloading": "Downloading video",
    "extracting_audio": "Extracting audio",
    "transcribing": "Transcribing",
    "summarizing": "Summarizing",
    "translating": "Translating summary",
    "generating_audio": "Generating audio",
    "generating_subtitles": "Generating subtitles",
    "highlight_topics": "Preparing highlight video",
    "highlight_visuals": "Preparing highlight video",
    "highlight_assembly": "Rendering highlight video",
    "highlight_complete": "Finishing highlight video",
}

_STEP_LINE = re.compile(r"^Step: (\w+)(.*)$")


def is_internal_detail(text: str) -> bool:
    """True when the text names infrastructure and must not be shown to a user."""
    return bool(_INFRA.search(text or ""))


def public_log_line(text: str) -> str | None:
    """The version of a progress log line that a user may see, or None to hide it."""
    text = str(text or "").strip()
    if not text:
        return None
    step = _STEP_LINE.match(text)
    if step:
        label = _STEP_LABELS.get(step.group(1))
        if label is None:
            return None
        return f"Step: {label}{step.group(2)}"
    if is_internal_detail(text):
        return None
    return text


def public_error(message: str | None, default: str = GENERIC_FAILURE) -> str:
    """Failure text for users. Messages that name infrastructure are replaced by a neutral message."""
    text = str(message or "").strip()
    if not text:
        return default
    if is_internal_detail(text):
        return default
    return text
