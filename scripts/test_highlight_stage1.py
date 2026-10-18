#!/usr/bin/env python3
"""Stage-1 test: chunking → topic segmentation → visual extraction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.highlight_chunking import chunk_whisper_segments
from src.topic_segmentation import run_topic_segmentation, write_highlight_scenes
from src.visual_extractor import extract_visuals_for_scenes, write_visual_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Test highlight pipeline stage 1")
    parser.add_argument(
        "--video",
        type=Path,
        default=PROJECT_ROOT / "transcripts" / "source.mp4",
        help="Source video path",
    )
    parser.add_argument(
        "--segments",
        type=Path,
        default=PROJECT_ROOT / "transcripts" / "segments.json",
        help="Whisper segments.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "output" / "highlight_stage1_test",
        help="Output directory",
    )
    parser.add_argument("--model", default=None, help="Ollama model override")
    parser.add_argument(
        "--max-scenes",
        type=int,
        default=0,
        help="Limit visuals for quick tests (0 = all scenes)",
    )
    parser.add_argument(
        "--profile",
        choices=("default", "sushi", "lecture"),
        default="default",
        help="sushi=source.mp4; lecture=static slide fixture (visual still test)",
    )
    args = parser.parse_args()

    if args.profile == "sushi":
        args.video = PROJECT_ROOT / "transcripts" / "source.mp4"
        args.segments = PROJECT_ROOT / "transcripts" / "segments.json"
        args.output = PROJECT_ROOT / "output" / "highlight_stage1_sushi"
    elif args.profile == "lecture":
        args.video = PROJECT_ROOT / "transcripts" / "lecture_static.mp4"
        args.segments = PROJECT_ROOT / "transcripts" / "segments.json"
        args.output = PROJECT_ROOT / "output" / "highlight_stage1_lecture"

    if not args.video.is_file():
        print(f"Video not found: {args.video}", file=sys.stderr)
        return 1
    if not args.segments.is_file():
        print(f"Segments not found: {args.segments}", file=sys.stderr)
        return 1

    args.output.mkdir(parents=True, exist_ok=True)

    with open(args.segments, encoding="utf-8") as f:
        segments = json.load(f)

    chunks = chunk_whisper_segments(segments)
    print(f"[OK] {len(segments)} segments → {len(chunks)} LLM chunk(s)")
    for c in chunks:
        print(
            f"     chunk {c.chunk_index}: seg {c.global_segment_start}-{c.global_segment_end}, "
            f"{c.time_start:.1f}s–{c.time_end:.1f}s, {len(c.llm_text)} chars"
        )

    if args.profile == "lecture":
        from src.topic_segmentation import TopicScene

        scenes = [
            TopicScene(0, 5.0, "Intro slide", "Static lecture intro."),
            TopicScene(1, 35.0, "Key idea", "Main point on slide."),
            TopicScene(2, 65.0, "Summary slide", "Recap on slide."),
        ]
        print("[OK] lecture profile: using fixed low-motion topic times (no LLM)")
    else:
        scenes = run_topic_segmentation(segments, model=args.model)
    scenes_path = write_highlight_scenes(args.output / "highlight_scenes.json", scenes)
    print(f"[OK] {len(scenes)} topic scene(s) → {scenes_path}")
    for i, s in enumerate(scenes[:8]):
        print(f"     {i}: seg={s.segment_id} t={s.source_time:.1f}s — {s.topic_title[:60]}")
    if len(scenes) > 8:
        print(f"     ... and {len(scenes) - 8} more")

    to_visualize = scenes
    if args.max_scenes > 0:
        to_visualize = scenes[: args.max_scenes]

    selections = extract_visuals_for_scenes(args.video, to_visualize, args.output)
    manifest_path = write_visual_manifest(args.output / "visual_manifest.json", selections)
    print(f"[OK] {len(selections)} visual(s) → {manifest_path}")
    for v in selections:
        print(f"     scene {v.scene_index}: {v.mode} score={v.sharpness_score} → {v.visual_path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
