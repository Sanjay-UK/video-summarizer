"""Translation cleanup and quality gates for en, hi, kn, te, ta summary/highlight text."""

from __future__ import annotations

import json
import re
from typing import Optional

from src.locales import (
    LocaleSpec,
    SCRIPT_DEVANAGARI,
    SCRIPT_KANNADA,
    SCRIPT_TAMIL,
    SCRIPT_TELUGU,
    char_in_script_ranges,
)
_INLINE_CODE = re.compile(r"`[^`]+`")

_REPEAT_WORD = re.compile(r"(\S+)(?:\s+\1){5,}", re.UNICODE)
_JSON_BLOB = re.compile(r'\{\s*["\']?(?:translated|translation|text)["\']?\s*:', re.IGNORECASE)
_LITERAL_U = re.compile(r"\\u([0-9a-fA-F]{4})")
_MANGLED_U = re.compile(r"\\+[uU]?([0-9a-fA-F]{3,4})")
_LEFTover_ESCAPES = re.compile(r"\\u[0-9a-fA-F]{4}|\\[0-9]{3,4}")

SUPPORTED_TRANSLATION_LOCALES = frozenset({"en", "hi", "kn", "te", "ta"})

_FOREIGN_SCRIPTS: dict[str, tuple[tuple[int, int], ...]] = {
    "kn": (SCRIPT_TELUGU, SCRIPT_DEVANAGARI, SCRIPT_TAMIL),
    "te": (SCRIPT_KANNADA, SCRIPT_DEVANAGARI, SCRIPT_TAMIL),
    "hi": (SCRIPT_KANNADA, SCRIPT_TELUGU, SCRIPT_TAMIL),
    "ta": (SCRIPT_KANNADA, SCRIPT_TELUGU, SCRIPT_DEVANAGARI),
}


def decode_literal_unicode_escapes(text: str) -> str:
    if not text or "\\" not in text:
        return text

    def _u(m: re.Match[str]) -> str:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)

    out = _LITERAL_U.sub(_u, text)
    # Ollama sometimes emits \092e-style garbage instead of \u092e
    out = _MANGLED_U.sub(_u, out)
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    return out


def _word_repeat_violation(text: str, min_run: int = 6, max_word_total: int = 40) -> bool:
    if _REPEAT_WORD.search(text):
        return True
    from collections import Counter

    words = re.findall(r"\S+", text)
    if not words:
        return False
    most, count = Counter(words).most_common(1)[0]
    if count >= max_word_total and len(most) <= 12:
        return True
    return False


def collapse_runaway_word_repeats(text: str, min_run: int = 6) -> str:
    if not text:
        return text
    pattern = re.compile(rf"(\S+)(?:\s+\1){{{min_run - 1},}}", re.UNICODE)
    out = text
    while True:
        nxt = pattern.sub(r"\1 \1", out)
        if nxt == out:
            break
        out = nxt
    return out


def _prose_only(text: str) -> str:
    return _INLINE_CODE.sub(" ", text)


def foreign_indic_letter_count(text: str, locale: LocaleSpec) -> int:
    foreign = _FOREIGN_SCRIPTS.get(locale.code)
    if not foreign:
        return 0
    prose = _prose_only(text)
    count = 0
    for ch in prose:
        if not ch.isalpha():
            continue
        if char_in_script_ranges(ch, locale.script_ranges):
            continue
        if any(char_in_script_ranges(ch, (rng,)) for rng in foreign):
            count += 1
    return count


def target_script_letter_count(text: str, locale: LocaleSpec) -> int:
    prose = _prose_only(text)
    return sum(
        1
        for ch in prose
        if ch.isalpha() and char_in_script_ranges(ch, locale.script_ranges)
    )


def looks_like_json_translation_artifact(text: str) -> bool:
    if not text:
        return False
    if _JSON_BLOB.search(text):
        return True
    if '{"' in text:
        return True
    if text.count('":') >= 2 and "{" in text:
        return True
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return True
        except json.JSONDecodeError:
            pass
    return False


def strip_embedded_json_blobs(text: str) -> str:
    """Remove Ollama-style JSON wrappers leaked into prose."""
    if "{" not in text:
        return text
    out = text
    # Drop standalone JSON objects (best-effort brace matching on short spans)
    start = 0
    cleaned: list[str] = []
    while start < len(out):
        idx = out.find("{", start)
        if idx < 0:
            cleaned.append(out[start:])
            break
        cleaned.append(out[start:idx])
        depth = 0
        end = idx
        for pos in range(idx, min(len(out), idx + 8000)):
            if out[pos] == "{":
                depth += 1
            elif out[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        else:
            cleaned.append(out[idx:])
            break
        blob = out[idx:end]
        drop_blob = looks_like_json_translation_artifact(blob)
        if not drop_blob:
            try:
                json.loads(blob)
                drop_blob = True
            except json.JSONDecodeError:
                drop_blob = False
        if drop_blob:
            try:
                data = json.loads(blob)
                if isinstance(data, dict):
                    for key in ("translated", "translation", "text"):
                        if key in data and isinstance(data[key], str):
                            cleaned.append(data[key])
                            break
                    else:
                        for val in data.values():
                            if isinstance(val, str) and val.strip():
                                cleaned.append(val)
                                break
            except json.JSONDecodeError:
                pass
        else:
            cleaned.append(blob)
        start = end
    return "".join(cleaned).strip()


def clean_translation_text(text: str, locale: LocaleSpec) -> str:
    """Normalize model output for any supported locale before display or storage."""
    out = decode_literal_unicode_escapes(text.strip())
    out = strip_embedded_json_blobs(out)
    out = collapse_runaway_word_repeats(out)
    return out.strip()


def clean_indic_translation_text(text: str, locale: LocaleSpec) -> str:
    """Backward-compatible alias."""
    return clean_translation_text(text, locale)


def _english_indic_leak_count(text: str) -> int:
    prose = _prose_only(text)
    foreign = (SCRIPT_DEVANAGARI, SCRIPT_KANNADA, SCRIPT_TELUGU, SCRIPT_TAMIL)
    return sum(
        1
        for ch in prose
        if ch.isalpha() and any(char_in_script_ranges(ch, (rng,)) for rng in foreign)
    )


def translation_passes_quality(
    text: str,
    locale: LocaleSpec,
    *,
    min_target_letters: int = 8,
    max_foreign_indic_letters: int = 2,
) -> tuple[bool, str]:
    if locale.code not in SUPPORTED_TRANSLATION_LOCALES:
        return False, f"unsupported locale {locale.code!r}"

    cleaned = clean_translation_text(text, locale)
    if not cleaned:
        return False, "empty after cleanup"

    if looks_like_json_translation_artifact(cleaned):
        return False, "JSON translation artifact"

    if _LEFTover_ESCAPES.search(cleaned):
        return False, "unresolved escape sequences"

    if _word_repeat_violation(cleaned):
        return False, "runaway word repetition"

    if locale.code == "en":
        if _english_indic_leak_count(cleaned) > max_foreign_indic_letters:
            return False, "unexpected Indic script in English text"
        return True, ""

    target = target_script_letter_count(cleaned, locale)
    if target < min_target_letters:
        return False, "too few target-script letters"

    foreign = foreign_indic_letter_count(cleaned, locale)
    if foreign > max_foreign_indic_letters:
        return False, f"foreign Indic script letters ({foreign})"

    return True, ""


def finalize_cleaned_translation(raw: str, locale: LocaleSpec) -> Optional[str]:
    """Return cleaned text or None if quality checks fail."""
    cleaned = clean_translation_text(raw, locale)
    ok, _reason = translation_passes_quality(cleaned, locale)
    return cleaned if ok else None
