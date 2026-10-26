"""Google Translate via deep-translator (with urllib fallback and retries)."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, TypeVar

from src.log_safety import redact, safe_exc

from config import HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC
from src.locales import LocaleSpec
from src.translators.google_circuit import get_google_circuit_breaker
from src.translators.google_guard import require_live_google

_GOOGLE_TARGETS = {"hi": "hi", "kn": "kn", "te": "te", "ta": "ta", "en": "en"}

# deep-translator validates len(text) < 5000
GOOGLE_TRANSLATE_MAX_CHARS = 4990

_MAX_ATTEMPTS = 5
_INITIAL_BACKOFF_SEC = 2.0
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BACKOFF_SEC = 5.0

# Process-wide: summary + highlight + batch items share one Google call pace.
_google_request_lock = threading.Lock()
_last_request_at: float = 0.0

SCENE_BATCH_DELIMITER = "\n\n<<<HIGHLIGHT_SCENE_BREAK>>>\n\n"

T = TypeVar("T")


class GoogleTranslateError(RuntimeError):
    """Google translation failed after retries."""


def is_rate_limit_error(exc: BaseException | str) -> bool:
    msg = str(exc).lower()
    return bool(
        re.search(
            r"429|too many requests|rate limit|requests per second|try again later",
            msg,
        )
    )


def _min_interval_sec() -> float:
    return max(HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC, 1.0)


def _throttle_before_request() -> None:
    """Enforce at least one second between Google Translate HTTP calls (process-wide)."""
    global _last_request_at
    interval = _min_interval_sec()
    with _google_request_lock:
        now = time.time()
        gap = interval - (now - _last_request_at)
        if gap > 0:
            time.sleep(gap)
        _last_request_at = time.time()


def split_text_for_google(text: str, max_chars: int = GOOGLE_TRANSLATE_MAX_CHARS) -> list[str]:
    """Split text so each piece is within deep-translator / web Translate size limits."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    for block in re.split(r"\n\n+", text):
        piece = block.strip()
        if not piece:
            continue
        if len(piece) <= max_chars:
            if chunks and len(chunks[-1]) + 2 + len(piece) <= max_chars:
                chunks[-1] = f"{chunks[-1]}\n\n{piece}"
            else:
                chunks.append(piece)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", piece)
        buf = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate = f"{buf} {sentence}".strip() if buf else sentence
            if len(candidate) <= max_chars:
                buf = candidate
            else:
                if buf:
                    chunks.append(buf)
                if len(sentence) <= max_chars:
                    buf = sentence
                else:
                    start = 0
                    while start < len(sentence):
                        chunks.append(sentence[start : start + max_chars])
                        start += max_chars
                    buf = ""
        if buf:
            chunks.append(buf)

    return chunks if chunks else [text[:max_chars]]


def _translate_via_urllib(text: str, target: str) -> str:
    q = urllib.parse.quote(text)
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=en&tl={target}&dt=t&q={q}"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "video-summarizer/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise GoogleTranslateError(f"HTTP 429 from Google Translate: {redact(str(exc))}") from exc
        raise
    parts = payload[0] if payload else []
    return "".join(str(chunk[0]) for chunk in parts if chunk and chunk[0]).strip()


def _translate_via_deep_translator(text: str, target: str) -> str:
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source="en", target=target).translate(text)


def _one_http_translate(text: str, target: str, *, prefer_deep: bool) -> str:
    """Single Google HTTP request (no throttle — use _call_google_ratelimited)."""
    if prefer_deep:
        try:
            return _translate_via_deep_translator(text, target)
        except ImportError:
            prefer_deep = False
            print(
                "[WARN] deep-translator not installed; using direct Google HTTP",
                file=sys.stderr,
            )
    return _translate_via_urllib(text, target)


def _circuit_open() -> bool:
    breaker = get_google_circuit_breaker()
    return breaker is not None and breaker.open


def _trip_on_rate_limit(exc: Exception) -> None:
    if not is_rate_limit_error(exc):
        return
    breaker = get_google_circuit_breaker()
    if breaker is not None:
        breaker.trip(redact(str(exc)))


def _call_google_ratelimited(
    fn: Callable[[], T],
    *,
    label: str,
    throttle: bool = True,
) -> T:
    """
    Wait for central throttle, then call Google. Retries transient errors and 429 with backoff
    before surfacing failure to the translation chain (NLLB unchanged).
    """
    if _circuit_open():
        breaker = get_google_circuit_breaker()
        reason = breaker.reason if breaker else "circuit open"
        raise GoogleTranslateError(f"Google skipped for job ({reason})")

    require_live_google()
    last_err: Optional[Exception] = None
    rate_limit_streak = 0

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if throttle:
            _throttle_before_request()
        try:
            return fn()
        except Exception as exc:
            last_err = exc
            if is_rate_limit_error(exc):
                rate_limit_streak += 1
                if rate_limit_streak <= _RATE_LIMIT_RETRIES:
                    delay = _RATE_LIMIT_BACKOFF_SEC * rate_limit_streak
                    print(
                        f"[WARN] Google {label} rate limited (attempt {rate_limit_streak}); "
                        f"retry in {delay:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                _trip_on_rate_limit(exc)
                raise GoogleTranslateError(f"Google {label} rate limited: {redact(str(exc))}") from exc

            rate_limit_streak = 0
            if attempt >= _MAX_ATTEMPTS:
                break
            delay = _INITIAL_BACKOFF_SEC * (2 ** (attempt - 1))
            print(
                f"[WARN] Google {label} attempt {attempt} failed ({safe_exc(exc)}); retry in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise GoogleTranslateError(f"Google {label} failed after {_MAX_ATTEMPTS} attempts: {redact(str(last_err))}")


def _translate_pieces_joined(
    text: str,
    target: str,
    *,
    label: str,
    prefer_deep: bool,
    throttle: bool = True,
) -> str:
    pieces = split_text_for_google(text)
    if not pieces:
        return ""
    translated: list[str] = []
    for i, piece in enumerate(pieces):
        part_label = f"{label}[{i + 1}/{len(pieces)}]" if len(pieces) > 1 else label

        def _do(p: str = piece) -> str:
            return _one_http_translate(p, target, prefer_deep=prefer_deep)

        translated.append(_call_google_ratelimited(_do, label=part_label, throttle=throttle))
    return " ".join(t.strip() for t in translated if t.strip())


def google_translate_text(text: str, target_lang: str, *, throttle: bool = True) -> str:
    target = _GOOGLE_TARGETS.get(target_lang, target_lang)
    prefer_deep = True
    result = _translate_pieces_joined(
        text.strip(),
        target,
        label=f"translate->{target}",
        prefer_deep=prefer_deep,
        throttle=throttle,
    )
    if not result:
        raise GoogleTranslateError("empty Google Translate response")
    return result


def google_translate_batch(texts: list[str], target_lang: str, *, throttle: bool = True) -> list[str]:
    """
    Translate each string with one throttled HTTP call per safe-sized piece.
    Does not use deep-translator translate_batch (no unthrottled inner loop).
    """
    if not texts:
        return []
    target = _GOOGLE_TARGETS.get(target_lang, target_lang)
    prefer_deep = True

    combined = SCENE_BATCH_DELIMITER.join(t.strip() for t in texts if t and str(t).strip())
    if (
        len(texts) > 1
        and combined
        and len(combined) <= GOOGLE_TRANSLATE_MAX_CHARS
        and SCENE_BATCH_DELIMITER in combined
    ):
        try:

            def _do_combined() -> list[str]:
                out = _one_http_translate(combined, target, prefer_deep=prefer_deep)
                parts = [p.strip() for p in out.split("<<<HIGHLIGHT_SCENE_BREAK>>>")]
                if len(parts) == len(texts):
                    return parts
                raise GoogleTranslateError(
                    f"batch split mismatch: expected {len(texts)} scenes, got {len(parts)}"
                )

            return _call_google_ratelimited(_do_combined, label=f"batch->{target}", throttle=throttle)
        except (GoogleTranslateError, Exception):
            print(
                f"[WARN] Combined batch translate failed for {target}; per-item fallback",
                file=sys.stderr,
            )

    results: list[str] = []
    for idx, raw in enumerate(texts):
        item = str(raw).strip() if raw else ""
        if not item:
            results.append("")
            continue
        results.append(
            _translate_pieces_joined(
                item,
                target,
                label=f"batch->{target}[{idx + 1}/{len(texts)}]",
                prefer_deep=prefer_deep,
                throttle=throttle,
            )
        )
    return results


def google_translator_probe() -> tuple[bool, str]:
    """Quick reachability check (not rate limit). Requires live Google enabled."""
    try:
        require_live_google()
        google_translate_text("hello", "hi")
        return True, ""
    except GoogleTranslateError as exc:
        if is_rate_limit_error(exc):
            return True, "rate_limited"
        return False, redact(str(exc))
    except Exception as exc:
        if is_rate_limit_error(exc):
            return True, "rate_limited"
        return False, redact(str(exc))


class GoogleSceneTranslator:
    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        _ = scene_index
        if locale.code == "en":
            return english_text.strip()
        return google_translate_text(english_text.strip(), locale.code)
