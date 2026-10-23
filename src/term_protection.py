"""Protect proper nouns / domain terms when translating highlight narration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from config import HIGHLIGHT_EXTRA_PROTECTED_TERMS
from src.locales import LocaleSpec
from src.simple_indic_translit import try_transliterate
from src.translation_quality import clean_translation_text, translation_passes_quality

_EN_STOPWORDS = frozenset(
    """
    a an and are as at be but by do for from get got gotta had has have he her him his how
    i if in is it its let lets main me my of on or our out re s she so that the their them
    then there these they this those three to too up us ve was we were what when where which
    who will with you your use via on
    """.split()
)

_DEFAULT_LOANWORDS = frozenset(
    {
        "sushi",
        "nigiri",
        "nagiri",
        "maki",
        "nori",
        "wasabi",
        "kombu",
        "umami",
        "sashimi",
        "tempura",
        "handroll",
        "python",
        "cpu",
        "api",
    }
)

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_CAP_WORD = re.compile(r"\b[A-Z][a-z]{2,}\b")
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")
_NUMERIC = re.compile(r"\d+(?:\.\d+)?")
_TOKEN_SPLIT = re.compile(r"(\d+(?:\.\d+)?|[A-Za-z][A-Za-z'-]*|.)", re.UNICODE)
# Python identifiers only — not English words like "in", "for", "is" (avoids over-matching prose).
_CODE_KEYWORDS = frozenset(
    {
        "print",
        "def",
        "class",
        "import",
        "return",
        "lambda",
        "pass",
        "break",
        "continue",
        "true",
        "false",
        "none",
    }
)
_INLINE_CODE_SEGMENT = re.compile(r"`[^`]+`")


@dataclass(frozen=True)
class TermDecision:
    source: str
    action: str  # transliterated | kept_original | kept_as_is
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "action": self.action, "output": self.output}


def _prose_outside_inline_code(text: str) -> str:
    """Drop markdown inline code so term extraction does not treat code as terms."""
    return _INLINE_CODE_SEGMENT.sub(" ", text)


def extract_protected_terms(english_text: str) -> list[str]:
    found: set[str] = set()
    prose = _prose_outside_inline_code(english_text)
    for pat in (_CAP_WORD, _ACRONYM):
        for m in pat.finditer(prose):
            word = m.group(0)
            if word.lower() in _EN_STOPWORDS:
                continue
            found.add(word)

    for m in _LATIN_WORD.finditer(prose):
        word = m.group(0)
        low = word.lower()
        if low in _DEFAULT_LOANWORDS or low in HIGHLIGHT_EXTRA_PROTECTED_TERMS:
            found.add(word if word[0].isupper() else low)

    for extra in HIGHLIGHT_EXTRA_PROTECTED_TERMS:
        if extra:
            found.add(extra)

    for term in list(found):
        if len(term) > 1 and term[0].isupper() and term[1:].islower():
            found.add(term.lower())

    return sorted(found, key=lambda t: (-len(t), t.lower()))


def split_prose_and_english_code(text: str) -> list[tuple[str, str]]:
    """Split into ('prose'|'code', segment); only markdown inline `code` is preserved verbatim."""
    parts: list[tuple[str, str]] = []
    last = 0
    for match in _INLINE_CODE_SEGMENT.finditer(text):
        if match.start() > last:
            parts.append(("prose", text[last : match.start()]))
        parts.append(("code", match.group(0)))
        last = match.end()
    if last < len(text):
        parts.append(("prose", text[last:]))
    if not parts:
        parts.append(("prose", text))
    return parts


def translate_text_preserving_english_code(
    english_text: str,
    translate_prose: Callable[[str], str],
) -> str:
    """Translate only prose; leave markdown code spans and print(...) in English."""
    pieces: list[str] = []
    for kind, segment in split_prose_and_english_code(english_text):
        if kind == "code":
            pieces.append(segment)
            continue
        if not segment.strip():
            pieces.append(segment)
            continue
        pieces.append(translate_prose(segment))
    return "".join(pieces)


def resolve_latin_term(term: str, locale: LocaleSpec) -> TermDecision:
    if locale.code == "en":
        return TermDecision(term, "kept_as_is", term)
    if _NUMERIC.fullmatch(term):
        return TermDecision(term, "kept_as_is", term)
    if term.lower() in _CODE_KEYWORDS:
        return TermDecision(term, "kept_original", term)

    out, ok = try_transliterate(term, locale.code)
    if ok:
        return TermDecision(term, "transliterated", out)
    return TermDecision(term, "kept_original", term)


def build_term_map_with_log(
    english_text: str,
    locale: LocaleSpec,
) -> tuple[dict[str, str], list[TermDecision]]:
    terms = extract_protected_terms(english_text)
    mapping: dict[str, str] = {}
    decisions: list[TermDecision] = []
    seen: set[str] = set()

    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        decision = resolve_latin_term(term, locale)
        decisions.append(decision)
        mapping[key] = decision.output
        if term != key:
            cap = term[0].upper() + term[1:] if term else term
            mapping[cap.lower()] = decision.output

    return mapping, decisions


def apply_term_map_to_translation(text: str, term_map: dict[str, str]) -> str:
    if not text or not term_map:
        return text

    if "`" not in text:
        out = text
        for latin_key in sorted(term_map.keys(), key=len, reverse=True):
            script_form = term_map[latin_key]
            if not script_form:
                continue
            out = re.compile(re.escape(latin_key), re.IGNORECASE).sub(script_form, out)
        return out

    merged: list[str] = []
    for kind, segment in split_prose_and_english_code(text):
        if kind == "code":
            merged.append(segment)
            continue
        seg_out = segment
        for latin_key in sorted(term_map.keys(), key=len, reverse=True):
            script_form = term_map[latin_key]
            if not script_form:
                continue
            seg_out = re.compile(re.escape(latin_key), re.IGNORECASE).sub(script_form, seg_out)
        merged.append(seg_out)
    return "".join(merged)


def _normalize_prose_latin(
    text: str,
    locale: LocaleSpec,
    term_map: dict[str, str],
    logged_sources: set[str],
) -> tuple[str, list[TermDecision]]:
    extra: list[TermDecision] = []
    pieces: list[str] = []

    for match in _TOKEN_SPLIT.finditer(text):
        part = match.group(0)
        if not part:
            continue
        if _NUMERIC.fullmatch(part):
            pieces.append(part)
            continue
        if _LATIN_WORD.fullmatch(part):
            key = part.lower()
            if key in _CODE_KEYWORDS:
                pieces.append(part)
                continue
            if key in term_map:
                pieces.append(term_map[key])
                continue
            decision = resolve_latin_term(part, locale)
            if decision.source.lower() not in logged_sources:
                extra.append(decision)
                logged_sources.add(decision.source.lower())
            pieces.append(decision.output)
            continue
        pieces.append(part)

    return "".join(pieces), extra


def normalize_stray_latin_with_log(
    text: str,
    locale: LocaleSpec,
    term_map: dict[str, str],
    logged_sources: set[str],
) -> tuple[str, list[TermDecision]]:
    """Replace stray Latin words; never remove tokens. Digits/versions unchanged."""
    if locale.code == "en":
        return text, []

    if "`" not in text:
        return _normalize_prose_latin(text, locale, term_map, logged_sources)

    extra: list[TermDecision] = []
    merged: list[str] = []
    for kind, segment in split_prose_and_english_code(text):
        if kind == "code":
            merged.append(segment)
            continue
        seg_out, seg_extra = _normalize_prose_latin(segment, locale, term_map, logged_sources)
        merged.append(seg_out)
        extra.extend(seg_extra)
    return "".join(merged), extra


def restore_inline_code_from_english(source_en: str, translated: str) -> str:
    """Re-insert any `...` spans from English if the model dropped or mangled them."""
    codes = _INLINE_CODE_SEGMENT.findall(source_en)
    if not codes:
        return translated
    out = translated
    for code in codes:
        if code in out:
            continue
        inner = code[1:-1]
        if inner and inner in out:
            out = out.replace(inner, code, 1)
            continue
        out = out + (" " if out and not out.endswith("\n") else "") + code
    return out


def finalize_indic_translation_with_log(
    english_text: str,
    translated: str,
    locale: LocaleSpec,
) -> tuple[str, list[dict[str, Any]]]:
    term_map, decisions = build_term_map_with_log(english_text, locale)
    logged = {d.source.lower() for d in decisions}
    text = apply_term_map_to_translation(translated, term_map)
    text, stray = normalize_stray_latin_with_log(text, locale, term_map, logged)
    text = restore_inline_code_from_english(english_text, text)
    text = clean_translation_text(text, locale)
    ok, reason = translation_passes_quality(text, locale)
    if not ok:
        raise ValueError(f"translation quality check failed: {reason}")
    all_decisions = decisions + stray
    return text.strip(), [d.to_dict() for d in all_decisions]


def finalize_indic_translation(english_text: str, translated: str, locale: LocaleSpec) -> str:
    text, _ = finalize_indic_translation_with_log(english_text, translated, locale)
    return text


def protected_terms_hint(english_text: str) -> str:
    terms = extract_protected_terms(english_text)
    return ", ".join(terms) if terms else ""


class TermProtectedTranslator:
    def __init__(
        self,
        inner: "SceneTranslator",
        *,
        term_log: "TranslationTermLog | None" = None,
        backend: str = "",
    ) -> None:
        self.inner = inner
        self.term_log = term_log
        self.backend = backend

    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        if locale.code == "en":
            return english_text.strip()

        def _translate_prose(segment: str) -> str:
            return self.inner.translate(segment, locale, scene_index=scene_index)

        raw = translate_text_preserving_english_code(english_text, _translate_prose)
        text, term_entries = finalize_indic_translation_with_log(english_text, raw, locale)
        if self.term_log is not None:
            self.term_log.record_scene(
                scene_index=scene_index,
                language=locale.code,
                backend=self.backend,
                terms=term_entries,
                from_cache=False,
            )
        return text


class SceneTranslator:
    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str: ...
