#!/usr/bin/env python3
"""Batch NLLB translations on RunPod GPU (highlight path fallback)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.locales import get_locale  # noqa: E402
from src.translators.nllb_backend import NllbSceneTranslator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    lang = payload["language"]
    texts = payload["texts"]
    locale = get_locale(lang)
    translator = NllbSceneTranslator()
    out_texts = []
    for idx, text in enumerate(texts):
        out_texts.append(translator.translate(text, locale, scene_index=idx))
    Path(args.output).write_text(
        json.dumps({"language": lang, "texts": out_texts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
