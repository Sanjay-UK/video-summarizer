"""Extract stills/clips and placeholders for highlight scenes."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import (
    HIGHLIGHT_CLIP_DURATION_SEC,
    HIGHLIGHT_FRAME_CANDIDATE_COUNT,
    HIGHLIGHT_FRAME_WINDOW_END_OFFSET_SEC,
    HIGHLIGHT_FRAME_WINDOW_START_OFFSET_SEC,
    HIGHLIGHT_MOTION_THRESHOLD,
    HIGHLIGHT_PLACEHOLDER_HEIGHT,
    HIGHLIGHT_PLACEHOLDER_WIDTH,
    HIGHLIGHT_SHARPNESS_THRESHOLD,
    HIGHLIGHT_FONT_PATH,
)
from src.topic_segmentation import TopicScene

VisualMode = Literal["still", "clip", "placeholder"]


@dataclass
class VisualSelection:
    scene_index: int
    mode: VisualMode
    source_time: float
    visual_path: Path
    sharpness_score: Optional[float] = None
    motion_score: Optional[float] = None


def _run_ffmpeg(args: list[str]) -> None:
    from src.highlight_ffmpeg import resolve_ffmpeg_bin

    result = subprocess.run(
        [resolve_ffmpeg_bin(), "-y", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg failed")


def sample_times_in_window(window_start: float, window_end: float, count: int) -> list[float]:
    if count < 1:
        return []
    if window_end <= window_start:
        return [window_start] * count
    if count == 1:
        return [window_start]
    step = (window_end - window_start) / (count - 1)
    return [window_start + i * step for i in range(count)]


def frame_window_for_scene(source_time: float) -> tuple[float, float]:
    """Sample frames from source_time+2s through source_time+5s."""
    start = source_time + HIGHLIGHT_FRAME_WINDOW_START_OFFSET_SEC
    end = source_time + HIGHLIGHT_FRAME_WINDOW_END_OFFSET_SEC
    return start, end


def extract_frame_at_time(video_path: Path, time_sec: float, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-ss",
            f"{max(0.0, time_sec):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
    )
    return output_path


def laplacian_variance(image_path: Path) -> float:
    gray = np.array(Image.open(image_path).convert("L"), dtype=np.float32)
    if gray.size == 0:
        return 0.0
    lap = (
        -4 * gray
        + np.roll(gray, 1, axis=0)
        + np.roll(gray, -1, axis=0)
        + np.roll(gray, 1, axis=1)
        + np.roll(gray, -1, axis=1)
    )
    return float(lap.var())


def detect_motion_in_window(
    video_path: Path,
    window_start: float,
    window_end: float,
    *,
    sample_count: int = 3,
) -> float:
    """Mean absolute pixel diff between consecutive samples (grayscale)."""
    times = sample_times_in_window(window_start, window_end, sample_count)
    if len(times) < 2:
        return 0.0

    tmp_dir = video_path.parent / ".highlight_motion_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    arrays: list[np.ndarray] = []

    try:
        for i, t in enumerate(times):
            frame_path = tmp_dir / f"motion_{i}.jpg"
            extract_frame_at_time(video_path, t, frame_path)
            arrays.append(
                np.array(Image.open(frame_path).convert("L").resize((320, 180)), dtype=np.float32)
            )

        diffs = [float(np.mean(np.abs(arrays[i] - arrays[i - 1]))) for i in range(1, len(arrays))]
        return sum(diffs) / len(diffs) if diffs else 0.0
    finally:
        for p in tmp_dir.glob("motion_*.jpg"):
            p.unlink(missing_ok=True)


def _load_font(size: int = 42) -> ImageFont.ImageFont:
    candidates = [
        HIGHLIGHT_FONT_PATH,
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_placeholder_png(topic_title: str, output_path: Path) -> Path:
    """Clean background + topic title (PIL PNG, not drawtext)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = HIGHLIGHT_PLACEHOLDER_WIDTH
    height = HIGHLIGHT_PLACEHOLDER_HEIGHT
    img = Image.new("RGB", (width, height), color=(28, 32, 38))
    draw = ImageDraw.Draw(img)
    font = _load_font(44)

    title = topic_title.strip() or "Topic"
    margin = 80
    max_width = width - 2 * margin
    lines = _wrap_text(draw, title, font, max_width)

    line_height = 52
    block_height = len(lines) * line_height
    y = (height - block_height) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2
        draw.text((x, y), line, fill=(240, 240, 245), font=font)
        y += line_height

    img.save(output_path, format="PNG")
    return output_path


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def extract_clip_segment(
    video_path: Path,
    center_time: float,
    duration_sec: float,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, center_time - duration_sec / 2)
    _run_ffmpeg(
        [
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration_sec:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )
    return output_path


def select_visual_for_scene(
    video_path: Path,
    scene: TopicScene,
    scene_index: int,
    output_dir: Path,
    *,
    prefer_clip: bool = False,
    clip_duration_sec: float = HIGHLIGHT_CLIP_DURATION_SEC,
    sharpness_threshold: float = HIGHLIGHT_SHARPNESS_THRESHOLD,
    motion_threshold: float = HIGHLIGHT_MOTION_THRESHOLD,
) -> VisualSelection:
    """Five frames in [start+2s, start+5s]; sharpest still or clip/placeholder."""
    output_dir.mkdir(parents=True, exist_ok=True)
    window_start, window_end = frame_window_for_scene(scene.source_time)
    times = sample_times_in_window(
        window_start,
        window_end,
        HIGHLIGHT_FRAME_CANDIDATE_COUNT,
    )

    motion_score = detect_motion_in_window(video_path, window_start, window_end)
    # Default still; clip only when motion clearly exceeds threshold (or forced).
    use_clip = prefer_clip or motion_score > motion_threshold

    frames_dir = output_dir / f"scene_{scene_index:03d}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    best_path: Optional[Path] = None
    best_score = -1.0

    for i, t in enumerate(times):
        frame_path = frames_dir / f"candidate_{i:02d}.jpg"
        try:
            extract_frame_at_time(video_path, t, frame_path)
            score = laplacian_variance(frame_path)
            if score > best_score:
                best_score = score
                best_path = frame_path
        except RuntimeError as exc:
            print(f"[WARN] Frame extract failed at t={t:.2f}: {exc}", file=sys.stderr)

    if use_clip and best_score >= sharpness_threshold:
        clip_path = output_dir / f"scene_{scene_index:03d}_clip.mp4"
        extract_clip_segment(video_path, scene.source_time, clip_duration_sec, clip_path)
        return VisualSelection(
            scene_index=scene_index,
            mode="clip",
            source_time=scene.source_time,
            visual_path=clip_path,
            sharpness_score=best_score,
            motion_score=motion_score,
        )

    if best_path is not None and best_score >= sharpness_threshold:
        still_path = output_dir / f"scene_{scene_index:03d}_still.jpg"
        still_path.write_bytes(best_path.read_bytes())
        return VisualSelection(
            scene_index=scene_index,
            mode="still",
            source_time=scene.source_time,
            visual_path=still_path,
            sharpness_score=best_score,
            motion_score=motion_score,
        )

    placeholder_path = output_dir / f"scene_{scene_index:03d}_placeholder.png"
    render_placeholder_png(scene.topic_title, placeholder_path)
    return VisualSelection(
        scene_index=scene_index,
        mode="placeholder",
        source_time=scene.source_time,
        visual_path=placeholder_path,
        sharpness_score=best_score if best_score >= 0 else None,
        motion_score=motion_score,
    )


def extract_visuals_for_scenes(
    video_path: Path,
    scenes: Sequence[TopicScene],
    output_dir: Path,
    **kwargs: Any,
) -> list[VisualSelection]:
    selections: list[VisualSelection] = []
    for idx, scene in enumerate(scenes):
        print(f"[INFO] Visual extract scene {idx + 1}/{len(scenes)} @ {scene.source_time:.1f}s")
        selections.append(
            select_visual_for_scene(
                video_path,
                scene,
                idx,
                output_dir / "visuals",
                **kwargs,
            )
        )
    return selections


def write_visual_manifest(path: Path, selections: Sequence[VisualSelection]) -> Path:
    import json

    from config import HIGHLIGHT_MOTION_THRESHOLD

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "motion_threshold": HIGHLIGHT_MOTION_THRESHOLD,
        "visuals": [
            {
                "scene_index": v.scene_index,
                "mode": v.mode,
                "source_time": v.source_time,
                "visual_path": str(v.visual_path),
                "sharpness_score": v.sharpness_score,
                "motion_score": v.motion_score,
            }
            for v in selections
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path
