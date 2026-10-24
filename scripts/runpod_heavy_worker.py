#!/usr/bin/env python3
"""Run GPU-heavy pipeline steps on a RunPod pod (invoked via SSH)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runpod_heavy_core import run_heavy_phase  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    workdir = Path(args.workdir)
    manifest_path = workdir / "heavy_manifest.json"
    if not manifest_path.is_file():
        print(f"Missing {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audio_name = manifest.get("audio_file", "audio.mp3")
    audio_path = workdir / audio_name
    if not audio_path.is_file():
        print(f"Missing audio {audio_path}", file=sys.stderr)
        return 2

    run_heavy_phase(workdir, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
