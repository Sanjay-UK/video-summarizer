"""Stage source video into per-job output directory (copy, not symlink)."""

from __future__ import annotations

import shutil
from pathlib import Path

JOB_SOURCE_BASENAME = "source.mp4"


def copy_source_video_to_job(source_video: Path, job_output_dir: Path) -> Path:
    """Copy the source video into output/<job_id>/source.mp4."""
    source_video = Path(source_video).resolve()
    if not source_video.is_file():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    job_output_dir = Path(job_output_dir)
    job_output_dir.mkdir(parents=True, exist_ok=True)
    dest = job_output_dir / JOB_SOURCE_BASENAME

    if source_video.resolve() == dest.resolve():
        return dest

    shutil.copy2(source_video, dest)
    return dest


def job_source_video_path(job_output_dir: Path) -> Path:
    return Path(job_output_dir) / JOB_SOURCE_BASENAME
