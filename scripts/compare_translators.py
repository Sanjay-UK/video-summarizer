#!/usr/bin/env python3
"""Compare highlight translation backends on 3 scenes → translations_compare.md."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import HIGHLIGHT_TRANSLATION_MODEL, HIGHLIGHT_TRANSLATOR
from src.locales import LOCALES, get_locale
from src.narration_rewrite import rewrite_for_speech
from src.term_protection import extract_protected_terms, protected_terms_hint
from src.topic_segmentation import run_topic_segmentation
from src.translators.factory import available_translator_backends, get_highlight_translator
from src.translators.nllb_backend import NllbNotAvailableError
from src.translation import translate_scene_with_retry

TARGET_LANGS = ("hi", "kn", "te", "ta")
BACKENDS = ("google", "nllb", "ollama")
NUM_SCENES = 3
OUTPUT_MD = PROJECT_ROOT / "translations_compare.md"


def load_english_scenes() -> list[str]:
    segments_path = PROJECT_ROOT / "output" / "text_only_regression" / "segments.json"
    if not segments_path.is_file():
        segments_path = PROJECT_ROOT / "transcripts" / "segments.json"
    if not segments_path.is_file():
        raise SystemExit(f"Missing segments.json (expected {segments_path})")

    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    scenes = run_topic_segmentation(segments)[:NUM_SCENES]
    texts: list[str] = []
    for scene in scenes:
        spoken = rewrite_for_speech(
            scene.summary_text,
            scene.topic_title,
            compression="high",
        )
        texts.append(spoken)
    return texts


def md_escape(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def main() -> int:
    english_scenes = load_english_scenes()
    avail = available_translator_backends()

    rows: list[tuple[int, str, str, str, str, str]] = []
    google_disabled = False

    for scene_idx, en_text in enumerate(english_scenes):
        terms = protected_terms_hint(en_text)
        for lang in TARGET_LANGS:
            locale = get_locale(lang)
            for backend in BACKENDS:
                if backend == "google" and google_disabled:
                    rows.append(
                        (
                            scene_idx,
                            lang,
                            backend,
                            en_text,
                            "(skipped — Google rate-limited; install deep-translator and re-run locally)",
                            terms,
                        )
                    )
                    continue

                if not avail.get(backend, False) and backend == "nllb":
                    rows.append(
                        (
                            scene_idx,
                            lang,
                            backend,
                            en_text,
                            "(skipped — NLLB not installed)",
                            "nllb not available",
                        )
                    )
                    continue

                try:
                    translator = get_highlight_translator(backend)
                except NllbNotAvailableError as exc:
                    rows.append((scene_idx, lang, backend, en_text, f"(skipped — {exc})", "nllb not available"))
                    continue

                res = translate_scene_with_retry(
                    en_text,
                    locale,
                    scene_index=scene_idx,
                    translator=translator,
                    backend_name=backend,
                )
                text = res.translated if res.ok else f"(FAILED: {res.warning})"
                rows.append((scene_idx, lang, backend, en_text, text, terms))
                if backend == "google":
                    if not res.ok and "429" in res.warning:
                        google_disabled = True
                    time.sleep(3.0)

    lines = [
        "# Highlight translation backend comparison",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Default config backend: `{HIGHLIGHT_TRANSLATOR}`",
        f"Ollama model (when backend=ollama): `{HIGHLIGHT_TRANSLATION_MODEL}`",
        f"Scenes: {NUM_SCENES} (English narration word-capped with compression=high)",
        "",
        "> **Note:** Re-run `python3 scripts/compare_translators.py` on your machine after "
        "`pip install deep-translator indic-transliteration` for full Google rows if any were skipped.",
        "",
        "## Protected terms per scene (English)",
        "",
    ]
    for i, en in enumerate(english_scenes):
        lines.append(f"- **Scene {i}:** {', '.join(extract_protected_terms(en)) or '(none)'}")

    lines.extend(
        [
            "",
            "## Side-by-side",
            "",
            "| Scene | Lang | Backend | English (source) | Translation | Protected terms |",
            "|------:|------|---------|------------------|-------------|-----------------|",
        ]
    )

    for scene_idx, lang, backend, en_text, translation, terms in rows:
        lines.append(
            f"| {scene_idx} | {lang} | {backend} | {md_escape(en_text[:120])}{'…' if len(en_text) > 120 else ''} "
            f"| {md_escape(translation)} | {md_escape(terms)} |"
        )

    lines.extend(
        [
            "",
            "## Windows install (per backend)",
            "",
            "### Google (`HIGHLIGHT_TRANSLATOR=google`) — recommended default",
            "```cmd",
            "pip install deep-translator indic-transliteration",
            "set HIGHLIGHT_TRANSLATOR=google",
            "```",
            "- Uses **deep-translator** (Google Translate). Retries on rate limits / network errors.",
            "- If `deep-translator` is missing, the app falls back to direct HTTP with the same retry policy.",
            "- Requires internet; no local model download.",
            "",
            "### Ollama (`HIGHLIGHT_TRANSLATOR=ollama`)",
            "```cmd",
            "pip install ollama indic-transliteration",
            "ollama pull llama3.2:3b",
            "set HIGHLIGHT_TRANSLATOR=ollama",
            "set HIGHLIGHT_TRANSLATION_MODEL=llama3.2:3b",
            "```",
            "- Optional: set `HIGHLIGHT_TRANSLATION_MODEL` to a larger model for quality experiments.",
            "",
            "### NLLB (`HIGHLIGHT_TRANSLATOR=nllb`) — optional offline",
            "```cmd",
            "pip install torch transformers sentencepiece accelerate indic-transliteration",
            "set HIGHLIGHT_TRANSLATOR=nllb",
            "```",
            "- First run downloads **facebook/nllb-200-distilled-600M** (~2.4 GB) via Hugging Face.",
            "- CPU inference is supported; GPU used automatically when CUDA is available.",
            "- **CTranslate2** conversion was skipped — extra conversion steps on Windows are brittle; "
            "transformers-only path is documented instead.",
            "",
            "### Term protection (all backends)",
            "```cmd",
            "pip install indic-transliteration",
            "```",
            "- Proper nouns / loanwords are transliterated to the target script after translation.",
            "- Optional: `set HIGHLIGHT_EXTRA_PROTECTED_TERMS=term1,term2`",
            "",
        ]
    )

    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_MD} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
