"""Helpers for highlight outputs on disk (job directories)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.locales import LOCALES, get_locale


def highlight_file_for_lang(job_dir: Path, lang_code: str) -> Path:
    code = lang_code.strip().lower()
    get_locale(code)
    return job_dir / "highlights" / code / f"highlight_{code}.mp4"


def highlight_status_path(job_dir: Path) -> Path:
    return Path(job_dir) / "highlights" / "highlight_status.json"


def read_highlight_status(job_dir: Path) -> dict[str, Any]:
    path = highlight_status_path(job_dir)
    if not path.is_file():
        return {"languages": {}, "requested_languages": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"languages": {}, "requested_languages": []}


def write_highlight_status(job_dir: Path, payload: dict[str, Any]) -> None:
    path = highlight_status_path(job_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_language_status(
    job_dir: Path,
    lang: str,
    entry: dict[str, Any],
    *,
    requested_languages: Optional[list[str]] = None,
) -> None:
    data = read_highlight_status(job_dir)
    langs = data.setdefault("languages", {})
    langs[lang] = entry
    if requested_languages is not None:
        data["requested_languages"] = requested_languages
    elif not data.get("requested_languages"):
        data["requested_languages"] = list(langs.keys())
    write_highlight_status(job_dir, data)


def list_highlight_language_status(job_dir: Path) -> list[dict[str, Any]]:
    """UI/API view: every requested language with success or failure reason."""
    job_dir = Path(job_dir)
    data = read_highlight_status(job_dir)
    requested = data.get("requested_languages") or list(data.get("languages", {}).keys())
    statuses = data.get("languages", {})
    out: list[dict[str, Any]] = []

    for code in requested:
        spec = LOCALES.get(code)
        name = spec.name if spec else code
        entry = statuses.get(code, {})
        mp4 = highlight_file_for_lang(job_dir, code)
        has_file = mp4.is_file()
        status = entry.get("status") or ("success" if has_file else "unknown")
        if has_file and status != "success":
            status = "success"
        row: dict[str, Any] = {
            "code": code,
            "name": name,
            "status": status,
            "message": entry.get("message", ""),
            "failure_kind": entry.get("failure_kind"),
            "translator": entry.get("translator") or entry.get("translator_backend") or "",
            "translation_seconds": entry.get("translation_seconds"),
            "tts_seconds": entry.get("tts_seconds"),
            "render_seconds": entry.get("render_seconds"),
            "total_seconds": entry.get("total_seconds"),
            "download_available": has_file,
        }
        if has_file:
            row["filename"] = mp4.name
        out.append(row)
    return out


def list_highlight_downloads(job_dir: Path) -> list[dict[str, Any]]:
    job_dir = Path(job_dir)
    found: list[dict[str, Any]] = []
    for row in list_highlight_language_status(job_dir):
        if row.get("download_available"):
            found.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "filename": row.get("filename", f"highlight_{row['code']}.mp4"),
                }
            )
    return found


def failed_highlight_languages(job_dir: Path) -> list[str]:
    failed: list[str] = []
    for row in list_highlight_language_status(job_dir):
        if row.get("status") == "failed":
            failed.append(str(row["code"]))
    return failed
