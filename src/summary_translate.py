"""Translate comprehensive summary (markdown) for selected output languages."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from src.highlight_translate import translate_language_scenes
from src.locales import LOCALES, get_locale
from src.translation_cache import JobTranslationCache
from src.translation_log import TranslationTermLog
from src.translators.google_circuit import (
    GoogleJobCircuitBreaker,
    reset_google_circuit_breaker,
    set_google_circuit_breaker,
)


def _split_by_paragraph_budget(text: str, max_chars: int) -> list[str]:
    """Split long text on paragraph boundaries up to max_chars per piece."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        piece = block.strip()
        if not piece:
            continue
        candidate = f"{buf}\n\n{piece}".strip() if buf else piece
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(piece) <= max_chars:
            buf = piece
        else:
            start = 0
            while start < len(piece):
                chunks.append(piece[start : start + max_chars])
                start += max_chars
    if buf:
        chunks.append(buf)
    return chunks


def _split_summary_chunks(text: str, max_chars: int = 1200) -> list[str]:
    """
    One translation unit per markdown section (### ...) when possible so each concept
    block gets a full decode budget; fall back to paragraph chunks for oversized sections.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars and "###" not in text:
        return [text]

    sections = re.split(r"(?=^###\s)", text, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]
    if len(sections) <= 1:
        return _split_by_paragraph_budget(text, max_chars)

    chunks: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            chunks.extend(_split_by_paragraph_budget(section, max_chars))
    return chunks


def summary_translations_path(job_dir: Path) -> Path:
    return Path(job_dir) / "summary_translations.json"


def read_summary_translations(job_dir: Path) -> dict[str, Any]:
    path = summary_translations_path(job_dir)
    if not path.is_file():
        return {"requested_languages": [], "languages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"requested_languages": [], "languages": {}}


def write_summary_translations(job_dir: Path, payload: dict[str, Any]) -> None:
    path = summary_translations_path(job_dir)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def list_summary_translation_status(job_dir: Path) -> list[dict[str, Any]]:
    data = read_summary_translations(job_dir)
    requested = data.get("requested_languages") or []
    langs = data.get("languages") or {}
    out: list[dict[str, Any]] = []
    for code in requested:
        spec = LOCALES.get(code)
        entry = langs.get(code, {})
        out.append(
            {
                "code": code,
                "name": spec.name if spec else code,
                "status": entry.get("status", "unknown"),
                "message": entry.get("message", ""),
                "translator": entry.get("translator", ""),
                "available": entry.get("status") == "success" and bool(entry.get("text")),
            }
        )
    return out


def summary_text_for_language(job_dir: Path, lang_code: str) -> Optional[str]:
    if lang_code == "en":
        path = Path(job_dir) / "summary.txt"
        return path.read_text(encoding="utf-8") if path.is_file() else None
    data = read_summary_translations(job_dir)
    entry = data.get("languages", {}).get(lang_code, {})
    if entry.get("status") == "success":
        return str(entry.get("text", ""))
    return None


def translate_summary_for_languages(
    job_output_dir: Path,
    english_summary: str,
    language_codes: list[str],
) -> dict[str, Any]:
    """Translate summary.txt into each non-English language code."""
    job_output_dir = Path(job_output_dir)
    targets = [c for c in language_codes if c != "en"]
    payload: dict[str, Any] = {
        "requested_languages": targets,
        "languages": {},
    }
    if not targets or not english_summary.strip():
        write_summary_translations(job_output_dir, payload)
        return payload

    shared_dir = job_output_dir / "highlights" / "_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    cache = JobTranslationCache(shared_dir / "translation_cache.json")
    term_log = TranslationTermLog(job_output_dir / "highlights" / "translation_log.json")
    chunks = _split_summary_chunks(english_summary)
    breaker = GoogleJobCircuitBreaker()
    token = set_google_circuit_breaker(breaker)

    try:
        for lang in targets:
            get_locale(lang)
            print(f"[INFO] Translating comprehensive summary -> {lang}", file=sys.stderr)
            outcome = translate_language_scenes(
                lang,
                chunks,
                cache=cache,
                term_log=term_log,
                google_circuit=breaker,
                # Comprehensive summary: never use Ollama (JSON/repetition failures on long markdown).
                backend_chain=("google", "nllb"),
            )
            if outcome.ok and outcome.spoken_texts:
                if len(outcome.spoken_texts) != len(chunks):
                    payload["languages"][lang] = {
                        "status": "failed",
                        "translator": outcome.translator_backend,
                        "text": "",
                        "message": (
                            f"Summary translation incomplete for {lang}: "
                            f"{len(outcome.spoken_texts)}/{len(chunks)} sections"
                        ),
                        "failure_kind": "error",
                    }
                    print(
                        f"[WARN] Summary section count mismatch for {lang}",
                        file=sys.stderr,
                    )
                    continue
                joined = "\n\n".join(str(s).strip() for s in outcome.spoken_texts if s)
                per_lang_path = job_output_dir / f"summary_{lang}.txt"
                per_lang_path.write_text(joined, encoding="utf-8")
                payload["languages"][lang] = {
                    "status": "success",
                    "translator": outcome.translator_backend,
                    "text": joined,
                    "message": "",
                }
            else:
                payload["languages"][lang] = {
                    "status": "failed",
                    "translator": outcome.translator_backend,
                    "text": "",
                    "message": outcome.message or f"Summary translation failed for {lang}",
                    "failure_kind": outcome.failure_kind,
                }
                print(
                    f"[WARN] Summary translation failed for {lang}: {outcome.message}",
                    file=sys.stderr,
                )
    finally:
        reset_google_circuit_breaker(token)

    write_summary_translations(job_output_dir, payload)
    return payload
