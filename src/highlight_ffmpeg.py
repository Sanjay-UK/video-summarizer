"""FFmpeg helpers for highlight assembly."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_FFMPEG_SUBTITLES_HELP = (
    "Highlight video requires an FFmpeg build with the libass 'subtitles' filter.\n"
    "macOS: brew install ffmpeg-full  (add /opt/homebrew/opt/ffmpeg-full/bin to PATH)\n"
    "Windows: install a full build (e.g. gyan.dev ffmpeg-full or BtbN FFmpeg-Builds),\n"
    "  then set FFMPEG_PATH to the folder containing ffmpeg.exe, e.g.\n"
    "  set FFMPEG_PATH=C:\\ffmpeg\\bin\\ffmpeg.exe\n"
    "Linux: install ffmpeg package that includes libass (often ffmpeg or libavfilter-extra).\n"
    "Verify: ffmpeg -filters 2>&1 | findstr subtitles   (Windows)\n"
    "        ffmpeg -filters 2>&1 | grep subtitles       (macOS/Linux)"
)


@lru_cache(maxsize=1)
def resolve_ffmpeg_bin() -> str:
    """FFMPEG_PATH env → PATH → platform defaults."""
    candidates: list[str] = []
    env_path = os.environ.get("FFMPEG_PATH", "").strip()
    if env_path:
        candidates.append(env_path)

    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)

    system = platform.system()
    if system == "Darwin":
        candidates.extend(
            [
                "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
                "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
            ]
        )
    elif system == "Windows":
        candidates.extend(
            [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
            ]
        )

    candidates.append("ffmpeg")

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return str(path)
    return "ffmpeg"


@lru_cache(maxsize=1)
def resolve_ffprobe_bin() -> str:
    ffmpeg = Path(resolve_ffmpeg_bin())
    sibling = ffmpeg.parent / ("ffprobe.exe" if platform.system() == "Windows" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    which = shutil.which("ffprobe")
    return which if which else "ffprobe"


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        [resolve_ffmpeg_bin(), "-y", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg failed")


def probe_duration_seconds(media_path: Path) -> float:
    result = subprocess.run(
        [
            resolve_ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return float(result.stdout.strip())


def ffmpeg_has_subtitles_filter() -> bool:
    try:
        result = subprocess.run(
            [resolve_ffmpeg_bin(), "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return re.search(r"^\s*\.\.\s+subtitles\s+", result.stdout, re.MULTILINE) is not None


def require_ffmpeg_subtitles_filter() -> None:
    if not ffmpeg_has_subtitles_filter():
        raise RuntimeError(_FFMPEG_SUBTITLES_HELP)


def escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape paths for ffmpeg subtitles=filename= (Windows drive colons, spaces)."""
    resolved = path.resolve()
    s = os.fsdecode(resolved)

    if platform.system() == "Windows":
        s = s.replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", s):
            s = s[0] + "\\:" + s[2:]
    else:
        s = s.replace("\\", "/")

    for char in ("'", ",", "[", "]", "(", ")", " "):
        s = s.replace(char, f"\\{char}")

    if platform.system() != "Windows":
        s = s.replace(":", "\\:")

    return s


def log_ffmpeg_subtitles_status_on_startup() -> None:
    """Log once at app startup; does not block text-only jobs."""
    if ffmpeg_has_subtitles_filter():
        print(f"[INFO] FFmpeg subtitles filter OK ({resolve_ffmpeg_bin()})")
    else:
        print(f"[WARN] FFmpeg at {resolve_ffmpeg_bin()} lacks 'subtitles' filter.")
        print("[WARN] Text summarization works; highlight video will fail until ffmpeg-full is installed.")
        print(_FFMPEG_SUBTITLES_HELP)
