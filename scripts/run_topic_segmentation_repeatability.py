#!/usr/bin/env python3
"""Run topic segmentation 3x on the same segments; report scene counts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.topic_segmentation import run_topic_segmentation


def main() -> int:
    segments_path = PROJECT_ROOT / "output" / "text_only_regression" / "segments.json"
    if not segments_path.is_file():
        segments_path = PROJECT_ROOT / "transcripts" / "segments.json"
    with open(segments_path, encoding="utf-8") as f:
        segments = json.load(f)

    counts = []
    for run in range(1, 4):
        scenes = run_topic_segmentation(segments)
        counts.append(len(scenes))
        ids = [s.segment_id for s in scenes]
        print(f"Run {run}: {len(scenes)} scene(s), segment_ids={ids}")

    print(f"Counts: {counts}")
    print(f"Stable: {len(set(counts)) == 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
