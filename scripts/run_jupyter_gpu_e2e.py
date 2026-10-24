#!/usr/bin/env python3
"""Run hybrid heavy phase via Jupyter (smoke test)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from src.gpu_selection import load_env_file  # noqa: E402
from src.pipeline_executor import HybridRunpodPipelineExecutor  # noqa: E402


def main() -> int:
    load_env_file()
    os.environ.setdefault("RUNPOD_REMOTE_TRANSPORT", "jupyter")

    video = PROJECT / "transcripts" / "smoke_clip.mp4"
    audio = PROJECT / "output" / "_e2e_audio.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "4",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )

    job_id = "e2e" + uuid.uuid4().hex[:6]
    out = PROJECT / "output" / job_id
    out.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(msg, flush=True)

    HybridRunpodPipelineExecutor().run_heavy_phase(
        job_id=job_id,
        audio_path=audio,
        job_output_dir=out,
        whisper_model="tiny",
        summarization_model="llama3.2:3b",
        compression="medium",
        highlight_languages="en",
        log=log,
    )
    print("JOB_ID", job_id, flush=True)
    print("SUMMARY", (out / "summary.txt").read_text(encoding="utf-8")[:2000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
