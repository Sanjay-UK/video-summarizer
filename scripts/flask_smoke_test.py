#!/usr/bin/env python3
"""Smoke-test Flask: text-only artifacts vs highlight (fixture translations by default)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Configure translation policy before app / pipeline import side effects
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow a single real Google Translate call (multilang test only)",
    )
    return parser.parse_args()


_ARGS = _parse_args()
from src.translators.google_guard import configure_test_google_policy  # noqa: E402

configure_test_google_policy(live=_ARGS.live)
from src.translators.fixture_backend import reset_fixture_cache  # noqa: E402

reset_fixture_cache()

from app import app  # noqa: E402
from src.highlight_jobs import list_highlight_language_status  # noqa: E402
from src.highlight_pipeline import run_highlight_pipeline  # noqa: E402
from src.job_video import copy_source_video_to_job  # noqa: E402


def test_text_only_job(job_id: str) -> None:
    """Verify standard outputs exist and highlight is absent (checkbox off behavior)."""
    print(f"\n=== checkbox OFF — job {job_id} ===")
    job_dir = PROJECT_ROOT / "output" / job_id
    for name in ("summary.txt", "summary.srt", "audio_explanation.mp3"):
        path = job_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise AssertionError(f"Missing or empty {name}")

    with app.test_client() as client:
        assert client.get(f"/download/summary/{job_id}").status_code == 200
        assert client.get(f"/download/subtitles/{job_id}").status_code == 200
        assert client.get(f"/download/audio/{job_id}").status_code == 200
        hl = client.get(f"/download/highlight/{job_id}")
        assert hl.status_code == 404, "highlight must not exist for text-only job"

        resp = client.get(f"/api/results/{job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("summary_length", 0) > 50
        assert not data.get("highlight_video")
    print("OK checkbox OFF")


def test_highlight_job() -> None:
    """Run highlight pipeline once and verify download route."""
    print("\n=== checkbox ON — highlight generation (fixture/offline) ===")
    job_id = f"smoke{uuid.uuid4().hex[:6]}"
    job_dir = PROJECT_ROOT / "output" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ref = PROJECT_ROOT / "output" / "text_only_regression"
    shutil.copy2(ref / "segments.json", job_dir / "segments.json")
    shutil.copy2(ref / "summary.txt", job_dir / "summary.txt")
    shutil.copy2(ref / "transcript.txt", job_dir / "transcript.txt")
    copy_source_video_to_job(PROJECT_ROOT / "transcripts" / "source.mp4", job_dir)

    segments = json.loads((job_dir / "segments.json").read_text(encoding="utf-8"))
    out = run_highlight_pipeline(
        job_dir / "source.mp4",
        segments,
        job_dir,
    )
    print(f"Wrote {out}")

    with app.test_client() as client:
        hl = client.get(f"/download/highlight/{job_id}")
        assert hl.status_code == 200, f"expected 200, got {hl.status_code}"
        resp = client.get(f"/api/results/{job_id}")
        assert resp.status_code == 200
        assert resp.get_json().get("highlight_video") is True
    print("OK checkbox ON")


def test_highlight_multilang_job() -> None:
    """Three languages (en, hi, te) — fixture translations, no live Google."""
    print("\n=== highlight — 3 languages (en, hi, te) ===")
    if _ARGS.live:
        os.environ["HIGHLIGHT_TRANSLATION_CHAIN"] = "google"
        os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "0"
    job_id = f"smoke3{uuid.uuid4().hex[:5]}"
    job_dir = PROJECT_ROOT / "output" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ref = PROJECT_ROOT / "output" / "text_only_regression"
    if not (ref / "segments.json").is_file():
        print("SKIP 3-lang (missing text_only_regression)")
        return

    requested = ["en", "hi", "te"]
    shutil.copy2(ref / "segments.json", job_dir / "segments.json")
    if (ref / "summary.txt").is_file():
        shutil.copy2(ref / "summary.txt", job_dir / "summary.txt")
    if (ref / "transcript.txt").is_file():
        shutil.copy2(ref / "transcript.txt", job_dir / "transcript.txt")
    copy_source_video_to_job(PROJECT_ROOT / "transcripts" / "source.mp4", job_dir)
    segments = json.loads((job_dir / "segments.json").read_text(encoding="utf-8"))
    run_highlight_pipeline(
        job_dir / "source.mp4",
        segments,
        job_dir,
        max_scenes=1,
        compression="high",
        languages=requested,
    )

    rows = list_highlight_language_status(job_dir)
    for code in requested:
        row = next((r for r in rows if r["code"] == code), None)
        assert row, f"missing status for {code}"
        if not _ARGS.live:
            assert row["status"] == "success", f"{code}: {row.get('message')}"
            assert row.get("download_available"), f"no mp4 for {code}"
            if code != "en":
                assert row.get("translator") == "fixture", row

    with app.test_client() as client:
        assert client.get(f"/download/highlight/{job_id}/en").status_code == 200
        api = client.get(f"/api/results/{job_id}")
        assert api.status_code == 200
        data = api.get_json()
        langs = data.get("highlight_languages", [])
        codes = {x["code"] for x in langs}
        assert "en" in codes, f"expected en in {codes}"
        status_rows = data.get("highlight_language_status") or []
        assert len(status_rows) >= len(requested)
        for lang in ("hi", "te"):
            if lang in codes:
                assert client.get(f"/download/highlight/{job_id}/{lang}").status_code == 200
    print("OK 3 languages (en, hi, te)")


def main() -> int:
    test_text_only_job("text_only_regression")
    test_highlight_job()
    test_highlight_multilang_job()
    print("\nAll Flask smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
