"""Assemble normalized scene clips into highlight_en.mp4."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Literal

from config import (
    HIGHLIGHT_AUDIO_RATE,
    HIGHLIGHT_FPS,
    HIGHLIGHT_OUTPUT_HEIGHT,
    HIGHLIGHT_OUTPUT_WIDTH,
    PROJECT_DIR,
)
from src.locales import FONTS_DIR
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
    """Stage bundled highlight fonts for libass (all scripts in static/fonts)."""
    dest = Path(tempfile.gettempdir()) / "highlight_fonts"
    dest.mkdir(parents=True, exist_ok=True)
    sources = [FONTS_DIR, PROJECT_DIR / "static" / "fonts"]
    seen: set[str] = set()
    for src_dir in sources:
        if not src_dir.is_dir():
            continue
        for font in src_dir.glob("*.ttf"):
            if font.name in seen:
                continue
            seen.add(font.name)
            shutil.copy2(font, dest / font.name)
    return dest


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
    run_ffmpeg(
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
    return output_path


def build_scene_video(
    visual: VisualSelection,
    narration_audio: Path,
    ass_path: Path,
    narration_duration: float,
    work_dir: Path,
) -> Path:
    display_duration = scene_display_duration(narration_duration)
    work_dir.mkdir(parents=True, exist_ok=True)
    tag = f"scene_{visual.scene_index:03d}"

    padded_audio = work_dir / f"{tag}_audio.aac"
    pad_narration_audio(narration_audio, display_duration, padded_audio)

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

    scene_out = work_dir / f"{tag}_final.mp4"
    return mux_scene(visual_video, padded_audio, ass_path, display_duration, scene_out)


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
