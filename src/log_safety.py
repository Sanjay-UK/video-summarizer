"""Redaction for anything that reaches a log, the server console, a job log, or API error text.

Exception messages can carry signed URLs, query tokens, basic-auth credentials, bearer tokens, cookie values, or AWS
access-key IDs. redact() removes those values and keeps the rest, so the message stays useful. safe_exc() keeps the
exception type. safe_traceback() redacts a full traceback. Redaction never raises.

Limitations: this is pattern-based. A secret with no recognisable label, key name, or shape is not caught. Source
lines shown in a traceback are code, not redacted.
"""

from __future__ import annotations

import re
import traceback

# Key names whose values are secrets, in any case, with optional separators.
_KEYS = (
    r"access[_-]?token|id[_-]?token|refresh[_-]?token|token|signature|x-amz-signature|x-amz-credential|"
    r"x-amz-security-token|x-amz-date|api[_-]?key|apikey|secret[_-]?access[_-]?key|access[_-]?key[_-]?id|"
    r"access[_-]?key|secret|password|passwd|authorization|set-cookie|cookie|session(?:[_-]?id)?|credentials?"
)

# Query strings of signed or other URLs. The '?' may be percent-encoded as %3F (any case). The host and path stay.
_URL_QUERY = re.compile(r"(https?://[^\s'\"<>?%]*(?:%(?!3F|3f)[^\s'\"<>?%]*)*)(?:\?|%3F)[^\s'\"<>]*", re.IGNORECASE)

# Basic-auth credentials in a URL, scheme://user[:password]@host. The host and path stay.
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^\s/@'\"<>]+@", re.IGNORECASE)

# JSON pairs whose key is quoted: "token": "value", 'token': 'value', "token":123. Spacing may vary.
_JSON_QUOTED = re.compile(
    r"""(["'])(""" + _KEYS + r""")\1(\s*:\s*)(?:"[^"]*"|'[^']*'|[^\s,}\]"']+)""",
    re.IGNORECASE,
)

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*")

# Bare key/value pairs: token=value, password: value, cookie=value.
_KEY_VALUE = re.compile(r"(?i)\b(" + _KEYS + r")\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&'\"]+)")

# Percent-encoded key/value pairs such as X-Amz-Signature%3D..., any case.
_KEY_VALUE_ENC = re.compile(r"(?i)\b(" + _KEYS + r")(%3D|%3A)([^\s&'\"<>]+)")

_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

# Token-shaped runs: 32+ letters and digits (with _ + / -) that mix letters and digits. UUIDs have hyphens and short
# groups, so object keys and job IDs are not affected.
_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_+/\-])(?=[A-Za-z0-9_+/\-]*\d)(?=[A-Za-z0-9_+/\-]*[A-Za-z])[A-Za-z0-9_+/\-]{32,}(?![A-Za-z0-9_+/\-])"
)

_COOKIE_FLAG = re.compile(r"(?i)(--cookies(?:-from-browser)?)(\s+)(\S+)")


def redact(text: object) -> str:
    """Remove secret values from text. Host and path of URLs are kept; query strings are dropped entirely.

    Pattern-based: a secret with no recognisable label or shape is not caught.
    """
    try:
        s = "" if text is None else str(text)
        s = _USERINFO.sub(lambda m: f"{m.group(1)}[redacted]@", s)
        s = _URL_QUERY.sub(lambda m: f"{m.group(1)}?[redacted-query]", s)
        s = _BEARER.sub("Bearer [redacted]", s)  # before key/value, so 'Authorization: Bearer x' is fully covered
        s = _JSON_QUOTED.sub(lambda m: f'{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}"[redacted]"', s)
        s = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", s)
        s = _KEY_VALUE_ENC.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", s)
        s = _AWS_KEY_ID.sub("[redacted-key-id]", s)
        s = _LONG_TOKEN.sub("[redacted-token]", s)
        s = _COOKIE_FLAG.sub(lambda m: f"{m.group(1)} [redacted]", s)
        return s
    except Exception:  # noqa: BLE001 — redaction must not be the thing that fails
        return "[unreadable message redacted]"


def safe_exc(exc: BaseException) -> str:
    """'ExceptionType: redacted message'. Use this wherever an exception would be interpolated into text."""
    message = redact(exc)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def safe_traceback() -> str:
    """The current exception's traceback, redacted. The structure and file lines are kept."""
    return redact(traceback.format_exc())
