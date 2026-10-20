#!/usr/bin/env python3
"""Test (a): one highlight scene in all configured languages — text, audio, caption frame."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.highlight_ffmpeg import run_ffmpeg
from src.highlight_pipeline import run_highlight_pipeline
from src.locales import LOCALES


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
    video = PROJECT_ROOT / "transcripts" / "source.mp4"
    segments_path = PROJECT_ROOT / "output" / "text_only_regression" / "segments.json"
    if not segments_path.is_file():
        segments_path = PROJECT_ROOT / "transcripts" / "segments.json"
    out_dir = PROJECT_ROOT / "output" / "multilang_one_scene_test"
    langs = ",".join(LOCALES.keys())

    with open(segments_path, encoding="utf-8") as f:
        segments = json.load(f)

    print(f"Job dir: {out_dir}")
    print(f"Languages: {langs}")
    print(f"Video: {video}")
    print(f"Segments: {segments_path}\n")

    run_highlight_pipeline(
        video,
        segments,
        out_dir,
        max_scenes=1,
        compression="high",
        languages=langs,
    )

    preview_root = out_dir / "previews"
    en_line = (out_dir / "highlights" / "_shared" / "english_narrations.json").read_text(
        encoding="utf-8"
    )
    en_json = json.loads(en_line)
    print("=== English (source narration, word-capped) ===")
    print(en_json["texts"][0])
    print()

    for code in LOCALES:
        lang_root = out_dir / "highlights" / code
        scene_dir = lang_root / "scene_000"
        narration_txt = (scene_dir / "narration.txt").read_text(encoding="utf-8").strip()
        audio = scene_dir / "narration.mp3"
        manifest = json.loads((lang_root / "narration_manifest.json").read_text(encoding="utf-8"))
        scene_meta = manifest["scenes"][0]
        final_mp4 = scene_dir / "build" / "scene_000_final.mp4"
        preview = preview_root / f"scene0_{code}.jpg"

        print(f"=== {code} ({LOCALES[code].name}) ===")
        print(f"Voice: {scene_meta.get('tts_voice', '?')}")
        print(f"Text: {narration_txt}")
        print(f"TTS: {audio} ({scene_meta['narration_seconds']:.2f}s)")
        print(f"Boundary: {scene_meta.get('tts_boundary_kind')}")

        if final_mp4.is_file():
            extract_preview_frame(final_mp4, preview, at_sec=min(4.0, scene_meta["display_seconds"] / 2))
            print(f"Caption frame: {preview}")
        else:
            print(f"[WARN] Missing render: {final_mp4}")
        print()

    print("Review the JPGs under:", preview_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
