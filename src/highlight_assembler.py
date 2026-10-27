"""Assemble normalized scene clips into highlight_en.mp4."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional

from config import (
    HIGHLIGHT_AUDIO_RATE,
    HIGHLIGHT_FPS,
    HIGHLIGHT_OUTPUT_HEIGHT,
    HIGHLIGHT_OUTPUT_WIDTH,
    PROJECT_DIR,
)
from src.locales import FONTS_DIR
from src.log_safety import safe_exc
from src.highlight_ffmpeg import escape_ffmpeg_filter_path, probe_duration_seconds, run_ffmpeg
from src.highlight_timing import scene_display_duration
from src.visual_extractor import VisualSelection

VisualMode = Literal["still", "clip", "placeholder"]


def _video_scale_filter() -> str:
    w, h, fps = HIGHLIGHT_OUTPUT_WIDTH, HIGHLIGHT_OUTPUT_HEIGHT, HIGHLIGHT_FPS
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )


def _fonts_dir_for_ffmpeg() -> Path:
    """Bundled highlight fonts for libass. Used read-only: nothing is copied, so concurrent jobs cannot race.

    The worker image copies static/fonts to this path. If it is missing, libass draws boxes for non-Latin text,
    and the glyph check in mux_scene reports that as a failed highlight instead of shipping it.
    """
    if not (FONTS_DIR.is_dir() and any(FONTS_DIR.glob("*.ttf"))):
        print(f"[WARN] Bundled caption fonts not found at {FONTS_DIR}; non-Latin subtitles may fail.")
    return FONTS_DIR


def _subtitles_filter(ass_path: Path) -> str:
    escaped = escape_ffmpeg_filter_path(ass_path)
    fonts_dir = escape_ffmpeg_filter_path(_fonts_dir_for_ffmpeg())
    return f"subtitles=filename={escaped}:fontsdir={fonts_dir}"


def pad_narration_audio(input_audio: Path, display_duration: float, output_audio: Path) -> Path:
    output_audio.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            str(input_audio),
            "-af",
            f"apad=whole_dur={display_duration:.3f}",
            "-t",
            f"{display_duration:.3f}",
            "-c:a",
            "aac",
            "-ar",
            str(HIGHLIGHT_AUDIO_RATE),
            "-b:a",
            "128k",
            str(output_audio),
        ]
    )
    return output_audio


def _build_video_from_still(
    image_path: Path,
    display_duration: float,
    work_dir: Path,
    tag: str,
) -> Path:
    out = work_dir / f"{tag}_visual.mp4"
    run_ffmpeg(
        [
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-t",
            f"{display_duration:.3f}",
            "-vf",
            _video_scale_filter(),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return out


def _normalize_muted_clip(clip_path: Path, work_dir: Path, tag: str) -> Path:
    out = work_dir / f"{tag}_norm_clip.mp4"
    run_ffmpeg(
        [
            "-i",
            str(clip_path),
            "-vf",
            _video_scale_filter(),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return out


def _trim_video(input_video: Path, duration: float, output: Path) -> Path:
    run_ffmpeg(
        [
            "-i",
            str(input_video),
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "copy",
            str(output),
        ]
    )
    return output


def _extract_last_frame(video_path: Path, frame_path: Path) -> Path:
    run_ffmpeg(
        [
            "-sseof",
            "-0.05",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(frame_path),
        ]
    )
    return frame_path


def _build_clip_plus_freeze(
    clip_path: Path,
    display_duration: float,
    work_dir: Path,
    tag: str,
) -> Path:
    norm = _normalize_muted_clip(clip_path, work_dir, tag)
    clip_dur = min(probe_duration_seconds(norm), display_duration)
    if clip_dur <= 0:
        clip_dur = 0.1

    if clip_dur >= display_duration - 0.01:
        return _trim_video(norm, display_duration, work_dir / f"{tag}_visual.mp4")

    part1 = work_dir / f"{tag}_part1.mp4"
    _trim_video(norm, clip_dur, part1)
    freeze_dur = display_duration - clip_dur
    last_frame = work_dir / f"{tag}_last.jpg"
    _extract_last_frame(part1, last_frame)
    part2 = _build_video_from_still(last_frame, freeze_dur, work_dir, f"{tag}_freeze")

    concat_list = work_dir / f"{tag}_concat.txt"
    concat_list.write_text(
        f"file '{part1.resolve()}'\nfile '{part2.resolve()}'\n",
        encoding="utf-8",
    )
    out = work_dir / f"{tag}_visual.mp4"
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return out


def _ass_path_for_ffmpeg(ass_path: Path) -> Path:
    """Copy ASS to a space-free temp path so ffmpeg subtitles= parses reliably."""
    tmp = Path(tempfile.gettempdir()) / f"highlight_{ass_path.parent.name}_{ass_path.name}"
    shutil.copy2(ass_path, tmp)
    return tmp


class SubtitleFontError(RuntimeError):
    """The subtitle font cannot draw the text. The highlight for this language is failed, not rendered with boxes."""


MISSING_GLYPH_MARKER = "failed to find any fallback with glyph"


def subtitle_glyphs_missing(ffmpeg_stderr: str) -> bool:
    """True when libass reports that no available font has a glyph for some character."""
    return MISSING_GLYPH_MARKER in (ffmpeg_stderr or "").lower()


def mux_scene(
    visual_video: Path,
    padded_audio: Path,
    ass_path: Path,
    display_duration: float,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_for_filter = _ass_path_for_ffmpeg(ass_path)
    vf = f"{_video_scale_filter()},{_subtitles_filter(ass_for_filter)}"
    stderr = run_ffmpeg(
        [
            "-i",
            str(visual_video),
            "-i",
            str(padded_audio),
            "-t",
            f"{display_duration:.3f}",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            str(HIGHLIGHT_AUDIO_RATE),
            "-b:a",
            "128k",
            "-shortest",
            str(output_path),
        ]
    )
    if subtitle_glyphs_missing(stderr):
        # Without the glyphs the subtitles would be drawn as empty boxes. Fail instead of shipping that.
        raise SubtitleFontError(
            "Subtitles could not be drawn: the caption font has no glyphs for this text. "
            "The worker needs the bundled fonts in static/fonts."
        )
    return output_path


def build_scene_visual_only(
    visual: VisualSelection,
    display_duration: float,
    work_dir: Path,
    output_path: Path,
) -> Path:
    """Muted scene video (shared across all highlight languages)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    tag = f"scene_{visual.scene_index:03d}"
    mode: VisualMode = visual.mode  # type: ignore[assignment]
    if mode == "clip":
        visual_video = _build_clip_plus_freeze(visual.visual_path, display_duration, work_dir, tag)
    else:
        visual_video = _build_video_from_still(
            visual.visual_path,
            display_duration,
            work_dir,
            tag,
        )
    if visual_video.resolve() != output_path.resolve():
        shutil.copy2(visual_video, output_path)
    return output_path


def build_scene_video(
    visual: VisualSelection,
    narration_audio: Path,
    ass_path: Path,
    narration_duration: float,
    work_dir: Path,
    *,
    display_duration: float | None = None,
) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    tag = f"scene_{visual.scene_index:03d}"
    dur = display_duration if display_duration is not None else scene_display_duration(narration_duration)

    padded_audio = work_dir / f"{tag}_audio.aac"
    pad_narration_audio(narration_audio, dur, padded_audio)

    visual_out = work_dir / f"{tag}_visual.mp4"
    build_scene_visual_only(visual, dur, work_dir, visual_out)

    scene_out = work_dir / f"{tag}_final.mp4"
    return mux_scene(visual_out, padded_audio, ass_path, dur, scene_out)


# The English narration track kept next to highlight_en.mp4. The audio download serves this file.
HIGHLIGHT_NARRATION_FILE = "highlight_narration_en.mp3"
ENGLISH_HIGHLIGHT_VIDEO = "highlight_en.mp4"


def extract_narration_track(video_path: Path, output_mp3: Path) -> Path:
    """The narration exactly as it plays in the finished highlight video, as an MP3. No speech is synthesized again.

    The MP3 is written under a temporary name and moved into place only when it is complete and not empty.
    """
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    partial = output_mp3.with_name(output_mp3.stem + ".partial.mp3")
    try:
        run_ffmpeg(["-i", str(video_path), "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(partial)])
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("the extracted narration is empty")
        os.replace(partial, output_mp3)
    except Exception:
        partial.unlink(missing_ok=True)  # never leave a partial file behind
        raise
    return output_mp3


def ensure_narration_track(english_dir: Path) -> Optional[Path]:
    """The English narration track for the English highlight video in english_dir, made from that video if needed.

    Returns None when there is no usable video, or the track cannot be made. A track older than its video is rebuilt,
    so a stale narration is never returned. No speech is synthesized here.
    """
    video = english_dir / ENGLISH_HIGHLIGHT_VIDEO
    track = english_dir / HIGHLIGHT_NARRATION_FILE
    if not video.is_file() or video.stat().st_size == 0:
        track.unlink(missing_ok=True)
        return None
    if track.is_file() and track.stat().st_size > 0 and track.stat().st_mtime >= video.stat().st_mtime:
        return track
    try:
        return extract_narration_track(video, track)
    except Exception as exc:  # noqa: BLE001 — the video is still good; the audio download reports the gap
        print(f"[highlight] English narration track could not be made: {safe_exc(exc)}", file=sys.stderr)
        track.unlink(missing_ok=True)
        return None


def concat_scenes(scene_paths: list[Path], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_path.parent / "concat_scenes.txt"
    lines = []
    for p in scene_paths:
        resolved = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{resolved}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            str(HIGHLIGHT_AUDIO_RATE),
            "-b:a",
            "128k",
            str(output_path),
        ]
    )
    return output_path
