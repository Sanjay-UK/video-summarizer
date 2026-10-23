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

from config import choose_whisper_model  # noqa: E402
from src.highlight_ffmpeg import probe_duration_seconds  # noqa: E402
from src.locales import parse_language_list  # noqa: E402
from src.summary_translate import translate_summary_for_languages  # noqa: E402
from src.summarizer import summarize_transcript  # noqa: E402
from src.transcriber import transcribe_audio  # noqa: E402


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

    whisper_model = manifest.get("whisper_model")
    if not whisper_model:
        duration = probe_duration_seconds(audio_path)
        whisper_model = choose_whisper_model(duration)

    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
    os.environ["WHISPER_DEVICE"] = "cuda"

    transcribe_audio(
        audio_path,
        model=whisper_model,
        output_dir=workdir,
        device="cuda",
    )

    model = manifest.get("summarization_model", "llama3.2:3b")
    compression = manifest.get("compression", "medium")
    summarize_transcript(
        transcript_path=workdir / "transcript.txt",
        segments_path=workdir / "segments.json",
        output_dir=workdir,
        model=model,
        compression=compression,
    )

    langs = parse_language_list(manifest.get("highlight_languages", "en"))
    summary_text = (workdir / "summary.txt").read_text(encoding="utf-8")
    translate_summary_for_languages(workdir, summary_text, langs)

    (workdir / "heavy_done.json").write_text(
        json.dumps({"ok": True, "whisper_model": whisper_model}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
