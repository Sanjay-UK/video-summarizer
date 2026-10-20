#!/usr/bin/env python3
"""Benchmark (b): 448s highlight, 5 languages — NLLB for Indic (no live Google by default)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow live Google (not recommended; quota)",
    )
    parser.add_argument(
        "--job",
        type=Path,
        default=PROJECT_ROOT / "output" / "benchmark_448_5lang",
    )
    args = parser.parse_args()

    if args.live:
        from src.translators.google_guard import configure_test_google_policy

        configure_test_google_policy(live=True)
    else:
        os.environ["HIGHLIGHT_BLOCK_LIVE_GOOGLE"] = "1"
        os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "1"
        os.environ["HIGHLIGHT_TRANSLATION_CHAIN"] = "nllb,ollama"

    from src.highlight_jobs import list_highlight_language_status
    from src.highlight_pipeline import run_highlight_pipeline

    job = args.job
    segments_path = job / "segments.json"
    video = job / "source.mp4"
    if not segments_path.is_file() or not video.is_file():
        print(f"Missing {segments_path} or {video}", file=sys.stderr)
        return 1

    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    langs = ["en", "hi", "kn", "te", "ta"]

    print(f"[B] job={job} compression=high languages={langs}")
    t0 = time.perf_counter()
    run_highlight_pipeline(
        video,
        segments,
        job,
        compression="high",
        languages=langs,
        rebuild_shared=False,
    )
    wall = time.perf_counter() - t0

    rows = list_highlight_language_status(job)
    print("\n=== Benchmark (b) ===")
    print(f"Wall clock: {wall:.1f}s")
    succ = []
    for r in rows:
        code = r["code"]
        if r["status"] == "success" and r.get("download_available"):
            succ.append(code)
            print(
                f"{code}: OK [{r.get('translator')}] "
                f"translate={r.get('translation_seconds')}s "
                f"TTS={r.get('tts_seconds')}s render={r.get('render_seconds')}s "
                f"total={r.get('total_seconds')}s"
            )
        else:
            print(f"{code}: FAIL [{r.get('translator')}] {r.get('message')}")
    print("Succeeded:", succ)
    return 0 if len(succ) == len(langs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
