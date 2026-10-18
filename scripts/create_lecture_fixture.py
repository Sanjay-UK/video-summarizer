#!/usr/bin/env python3
"""Create a low-motion lecture-style test video (static slide + voiceover-like silence)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT = PROJECT_ROOT / "transcripts" / "lecture_static.mp4"
SOURCE = PROJECT_ROOT / "transcripts" / "source.mp4"
FRAME = PROJECT_ROOT / "transcripts" / "lecture_slide.jpg"


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "command failed")


def main() -> int:
    if not SOURCE.is_file():
        print(f"Missing {SOURCE}", file=sys.stderr)
        return 1

    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "120",
            "-i",
            str(SOURCE),
            "-frames:v",
            "1",
            str(FRAME),
        ]
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(FRAME),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "90",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(OUT),
        ]
    )
    print(f"Created {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
