"""Highlight video locale registry — one row per supported language."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from config import HIGHLIGHT_TTS_VOICE, PROJECT_DIR

FONTS_DIR = PROJECT_DIR / "static" / "fonts"

# Unicode script ranges (inclusive) for translation sanity checks
SCRIPT_DEVANAGARI = (0x0900, 0x097F)
SCRIPT_KANNADA = (0x0C80, 0x0CFF)
SCRIPT_TELUGU = (0x0C00, 0x0C7F)
SCRIPT_TAMIL = (0x0B80, 0x0BFF)
SCRIPT_LATIN = (0x0041, 0x024F)


@dataclass(frozen=True)
class LocaleSpec:
    code: str
    name: str
    edge_voice_primary: str
    edge_voice_fallback: str
    caption_font_name: str
    caption_font_file: Path
    script_ranges: Tuple[Tuple[int, int], ...]
    gtts_lang: str

    def font_path(self) -> Path:
        return self.caption_font_file

    def voice_chain(self) -> Tuple[str, ...]:
        return (self.edge_voice_primary, self.edge_voice_fallback)


LOCALES: dict[str, LocaleSpec] = {
    "en": LocaleSpec(
        code="en",
        name="English",
        edge_voice_primary=HIGHLIGHT_TTS_VOICE,
        edge_voice_fallback="en-IN-NeerjaNeural",
        caption_font_name="DejaVu Sans",
        caption_font_file=FONTS_DIR / "DejaVuSans.ttf",
        script_ranges=(SCRIPT_LATIN,),
        gtts_lang="en",
    ),
    "hi": LocaleSpec(
        code="hi",
        name="Hindi",
        edge_voice_primary="hi-IN-SwaraNeural",
        edge_voice_fallback="hi-IN-MadhurNeural",
        caption_font_name="Noto Sans Devanagari",
        caption_font_file=FONTS_DIR / "NotoSansDevanagari-Regular.ttf",
        script_ranges=(SCRIPT_DEVANAGARI,),
        gtts_lang="hi",
    ),
    "kn": LocaleSpec(
        code="kn",
        name="Kannada",
        edge_voice_primary="kn-IN-SapnaNeural",
        edge_voice_fallback="kn-IN-GaganNeural",
        caption_font_name="Noto Sans Kannada",
        caption_font_file=FONTS_DIR / "NotoSansKannada-Regular.ttf",
        script_ranges=(SCRIPT_KANNADA,),
        gtts_lang="kn",
    ),
    "te": LocaleSpec(
        code="te",
        name="Telugu",
        edge_voice_primary="te-IN-ShrutiNeural",
        edge_voice_fallback="te-IN-MohanNeural",
        caption_font_name="Noto Sans Telugu",
        caption_font_file=FONTS_DIR / "NotoSansTelugu-Regular.ttf",
        script_ranges=(SCRIPT_TELUGU,),
        gtts_lang="te",
    ),
    "ta": LocaleSpec(
        code="ta",
        name="Tamil",
        edge_voice_primary="ta-IN-PallaviNeural",
        edge_voice_fallback="ta-IN-ValluvarNeural",
        caption_font_name="Noto Sans Tamil",
        caption_font_file=FONTS_DIR / "NotoSansTamil-Regular.ttf",
        script_ranges=(SCRIPT_TAMIL,),
        gtts_lang="ta",
    ),
}

DEFAULT_HIGHLIGHT_LANGUAGES = ("en",)


def get_locale(code: str) -> LocaleSpec:
    key = code.strip().lower()
    if key not in LOCALES:
        raise ValueError(f"Unsupported highlight language: {code!r}")
    return LOCALES[key]


def parse_language_list(raw: Optional[str | Sequence[str]]) -> list[str]:
    if raw is None:
        return list(DEFAULT_HIGHLIGHT_LANGUAGES)
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    if not parts:
        return list(DEFAULT_HIGHLIGHT_LANGUAGES)
    out: list[str] = []
    for code in parts:
        get_locale(code)
        if code not in out:
            out.append(code)
    return out


def char_in_script_ranges(ch: str, ranges: Sequence[Tuple[int, int]]) -> bool:
    cp = ord(ch)
    return any(start <= cp <= end for start, end in ranges)


def text_has_target_script(text: str, locale: LocaleSpec) -> bool:
    """True if text contains at least one letter in the locale script."""
    stripped = text.strip()
    if not stripped:
        return False
    if locale.code == "en":
        return bool(re.search(r"[A-Za-z]", stripped))
    for ch in stripped:
        if ch.isalpha() and char_in_script_ranges(ch, locale.script_ranges):
            return True
    return False
