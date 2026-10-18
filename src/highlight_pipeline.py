"""End-to-end highlight video pipeline (English, opt-in)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.highlight_assembler import build_scene_video, concat_scenes
from src.highlight_ffmpeg import require_ffmpeg_subtitles_filter
from src.highlight_captions import build_scene_ass
from src.highlight_timing import scene_display_duration
from src.highlight_tts import generate_scene_narration
from src.job_video import copy_source_video_to_job, job_source_video_path
from src.narration_rewrite import rewrite_for_speech
from src.topic_segmentation import TopicScene, run_topic_segmentation, write_highlight_scenes
from src.visual_extractor import extract_visuals_for_scenes, write_visual_manifest


def resolve_job_video(video_path: Path, job_output_dir: Path) -> Path:
    """Ensure source.mp4 exists in the job directory."""
    staged = job_source_video_path(job_output_dir)
    if staged.is_file():
        return staged
    return copy_source_video_to_job(video_path, job_output_dir)


def run_highlight_pipeline(
    video_path: Path,
    segments: list[dict[str, Any]],
    job_output_dir: Path,
    *,
    model: Optional[str] = None,
    max_scenes: int = 0,
) -> Path:
    require_ffmpeg_subtitles_filter()

    job_output_dir = Path(job_output_dir)
    highlight_root = job_output_dir / "highlights" / "en"
    highlight_root.mkdir(parents=True, exist_ok=True)

    staged_video = resolve_job_video(Path(video_path), job_output_dir)

    scenes = run_topic_segmentation(segments, model=model)
    if max_scenes > 0:
        scenes = scenes[:max_scenes]

    write_highlight_scenes(job_output_dir / "highlight_scenes.json", scenes)

    visuals = extract_visuals_for_scenes(
        staged_video,
        scenes,
        job_output_dir,
    )
    write_visual_manifest(job_output_dir / "visual_manifest.json", visuals)

    scene_videos: list[Path] = []
    narration_manifest: list[dict[str, Any]] = []

    for scene, visual in zip(scenes, visuals):
        scene_dir = highlight_root / f"scene_{visual.scene_index:03d}"
        scene_dir.mkdir(parents=True, exist_ok=True)

        spoken = rewrite_for_speech(scene.summary_text, scene.topic_title, model=model)
        (scene_dir / "narration.txt").write_text(spoken, encoding="utf-8")

        tts = generate_scene_narration(spoken, scene_dir / "narration.mp3")
        display_sec = scene_display_duration(tts.duration_seconds)
        ass_path = build_scene_ass(tts, display_sec, scene_dir / "captions.ass")

        final_scene = build_scene_video(
            visual,
            tts.audio_path,
            ass_path,
            tts.duration_seconds,
            scene_dir / "build",
        )
        scene_videos.append(final_scene)

        narration_manifest.append(
            {
                "scene_index": visual.scene_index,
                "topic_title": scene.topic_title,
                "spoken_text": spoken,
                "tts_boundary_kind": tts.boundary_kind,
                "narration_seconds": tts.duration_seconds,
                "display_seconds": display_sec,
            }
        )

    with open(highlight_root / "narration_manifest.json", "w", encoding="utf-8") as f:
        json.dump({"scenes": narration_manifest}, f, indent=2)

    output_mp4 = highlight_root / "highlight_en.mp4"
    concat_scenes(scene_videos, output_mp4)
    return output_mp4
