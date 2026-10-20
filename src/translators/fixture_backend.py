"""Deterministic translations for smoke/benchmark tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from src.locales import LocaleSpec

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "fixtures" / "highlight_smoke_translations.json"
_cache: dict[str, list[str]] | None = None


def load_fixture_translations(path: Path | None = None) -> dict[str, list[str]]:
    global _cache
    if _cache is not None:
        return _cache
    p = path or _FIXTURE_PATH
    if not p.is_file():
        raise FileNotFoundError(f"Missing fixture translations: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    by_lang = data.get("by_lang") or data
    _cache = {str(k): list(v) for k, v in by_lang.items()}
    return _cache


def reset_fixture_cache() -> None:
    global _cache
    _cache = None


class FixtureSceneTranslator:
    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        _ = english_text
        if locale.code == "en":
            return english_text.strip()
        table = load_fixture_translations()
        lines = table.get(locale.code)
        if not lines:
            raise RuntimeError(f"No fixture lines for language {locale.code}")
        if scene_index >= len(lines):
            raise RuntimeError(
                f"Fixture for {locale.code} has {len(lines)} scenes, need index {scene_index}"
            )
        return str(lines[scene_index]).strip()
