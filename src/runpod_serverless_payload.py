"""Build RunPod serverless job inputs (gzip+base64 audio, manifests)."""

from __future__ import annotations

import base64
import gzip
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

# RunPod async /run payload limit
MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
SAFE_PAYLOAD_BYTES = 9 * 1024 * 1024

LogFn = Callable[[str], None]


def _gzip_b64(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data)).decode("ascii")


def estimate_heavy_phase_payload_bytes(
    *,
    audio_path: Path,
    job_id: str,
    whisper_model: str = "base",
    summarization_model: str = "llama3.2:3b",
    compression: str = "medium",
    highlight_languages: str = "en",
) -> int:
    raw = audio_path.read_bytes()
    b64 = _gzip_b64(raw)
    body = {
        "job_type": "heavy_phase",
        "job_id": job_id,
        "audio_gzip_b64": b64,
        "audio_file": audio_path.name,
        "whisper_model": whisper_model,
        "summarization_model": summarization_model,
        "compression": compression,
        "highlight_languages": highlight_languages,
    }
    return len(json.dumps(body).encode("utf-8"))


def _reencode_audio_for_whisper(
    src: Path,
    dest: Path,
    *,
    bitrate_kbps: int,
) -> None:
    from src.highlight_ffmpeg import resolve_ffmpeg_bin

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate_kbps}k",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(f"FFmpeg could not shrink audio for RunPod: {tail}")


def ensure_audio_fits_runpod_payload(
    audio_path: Path,
    job_id: str,
    *,
    log: LogFn | None = None,
    whisper_model: str = "base",
    summarization_model: str = "llama3.2:3b",
    compression: str = "medium",
    highlight_languages: str = "en",
) -> Path:
    """
    Return an audio path whose heavy_phase JSON payload fits RunPod's ~10 MB /run limit.
    Re-encodes to mono 16 kHz MP3 at lower bitrates when needed (Whisper-friendly).
    """
    estimate_kwargs = {
        "job_id": job_id,
        "whisper_model": whisper_model,
        "summarization_model": summarization_model,
        "compression": compression,
        "highlight_languages": highlight_languages,
    }
    size = estimate_heavy_phase_payload_bytes(audio_path=audio_path, **estimate_kwargs)
    if size <= SAFE_PAYLOAD_BYTES:
        return audio_path

    def step(msg: str) -> None:
        if log:
            log(msg)

    mb = size / (1024 * 1024)
    step(
        f"Audio RunPod payload ~{mb:.1f} MB exceeds ~9 MB safe limit; "
        "re-encoding to mono 16 kHz for serverless upload…"
    )
    shrunk = audio_path.parent / "audio_runpod.mp3"
    for kbps in (64, 48, 32, 24, 16, 12):
        _reencode_audio_for_whisper(audio_path, shrunk, bitrate_kbps=kbps)
        size = estimate_heavy_phase_payload_bytes(audio_path=shrunk, **estimate_kwargs)
        if size <= SAFE_PAYLOAD_BYTES:
            step(
                f"Using compressed audio ({kbps} kbps mono 16 kHz, "
                f"payload ~{size / (1024 * 1024):.1f} MB)."
            )
            return shrunk

    mb = size / (1024 * 1024)
    raise ValueError(
        f"Audio still ~{mb:.1f} MB after maximum compression for RunPod /run (~10 MB). "
        "Try a shorter video or split the lecture."
    )


def build_heavy_phase_input(
    *,
    audio_path: Path,
    whisper_model: str,
    summarization_model: str,
    compression: str,
    highlight_languages: str,
    job_id: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    send_path = ensure_audio_fits_runpod_payload(
        audio_path,
        job_id,
        log=log,
        whisper_model=whisper_model,
        summarization_model=summarization_model,
        compression=compression,
        highlight_languages=highlight_languages,
    )
    raw = send_path.read_bytes()
    b64 = _gzip_b64(raw)
    payload_estimate = estimate_heavy_phase_payload_bytes(
        audio_path=send_path,
        job_id=job_id,
        whisper_model=whisper_model,
        summarization_model=summarization_model,
        compression=compression,
        highlight_languages=highlight_languages,
    )
    if payload_estimate > SAFE_PAYLOAD_BYTES:
        mb = payload_estimate / (1024 * 1024)
        raise ValueError(
            f"Audio job payload ~{mb:.1f} MB exceeds RunPod /run limit (~10 MB)."
        )
    return {
        "job_type": "heavy_phase",
        "job_id": job_id,
        "audio_file": send_path.name,
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


def decode_gzip_b64_to_path(b64: str, dest: Path) -> Path:
    raw = gzip.decompress(base64.b64decode(b64))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB cap for the source video downloaded by the worker
DOWNLOAD_TIMEOUT_SEC = 600


def make_safe_video_name(name: str) -> str:
    """Keep only a plain file name with a known video extension, so a URL-supplied name cannot escape the temp dir."""
    from src.s3_video_store import ALLOWED_VIDEO_EXTENSIONS

    base = Path(name or "").name
    stem = Path(base).stem or "source"
    ext = Path(base).suffix.lower()
    safe_stem = "".join(ch for ch in stem if ch.isalnum() or ch in "-_")[:64] or "source"
    return f"{safe_stem}{ext if ext in ALLOWED_VIDEO_EXTENSIONS else '.mp4'}"


def download_video_from_url(url: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES,
                            timeout: float = DOWNLOAD_TIMEOUT_SEC) -> Path:
    """Stream a presigned video URL to dest. Errors never include the URL, which is a bearer credential."""
    import urllib.error
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError(f"source video too large ({int(declared)} bytes, limit {max_bytes})")
            written = 0
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"source video exceeds limit of {max_bytes} bytes")
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            # S3 answers 403 for an expired or invalid presigned URL. Say so, so the failure is not mistaken for a bug.
            raise RuntimeError(
                f"source video download refused (HTTP {exc.code}); the signed URL may have expired. "
                "No local rendering was attempted."
            ) from None
        raise RuntimeError(f"source video download failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"source video download failed ({type(exc.reason).__name__})") from None
    if dest.stat().st_size == 0:
        raise ValueError("downloaded source video is empty")
    return dest


def decode_video_to_path(input_payload: dict[str, Any], dest: Path) -> Path:
    name = input_payload.get("video_file") or "source.mp4"
    url = input_payload.get("video_url")
    if url:
        return download_video_from_url(str(url), dest.parent / make_safe_video_name(name))
    b64 = input_payload.get("video_gzip_b64")
    if not b64:
        raise ValueError("Missing video_gzip_b64 in job input")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return decode_gzip_b64_to_path(b64, dest.parent / name)


def encode_file_gzip_b64(path: Path) -> str:
    return _gzip_b64(path.read_bytes())


def estimate_highlight_phase_payload_bytes(
    *,
    video_path: Path,
    job_id: str,
    segments_json: str,
    languages: str,
    summarization_model: str = "llama3.2:3b",
    compression: str = "medium",
) -> int:
    raw = video_path.read_bytes()
    b64 = _gzip_b64(raw)
    body = {
        "job_type": "highlight_phase",
        "job_id": job_id,
        "video_gzip_b64": b64,
        "video_file": video_path.name,
        "segments_json": segments_json,
        "languages": languages,
        "summarization_model": summarization_model,
        "compression": compression,
    }
    return len(json.dumps(body).encode("utf-8"))


def _reencode_video_for_runpod(
    src: Path,
    dest: Path,
    *,
    max_height: int,
    crf: int,
) -> None:
    from src.highlight_ffmpeg import resolve_ffmpeg_bin

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-vf",
        f"scale=-2:{max_height}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(f"FFmpeg could not shrink video for RunPod: {tail}")


def ensure_video_fits_runpod_payload(
    video_path: Path,
    job_id: str,
    *,
    segments_json: str,
    languages: str,
    log: LogFn | None = None,
    summarization_model: str = "llama3.2:3b",
    compression: str = "medium",
) -> Path:
    """
    Return a video path whose highlight_phase JSON payload fits RunPod's ~10 MB /run limit.
    Re-encodes to smaller H.264 when needed (topic/scene work uses this copy on the worker).
    """
    estimate_kwargs = {
        "job_id": job_id,
        "segments_json": segments_json,
        "languages": languages,
        "summarization_model": summarization_model,
        "compression": compression,
    }
    size = estimate_highlight_phase_payload_bytes(video_path=video_path, **estimate_kwargs)
    if size <= SAFE_PAYLOAD_BYTES:
        return video_path

    def step(msg: str) -> None:
        if log:
            log(msg)

    mb = size / (1024 * 1024)
    step(
        f"Video RunPod payload ~{mb:.1f} MB exceeds ~9 MB safe limit; "
        "re-encoding for serverless highlight upload…"
    )
    shrunk = video_path.parent / "source_runpod.mp4"
    for height in (480, 360, 240):
        for crf in (28, 32, 36, 40):
            _reencode_video_for_runpod(video_path, shrunk, max_height=height, crf=crf)
            size = estimate_highlight_phase_payload_bytes(video_path=shrunk, **estimate_kwargs)
            if size <= SAFE_PAYLOAD_BYTES:
                step(
                    f"Using compressed video ({height}p crf{crf}, "
                    f"payload ~{size / (1024 * 1024):.1f} MB)."
                )
                return shrunk

    mb = size / (1024 * 1024)
    raise ValueError(
        f"Video still ~{mb:.1f} MB after maximum compression for RunPod /run (~10 MB limit). "
        "Remote highlight rendering was not started; no local rendering was done. "
        "Use a shorter video, or add object-storage upload to lift the limit."
    )


def build_highlight_phase_input(
    *,
    video_path: Path,
    segments_json: str,
    languages: str,
    job_id: str,
    summarization_model: str,
    compression: str,
    log: LogFn | None = None,
    video_url: str | None = None,
) -> dict[str, Any]:
    if video_url:
        # The worker downloads the source itself, so no video bytes travel in the /run request.
        return {
            "job_type": "highlight_phase",
            "job_id": job_id,
            "video_file": make_safe_video_name(video_path.name),
            "video_url": video_url,
            "segments_json": segments_json,
            "languages": languages,
            "summarization_model": summarization_model,
            "compression": compression,
        }
    send_path = ensure_video_fits_runpod_payload(
        video_path,
        job_id,
        segments_json=segments_json,
        languages=languages,
        log=log,
        summarization_model=summarization_model,
        compression=compression,
    )
    raw = send_path.read_bytes()
    b64 = _gzip_b64(raw)
    payload_estimate = estimate_highlight_phase_payload_bytes(
        video_path=send_path,
        job_id=job_id,
        segments_json=segments_json,
        languages=languages,
        summarization_model=summarization_model,
        compression=compression,
    )
    if payload_estimate > SAFE_PAYLOAD_BYTES:
        mb = payload_estimate / (1024 * 1024)
        raise ValueError(
            f"Highlight job payload ~{mb:.1f} MB exceeds RunPod /run limit (~10 MB)."
        )
    return {
        "job_type": "highlight_phase",
        "job_id": job_id,
        "video_file": send_path.name,
        "video_gzip_b64": b64,
        "segments_json": segments_json,
        "languages": languages,
        "summarization_model": summarization_model,
        "compression": compression,
    }
