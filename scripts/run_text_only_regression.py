#!/usr/bin/env python3
"""Run text-only pipeline (no highlight) on a local video; verify standard artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import SUMMARIZATION_MODEL
from src.audio_extractor import extract_audio
from src.subtitle_generator import generate_subtitles
from src.summarizer import summarize_transcript
from src.transcriber import transcribe_audio
from src.tts_generator import generate_audio_explanation


def main() -> int:
    video = PROJECT_ROOT / "transcripts" / "source.mp4"
    out = PROJECT_ROOT / "output" / "text_only_regression"
    out.mkdir(parents=True, exist_ok=True)

    if not video.is_file():
        print(f"Missing test video: {video}", file=sys.stderr)
        return 1

    print("[1/5] Extract audio...")
    audio = extract_audio(video, out)

    print("[2/5] Transcribe...")
    transcribe_audio(audio, output_dir=out)

    print("[3/5] Summarize...")
    summarize_transcript(
        transcript_path=out / "transcript.txt",
        segments_path=out / "segments.json",
        output_dir=out,
        model=SUMMARIZATION_MODEL,
        compression="medium",
    )

    print("[4/5] Audio explanation...")
    summary_text = (out / "summary.txt").read_text(encoding="utf-8")
    _audio_path, audio_transcript = generate_audio_explanation(
        summary_text,
        output_path=out / "audio_explanation.mp3",
    )

    print("[5/5] Subtitles...")
    generate_subtitles(
        summary_segments_path=out / "summary_segments.json",
        output_path=out / "summary.srt",
        audio_transcript=audio_transcript,
    )

    required = ["summary.txt", "summary.srt", "audio_explanation.mp3", "summary_segments.json"]
    ok = True
    for name in required:
        path = out / name
        exists = path.is_file() and path.stat().st_size > 0
        print(f"  {'OK' if exists else 'MISSING'} {name} ({path.stat().st_size if exists else 0} bytes)")
        ok = ok and exists

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
