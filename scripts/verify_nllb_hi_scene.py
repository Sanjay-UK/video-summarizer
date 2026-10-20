#!/usr/bin/env python3
"""One Hindi scene via NLLB: translation text, TTS, caption frame (no Google)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "1"
os.environ["HIGHLIGHT_TRANSLATION_CHAIN"] = "nllb,ollama"

from src.highlight_ffmpeg import run_ffmpeg
from src.highlight_pipeline import run_highlight_pipeline
from src.translators.nllb_backend import nllb_backend_available


def extract_preview_frame(scene_mp4: Path, out_jpg: Path, at_sec: float = 3.0) -> None:
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-ss",
            f"{at_sec:.2f}",
            "-i",
            str(scene_mp4),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_jpg),
        ]
    )


def main() -> int:
    if not nllb_backend_available():
        print("NLLB not installed. Run: pip install -r requirements-nllb.txt", file=sys.stderr)
        return 1

    video = PROJECT_ROOT / "transcripts" / "source.mp4"
    segments_path = PROJECT_ROOT / "output" / "text_only_regression" / "segments.json"
    if not segments_path.is_file():
        segments_path = PROJECT_ROOT / "transcripts" / "segments.json"
    out_dir = PROJECT_ROOT / "output" / "nllb_hi_scene_verify"
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    run_highlight_pipeline(
        video,
        segments,
        out_dir,
        max_scenes=1,
        compression="high",
        languages=["en", "hi"],
        rebuild_shared=True,
    )

    hi_root = out_dir / "highlights" / "hi"
    status = json.loads((hi_root / "language_status.json").read_text(encoding="utf-8"))
    scene_dir = hi_root / "scene_000"
    text = (scene_dir / "narration.txt").read_text(encoding="utf-8").strip()
    final_mp4 = scene_dir / "build" / "scene_000_final.mp4"
    preview = out_dir / "nllb_hi_caption_frame.jpg"

    print("=== NLLB Hindi scene verify ===")
    print(f"Backend: {status.get('translator')}")
    print(f"Translation text:\n{text}\n")
    print(f"TTS: {scene_dir / 'narration.mp3'}")
    if final_mp4.is_file():
        extract_preview_frame(final_mp4, preview)
        print(f"Caption frame: {preview}")
    else:
        print(f"Missing render: {final_mp4}", file=sys.stderr)
        return 1
    if status.get("translator") != "nllb":
        print(f"Expected translator nllb, got {status.get('translator')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
