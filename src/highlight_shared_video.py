"""One shared muted video track per scene; languages differ only in audio + captions."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from src.highlight_assembler import build_scene_visual_only, mux_scene, pad_narration_audio
from src.highlight_timing import scene_display_duration
from src.highlight_tts import TtsResult, generate_scene_narration
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
            scene_dir / MASTER_NARRATION_AUDIO,
            locale=locale_en,
        )
        save_master_narration(scene_dir, str(spoken).strip(), tts)
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


MASTER_NARRATION_META = "master_narration.json"
MASTER_NARRATION_AUDIO = "master_narration.mp3"


def save_master_narration(scene_dir: Path, text: str, tts: TtsResult) -> None:
    """Record the English narration made for the timing pass, so the English render can reuse that exact audio.

    The text is stored with it. A reuse is allowed only when the text is unchanged.
    """
    payload = {
        "text": text,
        "duration_seconds": tts.duration_seconds,
        "boundary_kind": tts.boundary_kind,
        "boundaries": tts.boundaries,
        "voice_used": tts.voice_used,
    }
    (scene_dir / MASTER_NARRATION_META).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_master_narration(job_output_dir: Path, scene_index: int, text: str) -> Optional[TtsResult]:
    """The saved English narration for a scene, or None when it is missing, unreadable, or for different text.

    None means the caller must synthesize the narration itself. A stale file is never returned.
    """
    scene_dir = shared_highlight_dir(job_output_dir) / f"scene_{scene_index:03d}"
    meta_path = scene_dir / MASTER_NARRATION_META
    audio_path = scene_dir / MASTER_NARRATION_AUDIO
    if not meta_path.is_file() or not audio_path.is_file():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        duration = float(payload["duration_seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if str(payload.get("text", "")).strip() != str(text).strip():
        return None
    return TtsResult(
        audio_path=audio_path,
        spoken_text=str(text).strip(),
        duration_seconds=duration,
        boundary_kind=payload.get("boundary_kind", "none"),
        boundaries=list(payload.get("boundaries") or []),
        voice_used=str(payload.get("voice_used") or ""),
    )


def copy_master_narration(master: TtsResult, output_path: Path) -> TtsResult:
    """Copy the shared English narration into a language folder. Same audio, same timing, no new synthesis."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(master.audio_path, output_path)
    return TtsResult(
        audio_path=output_path,
        spoken_text=master.spoken_text,
        duration_seconds=master.duration_seconds,
        boundary_kind=master.boundary_kind,
        boundaries=list(master.boundaries),
        voice_used=master.voice_used,
    )


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
