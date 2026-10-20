#!/usr/bin/env python3
"""
Standalone video/audio → transcript utility.

Use as a library:
    from download_transcript import download_transcript, TranscriptResult

    result = download_transcript("https://www.youtube.com/watch?v=...", output_dir="./out")
    print(result.text)
    print(result.transcript_path)

Or from the shell:
    python download_transcript.py /path/to/lecture.mp4 -o ./transcripts
    python download_transcript.py "https://youtu.be/xxxx" --model base

Dependencies: ffmpeg, yt-dlp, faster-whisper (pip install yt-dlp faster-whisper).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union
from urllib.parse import urlparse

DEFAULT_WHISPER_MODEL = "base"
DEFAULT_WHISPER_DEVICE = "cpu"
DEFAULT_YOUTUBE_QUALITY = 360

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

YOUTUBE_NETLOCS = frozenset(
    {"www.youtube.com", "youtube.com", "youtu.be", "www.youtu.be", "m.youtube.com"}
)


@dataclass(frozen=True)
class TranscriptResult:
    """Paths and payload from a completed transcription."""

    text: str
    segments: List[dict]
    transcript_path: Path
    segments_path: Path
    source: str
    work_dir: Path


def is_youtube_url(source: str) -> bool:
    if not source or not isinstance(source, str):
        return False
    source = source.strip()
    if re.search(r'[<>"|*$\s\\;`]', source):
        return False
    try:
        parsed = urlparse(source)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.netloc in YOUTUBE_NETLOCS


def _resolve_local_media(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    else:
        path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Media file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    suffix = path.suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError(
            f"Unsupported extension {suffix!r}. "
            f"Use one of: {', '.join(sorted(MEDIA_EXTENSIONS))}"
        )
    return path


def _sanitize_dir(output_dir: Optional[Union[str, Path]]) -> Path:
    if output_dir is None:
        return Path(tempfile.mkdtemp(prefix="transcript_"))
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not out.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {out}")
    return out


def download_youtube_to_dir(url: str, dest_dir: Path, quality: int = DEFAULT_YOUTUBE_QUALITY) -> Path:
    """Download a YouTube video into dest_dir; return path to the video file."""
    if not is_youtube_url(url):
        raise ValueError("Invalid or unsupported YouTube URL")

    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is required for URLs. pip install yt-dlp") from exc

    dest_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest_dir / "source") + ".%(ext)s"
    format_str = (
        f"bestvideo[height<={quality}]+bestaudio/"
        f"best[height<={quality}]/best"
    )
    opts: dict[str, Any] = {
        "format": format_str,
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url.strip(), download=True)

    if info is None:
        raise RuntimeError("YouTube download failed: no video info")

    requested = info.get("requested_downloads") or []
    if requested:
        filepath = requested[0].get("filepath")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    ext = info.get("ext") or "mp4"
    candidate = dest_dir / f"source.{ext}"
    if candidate.exists():
        return candidate

    for p in dest_dir.glob("source.*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
            return p

    raise RuntimeError("YouTube download finished but output file was not found")


def extract_audio_ffmpeg(video_path: Path, output_dir: Path, audio_name: str = "audio.mp3") -> Path:
    """Extract audio track with ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (install ffmpeg)")

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / audio_name

    cmd = [
        "ffmpeg",
        "-i",
        str(video_path),
        "-q:a",
        "5",
        "-map",
        "a",
        "-y",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not audio_path.exists():
        stderr = (result.stderr or "").strip()[-2000:]
        raise RuntimeError(f"ffmpeg failed: {stderr or 'unknown error'}")

    return audio_path


def transcribe_file(
    audio_path: Path,
    output_dir: Path,
    *,
    model: str = DEFAULT_WHISPER_MODEL,
    device: str = DEFAULT_WHISPER_DEVICE,
    language: Optional[str] = "en",
) -> tuple[str, List[dict], Path, Path]:
    """Run faster-whisper and write transcript.txt + segments.json."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required. pip install faster-whisper") from exc

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    whisper = WhisperModel(model, device=device, compute_type="auto")

    segments_iter, _info = whisper.transcribe(
        str(audio_path),
        language=language,
        condition_on_previous_text=False,
    )
    segments = [
        {"start": seg.start, "end": seg.end, "text": seg.text.strip()}
        for seg in segments_iter
    ]
    full_text = " ".join(seg["text"] for seg in segments if seg["text"])

    transcript_path = output_dir / "transcript.txt"
    segments_path = output_dir / "segments.json"

    transcript_path.write_text(full_text, encoding="utf-8")
    segments_path.write_text(
        json.dumps(segments, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return full_text, segments, transcript_path, segments_path


def download_transcript(
    source: str,
    output_dir: Optional[Union[str, Path]] = None,
    *,
    model: str = DEFAULT_WHISPER_MODEL,
    device: str = DEFAULT_WHISPER_DEVICE,
    language: Optional[str] = "en",
    youtube_quality: int = DEFAULT_YOUTUBE_QUALITY,
    keep_work_dir: bool = False,
) -> TranscriptResult:
    """
    Transcribe a YouTube URL or local video/audio file.

    Args:
        source: YouTube URL or path to a media file.
        output_dir: Where to write transcript.txt and segments.json.
            If omitted, uses a temporary directory (set keep_work_dir=True to retain it).
        model: faster-whisper model size/name (e.g. base, small).
        device: cpu or cuda.
        language: ISO language code passed to Whisper, or None for auto-detect.
        youtube_quality: Max video height when downloading from YouTube.
        keep_work_dir: When output_dir is omitted, do not delete the temp folder on exit
            (always True when you pass output_dir explicitly).

    Returns:
        TranscriptResult with text, segments, and output file paths.
    """
    source = source.strip()
    if not source:
        raise ValueError("source must be a URL or file path")

    user_output = output_dir is not None
    work_dir = _sanitize_dir(output_dir)
    temp_root: Optional[Path] = None if user_output else work_dir

    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None

    try:
        if is_youtube_url(source):
            video_path = download_youtube_to_dir(source, work_dir, quality=youtube_quality)
        else:
            local = _resolve_local_media(source)
            if local.suffix.lower() in AUDIO_EXTENSIONS:
                audio_path = local
            else:
                video_path = local

        if audio_path is None:
            if video_path is None:
                raise RuntimeError("No media to transcribe")
            audio_path = extract_audio_ffmpeg(video_path, work_dir)

        text, segments, transcript_path, segments_path = transcribe_file(
            audio_path,
            work_dir,
            model=model,
            device=device,
            language=language,
        )

        return TranscriptResult(
            text=text,
            segments=segments,
            transcript_path=transcript_path,
            segments_path=segments_path,
            source=source,
            work_dir=work_dir,
        )
    except Exception:
        if temp_root is not None and not keep_work_dir:
            shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Download or transcribe video/audio to transcript.txt + segments.json",
    )
    parser.add_argument(
        "source",
        help="YouTube URL or path to a local video/audio file",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_dir",
        help="Output directory (default: temporary folder, printed at end)",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=DEFAULT_WHISPER_MODEL,
        help=f"faster-whisper model (default: {DEFAULT_WHISPER_MODEL})",
    )
    parser.add_argument(
        "--device",
        default=DEFAULT_WHISPER_DEVICE,
        choices=("cpu", "cuda"),
        help="Whisper device",
    )
    parser.add_argument(
        "--language",
        default="en",
        help='Language code for Whisper, or "auto" for detection',
    )
    parser.add_argument(
        "--youtube-quality",
        type=int,
        default=DEFAULT_YOUTUBE_QUALITY,
        metavar="HEIGHT",
        help="Max YouTube video height when downloading",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="When -o is omitted, keep the temporary work directory",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    lang = None if args.language.lower() == "auto" else args.language

    try:
        result = download_transcript(
            args.source,
            args.output_dir,
            model=args.model,
            device=args.device,
            language=lang,
            youtube_quality=args.youtube_quality,
            keep_work_dir=args.keep_temp or args.output_dir is not None,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Transcript: {result.transcript_path} ({len(result.text)} chars)")
    print(f"Segments:   {result.segments_path} ({len(result.segments)} segments)")
    if args.output_dir is None:
        print(f"Work dir:   {result.work_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
