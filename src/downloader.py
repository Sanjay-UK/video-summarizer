"""Video downloader with local file support and YouTube retry logic."""

import re
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union
from urllib.parse import urlparse

import yt_dlp

from src.log_safety import redact
from src.youtube_errors import (
    YOUTUBE_DOWNLOAD_FAILED,
    YouTubeDownloadError,
    classify_youtube_error,
    pick_error_text,
)

from config import DOWNLOADS_DIR

ALLOWED_NETLOCS = ("www.youtube.com", "youtube.com", "youtu.be", "www.youtu.be")

# Player clients that work around YouTube SABR / anti-bot restrictions (Aug 2025+).
# Android is tried first; combined clients provide fallbacks if one response lacks formats.
YOUTUBE_CLIENT_FALLBACKS = [
    {"extractor_args": {"youtube": {"player_client": ["android"]}}},
    {"extractor_args": {"youtube": {"player_client": ["tv", "android"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android", "web_embedded"]}}},
    {"extractor_args": {"youtube": {"player_client": ["ios"]}}},
]


def is_allowed_youtube_url(url: str) -> bool:
    """Validate that the input is a legitimate YouTube URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc not in ALLOWED_NETLOCS:
        return False
    if re.search(r'[<>"|*$\s\\;`]', url):
        return False
    return True


def sanitize_title(title: str, max_len: int = 40) -> str:
    """Turn a video title into a short, filesystem-safe folder name."""
    name = re.sub(r"[^\w\s-]", "", title.lower())
    name = re.sub(r"[\s_]+", "-", name).strip("-")
    if len(name) > max_len:
        name = name[:max_len].rstrip("-")
    return name or "video"


def extract_video_id(url: str) -> str:
    """Extract the YouTube video ID from a URL."""
    match = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([^&\n?#]+)", url)
    return match.group(1) if match else "video"


def _base_ytdlp_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }


def get_video_meta(url: str) -> Tuple[str, str, List[Tuple[int, str]]]:
    """Fetch video metadata without downloading.

    Returns (title, video_id, qualities) where qualities is a sorted list
    of (height, label) tuples.
    """
    opts = {**_base_ytdlp_opts(), "skip_download": True}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("Could not fetch video info")

    title = info.get("title") or info.get("id") or "video"
    video_id = info.get("id") or "video"

    height_to_label = {}
    for fmt in info.get("formats") or []:
        height = fmt.get("height")
        vcodec = fmt.get("vcodec") or "none"
        if height and isinstance(height, int) and vcodec != "none":
            note = fmt.get("format_note") or ""
            label = note.split(",")[0].strip() if note else f"{height}p"
            if height not in height_to_label or label.endswith("p"):
                height_to_label[height] = label

    qualities = [(h, height_to_label[h]) for h in sorted(height_to_label)]
    return title, video_id, qualities


def _build_download_opts(out_dir: Path, base_name: str, quality: int, progress_hook=None) -> dict:
    format_str = (
        f"bestvideo[height<={quality}]+bestaudio/"
        f"best[height<={quality}]/best"
    )
    return {
        "format": format_str,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / base_name) + ".%(ext)s",
        "quiet": False,
        "no_warnings": False,
        "overwrites": True,
        "socket_timeout": 30,
        "fragment_retries": 10,
        "retries": 5,
        "skip_unavailable_fragments": True,
        **({"progress_hooks": [progress_hook]} if progress_hook else {}),
    }


def _try_download(url: str, opts: dict) -> Optional[str]:
    """Attempt a yt-dlp download. Returns None on success, or the yt-dlp error text on failure."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
            return None
        except yt_dlp.utils.DownloadError as exc:
            return redact(str(exc)) or "download failed"


def _download_with_clients(
    url: str,
    out_dir: Path,
    base_name: str,
    quality: int,
    progress_hook=None,
) -> tuple[Optional[Path], Optional[str]]:
    """Try each YouTube player client. Returns (path, last_error_text); path is None when all clients fail."""
    video_path = out_dir / f"{base_name}.mp4"
    base_opts = _build_download_opts(out_dir, base_name, quality, progress_hook)
    errors: list[str] = []

    for client_args in YOUTUBE_CLIENT_FALLBACKS:
        client_name = client_args["extractor_args"]["youtube"]["player_client"]
        print(f"[INFO] Trying player client(s): {', '.join(client_name)}")
        opts = {**base_opts, **client_args}
        error = _try_download(url, opts)
        if error is None and video_path.exists():
            return video_path, None
        if error:
            errors.append(error)

        # yt-dlp may write a different extension before merge
        for candidate in out_dir.glob(f"{base_name}.*"):
            if candidate.suffix.lower() in {".mp4", ".mkv", ".webm"}:
                if candidate != video_path and candidate.suffix.lower() != ".mp4":
                    candidate.rename(video_path)
                if video_path.exists():
                    return video_path, None

    return None, pick_error_text(errors) or "download failed with all player clients"


def get_local_video(file_path: Union[Path, str]) -> Path:
    """Use a local video file directly (no download needed)."""
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Video file not found: {file_path}")

    valid_formats = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm"}
    if file_path.suffix.lower() not in valid_formats:
        raise ValueError(f"Unsupported format: {file_path.suffix}. Supported: {valid_formats}")

    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    print(f"[INFO] Using local video file: {file_path.name}")
    print(f"[INFO] Size: {file_size_mb:.1f} MB")

    return file_path


def download_youtube_video(
    url: str,
    output_dir=None,
    quality: int = 720,
    max_retries: int = 3,
    progress_hook=None,
) -> Path:
    """Download a YouTube video using yt-dlp with player-client fallbacks.

    Raises YouTubeDownloadError with a stable code. Deterministic failures (sign-in, private, unavailable) stop
    at once, without retries or waiting.
    """
    if output_dir is None:
        output_dir = DOWNLOADS_DIR
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    if not is_allowed_youtube_url(url):
        raise ValueError(f"Invalid YouTube URL: {url}")

    last_error: Optional[YouTubeDownloadError] = None

    for attempt in range(1, max_retries + 1):
        try:
            print(f"[INFO] Download attempt {attempt}/{max_retries}")
            print("[INFO] Fetching video info...")

            title, video_id, _qualities = get_video_meta(url)
            short_name = sanitize_title(title)

            print(f"[INFO] Title: {title}")
            print(f"[INFO] Video ID: {video_id}")
            print(f"[INFO] Downloading video (quality <= {quality}p)...")

            out_dir = output_dir / short_name
            out_dir.mkdir(parents=True, exist_ok=True)

            video_path, client_error = _download_with_clients(
                url, out_dir, short_name, quality, progress_hook
            )
            if video_path and video_path.exists():
                print(f"[DONE] Video downloaded: {video_path}")
                print(f"       Size: {video_path.stat().st_size / (1024 * 1024):.1f} MB")
                return video_path

            raise YouTubeDownloadError(classify_youtube_error(client_error or ""), client_error or "")

        except YouTubeDownloadError as e:
            last_error = e
        except Exception as e:  # noqa: BLE001 — classified below; never swallowed silently
            last_error = YouTubeDownloadError(classify_youtube_error(str(e)), str(e))

        print(f"[WARN] Attempt {attempt} failed: {last_error.code}")
        print(f"[WARN] Detail: {last_error.detail}")
        if not last_error.retryable:
            print("[INFO] This failure will not succeed on retry; stopping.")
            raise last_error
        if attempt < max_retries:
            wait_time = min(2 ** attempt, 30)
            print(f"[INFO] Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    raise YouTubeDownloadError(
        last_error.code if last_error else YOUTUBE_DOWNLOAD_FAILED,
        f"after {max_retries} attempts: {last_error.detail if last_error else ''}",
    )


def handle_video_source(source: Union[str, Path], output_dir=None, quality: int = 720) -> Path:
    """Handle both YouTube URLs and local files."""
    source = str(source).strip()

    if "youtube.com" in source or "youtu.be" in source:
        return download_youtube_video(source, output_dir, quality)

    if "vimeo.com" in source:
        raise NotImplementedError(
            "Vimeo support coming soon - please use a local file or YouTube URL"
        )

    return get_local_video(source)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download YouTube video")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--output", "-o", help="Output directory")
    parser.add_argument("--quality", "-q", type=int, default=720, help="Max video quality in pixels")
    args = parser.parse_args()

    path = download_youtube_video(args.url, args.output, args.quality)
    print(f"\nVideo saved to: {path}")
