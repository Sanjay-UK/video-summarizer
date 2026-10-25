"""Source-video storage in S3 for RunPod highlight jobs.

Security model: this app runs on the user's own machine, which is the trusted backend. AWS credentials come
only from the standard AWS credential chain on that machine (a named profile via AWS_PROFILE/VIDEO_S3_PROFILE,
or environment variables). They are never stored in the repo, the browser, or the RunPod request. The worker
receives only a presigned GET URL for one object, which expires (default 3600 s).

Off by default: nothing here runs unless VIDEO_S3_ENABLED is true. Local mode never touches S3.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_BUCKET = "video-summarizer-757374023708-ap-south-1-an"
DEFAULT_REGION = "ap-south-1"
DEFAULT_PROFILE = "video-summarizer"
DEFAULT_PRESIGN_EXPIRES = 3600
ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv"})
S3_RECORD_NAME = "s3_video.json"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def s3_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """True only when BOTH flags are set. Default is off, so local mode and existing deployments are unchanged.

    VIDEO_S3_ENABLED turns storage on. VIDEO_S3_WORKER_URL_SUPPORT must be set only after a worker image that
    accepts video_url jobs is deployed. Without it, URL jobs would fail on the current worker, so the second
    flag stops a single setting from breaking remote highlights.
    """
    env = os.environ if env is None else env
    return _truthy(env.get("VIDEO_S3_ENABLED", "")) and _truthy(env.get("VIDEO_S3_WORKER_URL_SUPPORT", ""))


def settings_from_env(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    expires_raw = env.get("VIDEO_S3_PRESIGN_EXPIRES", "").strip()
    try:
        expires = int(expires_raw) if expires_raw else DEFAULT_PRESIGN_EXPIRES
    except ValueError:
        expires = DEFAULT_PRESIGN_EXPIRES
    expires = max(60, min(expires, 7 * 24 * 3600))  # S3 presigned URLs allow at most 7 days
    return {
        "bucket": env.get("VIDEO_S3_BUCKET", "").strip() or DEFAULT_BUCKET,
        "region": env.get("VIDEO_S3_REGION", "").strip() or DEFAULT_REGION,
        # Always a named profile. Never fall back to the default chain: on this machine the 'default' profile
        # belongs to another project. AWS_PROFILE is deliberately ignored.
        "profile": env.get("VIDEO_S3_PROFILE", "").strip() or DEFAULT_PROFILE,
        "presign_expires": expires,
    }


def make_object_key(original_name: str) -> str:
    """Unpredictable root-level key: a UUID plus the original extension when it is a known video type."""
    ext = Path(original_name or "").suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        ext = ""
    return f"{uuid.uuid4()}{ext}"


class VideoStore:
    """Thin wrapper around one S3 bucket. The boto3 client is created lazily, so tests can inject a fake."""

    def __init__(self, settings: dict[str, Any], client: Any = None):
        self.settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # imported here so local mode never needs AWS libraries

            session = boto3.Session(profile_name=self.settings.get("profile"))
            self._client = session.client("s3", region_name=self.settings["region"])
        return self._client

    @property
    def bucket(self) -> str:
        return self.settings["bucket"]

    def upload_source(self, path: Path, key: str) -> None:
        """Upload with server-side encryption (SSE-S3, AES256). Keys are unique per job, so nothing is overwritten."""
        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": "video/mp4"},
        )

    def presign_get(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(self.settings["presign_expires"]),
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def write_record(job_dir: Path, record: dict[str, Any]) -> None:
    """Job metadata only: bucket, key, and timestamps. Never the presigned URL or any credential."""
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / S3_RECORD_NAME).write_text(json.dumps(record, indent=2), encoding="utf-8")


def read_record(job_dir: Path) -> dict[str, Any]:
    path = job_dir / S3_RECORD_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
