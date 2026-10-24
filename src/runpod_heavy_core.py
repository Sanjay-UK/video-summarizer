"""Shared GPU heavy-phase logic (local worker script + RunPod serverless handler)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config import choose_whisper_model
from src.highlight_ffmpeg import probe_duration_seconds
from src.locales import parse_language_list
from src.summary_translate import translate_summary_for_languages
from src.summarizer import summarize_transcript
from src.transcriber import transcribe_audio


def run_heavy_phase(workdir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """
    Transcribe → summarize (Ollama) → summary translations.
    Writes artifacts under workdir; returns metadata for serverless responses.
    """
    workdir = Path(workdir)
    audio_name = manifest.get("audio_file", "audio.mp3")
    audio_path = workdir / audio_name
    if not audio_path.is_file():
        raise FileNotFoundError(f"Missing audio {audio_path}")

    whisper_model = manifest.get("whisper_model")
    if not whisper_model:
        duration = probe_duration_seconds(audio_path)
        whisper_model = choose_whisper_model(duration)

    os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
    os.environ["WHISPER_DEVICE"] = "cuda"
    # Worker runs NLLB locally; never recurse through RunPod proxy.
    os.environ["NLLB_VIA_RUNPOD"] = "0"

    transcribe_only = os.environ.get("RUNPOD_WORKER_TRANSCRIBE_ONLY", "").strip() == "1"
    transcribe_audio(
        audio_path,
        model=whisper_model,
        output_dir=workdir,
        device="cuda",
    )

    if transcribe_only:
        done = {"ok": True, "whisper_model": whisper_model, "phase": "transcribe_only"}
        (workdir / "heavy_done.json").write_text(json.dumps(done, indent=2), encoding="utf-8")
        return done

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

    done = {"ok": True, "whisper_model": whisper_model}
    (workdir / "heavy_done.json").write_text(json.dumps(done, indent=2), encoding="utf-8")
    return done
