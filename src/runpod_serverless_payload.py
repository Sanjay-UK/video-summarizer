"""Build RunPod serverless job inputs (gzip+base64 audio, manifests)."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any

# RunPod async /run payload limit
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
SAFE_PAYLOAD_BYTES = 9 * 1024 * 1024


def _gzip_b64(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data)).decode("ascii")


def build_heavy_phase_input(
    *,
    audio_path: Path,
    whisper_model: str,
    summarization_model: str,
    compression: str,
    highlight_languages: str,
    job_id: str,
) -> dict[str, Any]:
    raw = audio_path.read_bytes()
    compressed = gzip.compress(raw)
    b64 = base64.b64encode(compressed).decode("ascii")
    payload_estimate = len(
        json.dumps(
            {
                "job_type": "heavy_phase",
                "job_id": job_id,
                "audio_gzip_b64": b64,
                "audio_file": audio_path.name,
            }
        ).encode("utf-8")
    )
    if payload_estimate > SAFE_PAYLOAD_BYTES:
        mb = payload_estimate / (1024 * 1024)
        raise ValueError(
            f"Audio job payload ~{mb:.1f} MB exceeds RunPod /run limit (~10 MB). "
            "Use a shorter clip or pre-stage audio on the network volume (future path)."
        )
    return {
        "job_type": "heavy_phase",
        "job_id": job_id,
        "audio_file": audio_path.name,
        "audio_gzip_b64": b64,
        "whisper_model": whisper_model,
        "summarization_model": summarization_model,
        "compression": compression,
        "highlight_languages": highlight_languages,
    }


def build_nllb_batch_input(*, language_code: str, texts: list[str]) -> dict[str, Any]:
    return {
        "job_type": "nllb_batch",
        "language": language_code,
        "texts": texts,
    }


def decode_audio_to_path(input_payload: dict[str, Any], dest: Path) -> Path:
    name = input_payload.get("audio_file") or "audio.mp3"
    b64 = input_payload.get("audio_gzip_b64")
    if not b64:
        raise ValueError("Missing audio_gzip_b64 in job input")
    raw = gzip.decompress(base64.b64decode(b64))
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = dest.parent / name
    out.write_bytes(raw)
    return out
