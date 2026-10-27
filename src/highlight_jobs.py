"""Helpers for highlight outputs on disk (job directories)."""

from __future__ import annotations

from src.user_messages import GENERIC_HIGHLIGHT_FAILURE, public_error

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


def attempted_highlight_languages(job_dir: Path) -> list[str]:
    """Languages the user asked for, plus any language that has a status record (it was attempted).

    Requested languages come from the saved list. A file written before that list existed has no key, so only
    languages with a record count, and nothing is assumed about the rest. Languages that were never requested are
    left out entirely.
    """
    data = read_highlight_status(job_dir)
    statuses = data.get("languages") or {}
    ordered: list[str] = []
    if "requested_languages" in data:
        ordered.extend(str(code).strip().lower() for code in (data.get("requested_languages") or []))
    ordered.extend(str(code).strip().lower() for code in statuses.keys())
    seen: set[str] = set()
    return [c for c in ordered if c and not (c in seen or seen.add(c))]


def list_highlight_language_status(job_dir: Path) -> list[dict[str, Any]]:
    """UI/API view of the requested highlight languages.

    Each row is one of: success (a file exists), failed (attempted and failed, with the reason), or not_generated
    (requested but no record and no file). Languages that were not requested have no row.
    """
    job_dir = Path(job_dir)
    data = read_highlight_status(job_dir)
    statuses = data.get("languages", {}) or {}
    out: list[dict[str, Any]] = []

    for code in attempted_highlight_languages(job_dir):
        spec = LOCALES.get(code)
        name = spec.name if spec else code
        entry = statuses.get(code, {}) or {}
        mp4 = highlight_file_for_lang(job_dir, code)
        has_file = mp4.is_file()
        if has_file:
            status = "success"
        elif entry.get("status") == "failed":
            status = "failed"
        elif entry.get("status") == "success":
            status = "failed"  # a success record with no file is not a usable highlight
        else:
            status = "not_generated"
        row: dict[str, Any] = {
            "code": code,
            "name": name,
            "status": status,
            # Older status files may hold provider wording; the UI only ever receives user-safe text.
            "message": public_error(entry.get("message"), default=GENERIC_HIGHLIGHT_FAILURE) if entry.get("message") else "",
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


def list_output_languages(job_dir: Path) -> list[dict[str, Any]]:
    """Languages for results UI: highlight langs + comprehensive summary translations."""
    job_dir = Path(job_dir)
    data = read_highlight_status(job_dir)
    codes: list[str] = []
    seen: set[str] = set()

    def _add(code: str) -> None:
        c = (code or "").strip().lower()
        if not c or c in seen:
            return
        seen.add(c)
        codes.append(c)

    for code in data.get("requested_languages") or list(data.get("languages", {}).keys()):
        _add(str(code))

    try:
        from src.summary_translate import read_summary_translations

        summary_data = read_summary_translations(job_dir)
        for code in summary_data.get("requested_languages") or []:
            _add(str(code))
        for code in (summary_data.get("languages") or {}).keys():
            _add(str(code))
    except ImportError:
        pass

    _add("en")

    out: list[dict[str, Any]] = []
    for code in codes:
        spec = LOCALES.get(code)
        if spec:
            out.append({"code": code, "name": spec.name})
    return out if out else [{"code": "en", "name": "English"}]


def failed_highlight_languages(job_dir: Path) -> list[str]:
    failed: list[str] = []
    for row in list_highlight_language_status(job_dir):
        if row.get("status") == "failed":
            failed.append(str(row["code"]))
    return failed
