"""Job-level helpers for multilingual comprehensive summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.locales import get_locale
from src.summary_translate import list_summary_translation_status, read_summary_translations
from src.translation_quality import SUPPORTED_TRANSLATION_LOCALES, clean_translation_text


def summary_translations_for_api(job_dir: Path) -> dict[str, Any]:
    data = read_summary_translations(job_dir)
    langs = data.get("languages") or {}
    translated: dict[str, str] = {}
    for code, entry in langs.items():
        if entry.get("status") == "success" and entry.get("text"):
            if code not in SUPPORTED_TRANSLATION_LOCALES:
                continue
            locale = get_locale(code)
            translated[code] = clean_translation_text(str(entry["text"]), locale)
    return {
        "summary_translation_status": list_summary_translation_status(job_dir),
        "summary_translations": translated,
    }


def clean_summary_text_for_display(text: str, lang_code: str = "en") -> str:
    code = lang_code if lang_code in SUPPORTED_TRANSLATION_LOCALES else "en"
    return clean_translation_text(text, get_locale(code))
