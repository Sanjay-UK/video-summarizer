#!/usr/bin/env python3
"""Full highlight pipeline test → highlight_en.mp4."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.highlight_pipeline import run_highlight_pipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--segments", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output" / "highlight_full_test")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-scenes", type=int, default=0)
    args = parser.parse_args()

    with open(args.segments, encoding="utf-8") as f:
        segments = json.load(f)

    args.output.mkdir(parents=True, exist_ok=True)
    out = run_highlight_pipeline(
        args.video,
        segments,
        args.output,
        model=args.model,
        max_scenes=args.max_scenes,
    )
    print(f"[DONE] Highlight video: {out} ({out.stat().st_size / (1024*1024):.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
