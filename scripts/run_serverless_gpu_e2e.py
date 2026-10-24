#!/usr/bin/env python3
"""Smoke-test RunPod serverless heavy phase; prints delayTime / executionTime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.gpu_selection import load_env_file  # noqa: E402
from src.runpod_serverless import (  # noqa: E402
    load_endpoint_id_from_selection,
    run_heavy_phase_via_serverless,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--job-id", default="e2e-serverless")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output" / "e2e_serverless")
    args = parser.parse_args()
    load_env_file()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(msg, flush=True)

    endpoint_id = load_endpoint_id_from_selection()
    result = run_heavy_phase_via_serverless(
        endpoint_id=endpoint_id,
        job_id=args.job_id,
        audio_path=args.audio,
        job_output_dir=out,
        whisper_model="tiny",
        summarization_model="llama3.2:3b",
        compression="medium",
        highlight_languages="en",
        log=log,
    )
    print("delayTime ms:", result.get("delayTime"))
    print("executionTime ms:", result.get("executionTime"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
