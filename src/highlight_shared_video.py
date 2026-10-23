"""One shared muted video track per scene; languages differ only in audio + captions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.highlight_assembler import build_scene_visual_only, mux_scene, pad_narration_audio
from src.highlight_timing import scene_display_duration
from src.highlight_tts import generate_scene_narration
from src.locales import get_locale
from src.visual_extractor import VisualSelection


def shared_highlight_dir(job_output_dir: Path) -> Path:
    return Path(job_output_dir) / "highlights" / "_shared"


def scene_timing_manifest_path(job_output_dir: Path) -> Path:
    return shared_highlight_dir(job_output_dir) / "scene_timing.json"


def read_scene_timing_manifest(job_output_dir: Path) -> list[dict[str, Any]]:
    path = scene_timing_manifest_path(job_output_dir)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("scenes", []))


def shared_scene_visual_path(job_output_dir: Path, scene_index: int) -> Path:
    return shared_highlight_dir(job_output_dir) / f"scene_{scene_index:03d}" / "visual.mp4"


def ensure_shared_scene_visuals(
    job_output_dir: Path,
    visuals: list[VisualSelection],
    english_spoken_texts: list[str],
    *,
    rebuild: bool = False,
) -> list[dict[str, Any]]:
    """
    Build master scene timing from English TTS and encode each scene's video once (no audio).
    """
    job_output_dir = Path(job_output_dir)
    shared = shared_highlight_dir(job_output_dir)
    shared.mkdir(parents=True, exist_ok=True)
    manifest_path = scene_timing_manifest_path(job_output_dir)

    if not rebuild and manifest_path.is_file():
        rows = read_scene_timing_manifest(job_output_dir)
        if rows and all(shared_scene_visual_path(job_output_dir, int(r["scene_index"])).is_file() for r in rows):
            return rows

    locale_en = get_locale("en")
    manifest_scenes: list[dict[str, Any]] = []

    for visual, spoken in zip(visuals, english_spoken_texts):
        if not spoken or not str(spoken).strip():
            continue
        scene_dir = shared / f"scene_{visual.scene_index:03d}"
        scene_dir.mkdir(parents=True, exist_ok=True)
        tts = generate_scene_narration(
            str(spoken).strip(),
            scene_dir / "master_narration.mp3",
            locale=locale_en,
        )
        display_sec = scene_display_duration(tts.duration_seconds)
        visual_out = scene_dir / "visual.mp4"
        build_scene_visual_only(visual, display_sec, scene_dir, visual_out)
        manifest_scenes.append(
            {
                "scene_index": visual.scene_index,
                "display_seconds": display_sec,
                "master_narration_seconds": tts.duration_seconds,
                "visual_path": str(visual_out.relative_to(job_output_dir)),
            }
        )

    payload = {"scenes": manifest_scenes}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_scenes


def mux_language_scene(
    job_output_dir: Path,
    scene_index: int,
    display_seconds: float,
    narration_audio: Path,
    ass_path: Path,
    output_path: Path,
) -> Path:
    visual = shared_scene_visual_path(job_output_dir, scene_index)
    if not visual.is_file():
        raise FileNotFoundError(f"Missing shared scene visual: {visual}")
    work = output_path.parent
    work.mkdir(parents=True, exist_ok=True)
    padded = work / f"scene_{scene_index:03d}_audio.aac"
    pad_narration_audio(narration_audio, display_seconds, padded)
    return mux_scene(visual, padded, ass_path, display_seconds, output_path)
