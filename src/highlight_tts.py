"""Per-scene TTS for highlight video."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional

from config import HIGHLIGHT_TTS_VOICE
from src.highlight_ffmpeg import probe_duration_seconds
from src.tts_generator import enhance_text_for_tts

BoundaryKind = Literal["WordBoundary", "SentenceBoundary", "none"]


@dataclass
class TtsResult:
    audio_path: Path
    spoken_text: str
    duration_seconds: float
    boundary_kind: BoundaryKind
    boundaries: List[dict]


async def _synthesize_async(
    text: str,
    output_path: Path,
    *,
    voice: str = HIGHLIGHT_TTS_VOICE,
) -> TtsResult:
    from edge_tts import Communicate

    output_path.parent.mkdir(parents=True, exist_ok=True)
    enhanced = enhance_text_for_tts(text)
    boundaries: List[dict] = []
    boundary_kind: BoundaryKind = "none"

    communicate = Communicate(
        enhanced,
        voice=voice,
        rate="-10%",
        boundary="WordBoundary",
    )

    audio_chunks: List[bytes] = []
    async for message in communicate.stream():
        if message["type"] == "audio":
            audio_chunks.append(message["data"])
        elif message["type"] in ("WordBoundary", "SentenceBoundary"):
            boundaries.append(message)
            boundary_kind = message["type"]

    if not audio_chunks:
        raise RuntimeError("Edge TTS returned no audio")

    output_path.write_bytes(b"".join(audio_chunks))
    duration = probe_duration_seconds(output_path)
    return TtsResult(
        audio_path=output_path,
        spoken_text=enhanced,
        duration_seconds=duration,
        boundary_kind=boundary_kind if boundaries else "none",
        boundaries=boundaries,
    )


def _gtts_fallback(text: str, output_path: Path) -> TtsResult:
    from gtts import gTTS

    output_path.parent.mkdir(parents=True, exist_ok=True)
    gTTS(text=text, lang="en").save(str(output_path))
    duration = probe_duration_seconds(output_path)
    return TtsResult(
        audio_path=output_path,
        spoken_text=text,
        duration_seconds=duration,
        boundary_kind="none",
        boundaries=[],
    )


def generate_scene_narration(text: str, output_path: Path, *, voice: str = HIGHLIGHT_TTS_VOICE) -> TtsResult:
    loop = asyncio.new_event_loop()
    try:
        try:
            return loop.run_until_complete(
                _synthesize_async(text, output_path, voice=voice)
            )
        except Exception:
            return _gtts_fallback(text, output_path)
    finally:
        loop.close()
