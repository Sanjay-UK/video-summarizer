"""Google Translate via deep-translator (with urllib fallback and retries)."""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from config import HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC
from src.locales import LocaleSpec
from src.translators.google_circuit import get_google_circuit_breaker
from src.translators.google_guard import require_live_google

_GOOGLE_TARGETS = {"hi": "hi", "kn": "kn", "te": "te", "ta": "ta", "en": "en"}

_MAX_ATTEMPTS = 5
_INITIAL_BACKOFF_SEC = 2.0
_last_request_at: float = 0.0

SCENE_BATCH_DELIMITER = "\n\n<<<HIGHLIGHT_SCENE_BREAK>>>\n\n"


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


def _throttle_before_request() -> None:
    global _last_request_at
    now = time.time()
    gap = HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC - (now - _last_request_at)
    if gap > 0:
        time.sleep(gap)
    _last_request_at = time.time()


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
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    parts = payload[0] if payload else []
    return "".join(str(chunk[0]) for chunk in parts if chunk and chunk[0]).strip()


def _translate_via_deep_translator(text: str, target: str) -> str:
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source="en", target=target).translate(text)


def _translate_batch_via_deep_translator(texts: list[str], target: str) -> list[str]:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="en", target=target)
    if hasattr(translator, "translate_batch"):
        out = translator.translate_batch(texts)
        if isinstance(out, list) and len(out) == len(texts):
            return [str(x).strip() if x else "" for x in out]
    combined = SCENE_BATCH_DELIMITER.join(texts)
    single = translator.translate(combined)
    parts = single.split("<<<HIGHLIGHT_SCENE_BREAK>>>")
    cleaned = [p.strip() for p in parts]
    if len(cleaned) == len(texts):
        return cleaned
    raise GoogleTranslateError(
        f"batch split mismatch: expected {len(texts)} scenes, got {len(cleaned)}"
    )


def _circuit_open() -> bool:
    breaker = get_google_circuit_breaker()
    return breaker is not None and breaker.open


def _trip_on_rate_limit(exc: Exception) -> None:
    if not is_rate_limit_error(exc):
        return
    breaker = get_google_circuit_breaker()
    if breaker is not None:
        breaker.trip(str(exc))


def _request_with_retries(fn, *, label: str):
    if _circuit_open():
        breaker = get_google_circuit_breaker()
        reason = breaker.reason if breaker else "circuit open"
        raise GoogleTranslateError(f"Google skipped for job ({reason})")

    last_err: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            require_live_google()
            _throttle_before_request()
            return fn()
        except Exception as exc:
            last_err = exc
            if is_rate_limit_error(exc):
                _trip_on_rate_limit(exc)
                raise GoogleTranslateError(f"Google {label} rate limited: {exc}") from exc
            if attempt >= _MAX_ATTEMPTS:
                break
            delay = _INITIAL_BACKOFF_SEC * (2 ** (attempt - 1))
            print(
                f"[WARN] Google {label} attempt {attempt} failed ({exc}); retry in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise GoogleTranslateError(f"Google {label} failed after {_MAX_ATTEMPTS} attempts: {last_err}")


def google_translate_text(text: str, target_lang: str) -> str:
    target = _GOOGLE_TARGETS.get(target_lang, target_lang)
    use_deep = True

    def _do() -> str:
        nonlocal use_deep
        if use_deep:
            try:
                return _translate_via_deep_translator(text, target)
            except ImportError:
                use_deep = False
                print(
                    "[WARN] deep-translator not installed; using direct Google HTTP",
                    file=sys.stderr,
                )
        return _translate_via_urllib(text, target)

    result = _request_with_retries(_do, label=f"translate->{target}")
    if not result:
        raise GoogleTranslateError("empty Google Translate response")
    return result


def google_translate_batch(texts: list[str], target_lang: str) -> list[str]:
    """One batched API call (deep-translator batch or combined delimiter)."""
    if not texts:
        return []
    target = _GOOGLE_TARGETS.get(target_lang, target_lang)

    def _do() -> list[str]:
        try:
            return _translate_batch_via_deep_translator(texts, target)
        except ImportError:
            combined = SCENE_BATCH_DELIMITER.join(texts)
            single = _translate_via_urllib(combined, target)
            parts = [p.strip() for p in single.split("<<<HIGHLIGHT_SCENE_BREAK>>>")]
            if len(parts) != len(texts):
                raise GoogleTranslateError("urllib batch split mismatch")
            return parts

    return _request_with_retries(_do, label=f"batch->{target}")


def google_translator_probe() -> tuple[bool, str]:
    """Quick reachability check (not rate limit). Requires live Google enabled."""
    try:
        require_live_google()
        google_translate_text("hello", "hi")
        return True, ""
    except GoogleTranslateError as exc:
        if is_rate_limit_error(exc):
            return True, "rate_limited"
        return False, str(exc)
    except Exception as exc:
        if is_rate_limit_error(exc):
            return True, "rate_limited"
        return False, str(exc)


class GoogleSceneTranslator:
    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        _ = scene_index
        if locale.code == "en":
            return english_text.strip()
        return google_translate_text(english_text.strip(), locale.code)
