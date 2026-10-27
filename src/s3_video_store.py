"""S3 is the only store for what the app shows: History, results, playback and downloads.

Layout, one folder per job and per video:
    jobs/<job_id>/<video_uid>/manifest.json   written LAST. A job appears in History only when this exists.
    jobs/<job_id>/<video_uid>/<relative path>  every file of the job (source video, summary, audio, highlights, ...)

The local output folder is only a scratch area while a job runs. History, results, playback and downloads never read
it. A failed upload fails the job; nothing falls back to a local copy.

Security model: this app runs on the user's own machine, which is the trusted backend. AWS credentials come only from
the video-summarizer profile on that machine (see settings_from_env). They are never stored in the repo, the browser,
or the RunPod request. Every link is a fresh presigned GET URL that lasts HIGHLIGHT_URL_EXPIRES seconds. No URL is ever
saved. The RunPod worker gets a presigned URL for the source only when VIDEO_S3_WORKER_URL_SUPPORT is enabled.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_BUCKET = "video-summarizer-757374023708-ap-south-1-an"
DEFAULT_REGION = "ap-south-1"
DEFAULT_PROFILE = "video-summarizer"
DEFAULT_PRESIGN_EXPIRES = 3600
HIGHLIGHT_URL_EXPIRES = 3600  # fixed: every link the app issues is a one-hour URL, whatever VIDEO_S3_PRESIGN_EXPIRES says
ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv"})
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
JOB_ROOT = "jobs/"
MANIFEST_CACHE_SECONDS = 30
LOAD_WORKERS = 8

JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")
VIDEO_UID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")

# Local-only bookkeeping and per-scene render scratch. Not uploaded: the manifest describes the job instead.
_LOCAL_ONLY_NAMES = frozenset({"s3_video.json", "s3_highlights.json", "concat_scenes.txt"})
_SCRATCH_DIR_NAMES = frozenset({"build"})
_SCRATCH_SUFFIXES = (".partial.mp3", ".tmp")

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".ass": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}

# HTTP status for each HighlightStoreError code. Messages never include credentials, keys, or URLs.
ERROR_STATUS = {
    "not_in_s3": 404,
    "object_missing": 404,
    "job_not_found": 404,
    "invalid_record": 500,
    "presign_failed": 500,
    "credentials_missing": 503,
    "credentials_expire_early": 503,
    "s3_access": 503,
    "s3_unavailable": 503,
    "size_mismatch": 500,
    "invalid_language": 400,
}

S3_MESSAGES = {
    "credentials_missing": "AWS credentials for the video-summarizer profile were not found on this machine.",
    "credentials_expire_early": "The AWS signing credentials expire too soon. Use long-term keys for the video-summarizer profile.",
    "s3_access": "S3 refused the request. Check the video-summarizer AWS profile and its permissions.",
    "s3_unavailable": "S3 could not be reached. Check the network connection and try again.",
}


class HighlightStoreError(Exception):
    """A user-facing S3 problem. `code` is machine-readable; the message is safe to show and to log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def worker_url_support(env: Optional[dict[str, str]] = None) -> bool:
    """True when the RunPod worker may download the source from a presigned URL.

    Set this only after a worker image that accepts video_url jobs is deployed. Storage itself does not depend on it.
    """
    env = os.environ if env is None else env
    return _truthy(env.get("VIDEO_S3_WORKER_URL_SUPPORT", ""))


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


def make_video_uid() -> str:
    """A new unique ID for one video. Stored with the job in S3 and never reused."""
    return str(uuid.uuid4())


def job_prefix(job_id: str, video_uid: str) -> str:
    if not JOB_ID_RE.match(job_id or "") or not VIDEO_UID_RE.match(video_uid or ""):
        raise ValueError("invalid job or video id")
    return f"{JOB_ROOT}{job_id}/{video_uid}/"


def artifact_key(job_id: str, video_uid: str, rel_path: str) -> str:
    if not _REL_PATH_RE.match(rel_path or "") or ".." in rel_path.split("/"):
        raise ValueError("invalid artifact path")
    return job_prefix(job_id, video_uid) + rel_path


def manifest_key(job_id: str, video_uid: str) -> str:
    return job_prefix(job_id, video_uid) + MANIFEST_NAME


def content_type_for(rel_path: str) -> str:
    return _CONTENT_TYPES.get(Path(rel_path).suffix.lower(), "application/octet-stream")


def error_code(exc: BaseException) -> str:
    """AWS error code (AccessDenied, NoSuchKey, 404) or the exception class name. Never the message: it can echo keys."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)[:40]
    return type(exc).__name__[:40]


class VideoStore:
    """Thin wrapper around one S3 bucket. The boto3 client is created lazily, so tests can inject a fake."""

    def __init__(self, settings: dict[str, Any], client: Any = None):
        self.settings = settings
        self._client = client
        self._session: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # imported here so the rest of the app never needs AWS libraries to start

            from botocore.config import Config

            self._session = boto3.Session(profile_name=self.settings.get("profile"))
            # SigV4 with virtual-host addressing gives the regional host (bucket.s3.<region>.amazonaws.com). The
            # default presign host is the global one, and S3 rejects its SigV4 signature (SignatureDoesNotMatch)
            # for buckets outside us-east-1. Set here so uploads, head checks, and presigned links all agree.
            config = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
            self._client = self._session.client("s3", region_name=self.settings["region"], config=config)
        return self._client

    @property
    def bucket(self) -> str:
        return self.settings["bucket"]

    def upload_file(self, path: Path, key: str, content_type: Optional[str] = None) -> None:
        """Upload with server-side encryption (SSE-S3, AES256)."""
        extra = {"ServerSideEncryption": "AES256", "ContentType": content_type or content_type_for(key)}
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)

    def put_json(self, key: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def get_json(self, key: str) -> Optional[dict[str, Any]]:
        """The parsed object, or None when it does not exist."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        data = json.loads(response["Body"].read().decode("utf-8"))
        return data if isinstance(data, dict) else None

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dest))

    def head(self, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=self.bucket, Key=key)

    def head_bucket(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def list_common_prefixes(self, prefix: str) -> list[str]:
        """The "folders" directly under prefix, each ending in '/'."""
        found: list[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            found.extend(p["Prefix"] for p in response.get("CommonPrefixes", []))
            if not response.get("IsTruncated"):
                return found
            token = response.get("NextContinuationToken")

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return keys
            token = response.get("NextContinuationToken")

    def delete_keys(self, keys: Iterable[str]) -> None:
        batch = list(keys)
        for start in range(0, len(batch), 1000):
            chunk = [{"Key": k} for k in batch[start:start + 1000]]
            response = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": chunk, "Quiet": True})
            if response.get("Errors"):
                raise HighlightStoreError("s3_access", "Some files could not be deleted from S3.")

    def presign_get(
        self,
        key: str,
        *,
        expires: Optional[int] = None,
        content_type: Optional[str] = None,
        disposition: Optional[str] = None,
    ) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if content_type:
            params["ResponseContentType"] = content_type
        if disposition:
            params["ResponseContentDisposition"] = disposition
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=int(expires or self.settings["presign_expires"]),
        )

    def signing_credentials_problem(self, needed_seconds: int) -> Optional[str]:
        """None when the signing credentials stay valid for needed_seconds, otherwise a reason code.

        A long-term access key has no expiry until it is rotated. Temporary credentials (SSO, assumed role) stop
        signing working at their own expiry, even when a URL says it lasts longer. That case is reported here,
        not hidden behind a URL that will fail early. When the client was injected, there is nothing to inspect.
        """
        # Build the client first: the session only exists after that.
        self.client  # noqa: B018 — side effect: creates self._session
        if self._session is None:
            return None
        creds = self._session.get_credentials()
        if creds is None:
            return "credentials_missing"
        if not getattr(creds, "token", None):
            return None
        expiry = getattr(creds, "_expiry_time", None)
        if expiry is None:
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry - datetime.now(timezone.utc) < timedelta(seconds=needed_seconds):
            return "credentials_expire_early"
        return None


def highlight_store() -> VideoStore:
    """The store for the configured bucket and profile. Created per call; the boto3 client is built lazily."""
    return VideoStore(settings_from_env())


def as_store_error(exc: BaseException) -> HighlightStoreError:
    """Map any S3 or credential failure to a user-safe HighlightStoreError. The AWS message itself is never kept."""
    if isinstance(exc, HighlightStoreError):
        return exc
    code = error_code(exc)
    if "Credentials" in code or "Profile" in code:
        return HighlightStoreError("credentials_missing", S3_MESSAGES["credentials_missing"])
    if code in ("403", "AccessDenied", "Forbidden", "404", "NoSuchBucket"):
        return HighlightStoreError("s3_access", S3_MESSAGES["s3_access"])
    return HighlightStoreError("s3_unavailable", S3_MESSAGES["s3_unavailable"])


def ensure_ready(store: VideoStore) -> None:
    """Raise HighlightStoreError unless S3 can be used now: credentials, bucket access, and long enough credentials
    to sign one-hour links. Called before a job starts, so a broken setup is reported immediately."""
    try:
        store.head_bucket()
    except Exception as exc:  # noqa: BLE001
        raise as_store_error(exc) from None
    problem = store.signing_credentials_problem(HIGHLIGHT_URL_EXPIRES)
    if problem:
        raise HighlightStoreError(problem, S3_MESSAGES[problem])


# ---- Manifest cache. Only S3 is the source of truth; this just avoids a listing on every page refresh. ----

_cache_lock = threading.Lock()
_manifest_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
# The whole History listing. The page polls it every few seconds, so a full bucket listing must not run each time.
_history_cache: Optional[tuple[float, list[dict[str, Any]]]] = None


def clear_caches() -> None:
    """Forget every cached manifest and listing. For tests, which reuse one process across fake buckets."""
    global _history_cache
    with _cache_lock:
        _manifest_cache.clear()
        _history_cache = None


def invalidate_manifest(job_id: str) -> None:
    global _history_cache
    with _cache_lock:
        _manifest_cache.pop(job_id, None)
        _history_cache = None


def _valid_manifest(manifest: Any, job_id: str, video_uid: str) -> bool:
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return False
    if manifest.get("job_id") != job_id or manifest.get("video_uid") != video_uid:
        return False
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False
    for rel, entry in files.items():
        try:
            expected = artifact_key(job_id, video_uid, rel)
        except ValueError:
            return False
        if not isinstance(entry, dict) or entry.get("key") != expected:
            return False
    return True


def load_job_manifest(store: VideoStore, job_id: str) -> Optional[dict[str, Any]]:
    """The newest valid manifest for this job ID, or None. Reads S3 (cached for MANIFEST_CACHE_SECONDS)."""
    if not JOB_ID_RE.match(job_id or ""):
        return None
    with _cache_lock:
        cached = _manifest_cache.get(job_id)
    if cached and time.monotonic() - cached[0] < MANIFEST_CACHE_SECONDS:
        return cached[1]

    found: Optional[dict[str, Any]] = None
    for prefix in store.list_common_prefixes(f"{JOB_ROOT}{job_id}/"):
        video_uid = prefix[len(JOB_ROOT) + len(job_id) + 1:].strip("/")
        if not VIDEO_UID_RE.match(video_uid):
            continue
        candidate = store.get_json(prefix + MANIFEST_NAME)
        if candidate is None or not _valid_manifest(candidate, job_id, video_uid):
            continue
        if found is None or str(candidate.get("created_at", "")) > str(found.get("created_at", "")):
            found = candidate

    with _cache_lock:
        _manifest_cache[job_id] = (time.monotonic(), found)
    return found


def list_job_manifests(store: VideoStore) -> list[dict[str, Any]]:
    """Every job in the bucket that has a valid manifest, newest first. Jobs without one are not listed.

    Cached for MANIFEST_CACHE_SECONDS. Any publish or delete on this machine clears the cache at once.
    """
    global _history_cache
    with _cache_lock:
        cached_history = _history_cache
    if cached_history and time.monotonic() - cached_history[0] < MANIFEST_CACHE_SECONDS:
        return list(cached_history[1])
    store.client  # noqa: B018 — create the client before worker threads use it
    job_ids: list[str] = []
    for prefix in store.list_common_prefixes(JOB_ROOT):
        job_id = prefix[len(JOB_ROOT):].strip("/")
        if JOB_ID_RE.match(job_id):
            job_ids.append(job_id)
    with ThreadPoolExecutor(max_workers=LOAD_WORKERS) as pool:
        loaded = list(pool.map(lambda jid: load_job_manifest(store, jid), job_ids))
    found = [m for m in loaded if m]
    found.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
    with _cache_lock:
        _history_cache = (time.monotonic(), found)
    return list(found)


def publish_job(
    store: VideoStore,
    job_dir: Path,
    *,
    job_id: str,
    video_uid: str,
    source_name: str,
    created_at: str,
    completed_at: str,
    results: dict[str, Any],
    known_files: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Upload every file of a finished job folder, then write the manifest last.

    known_files holds files already uploaded during this job (the source video), so they are not sent twice.
    Any upload that fails raises, and no manifest is written: the job does not appear in History.
    """
    job_dir = Path(job_dir)
    files: dict[str, dict[str, Any]] = dict(known_files or {})
    for path in sorted(p for p in job_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(job_dir).as_posix()
        if rel in files or not _should_upload(rel):
            continue
        files[rel] = upload_job_file(store, job_id, video_uid, path, rel)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "job_id": job_id,
        "video_uid": video_uid,
        "bucket": store.bucket,
        "source": source_name,
        "status": "completed",
        "created_at": created_at,
        "completed_at": completed_at,
        "updated_at": utc_now(),
        "error": None,
        "error_code": None,
        "results": results,
        "files": files,
    }
    store.put_json(manifest_key(job_id, video_uid), manifest)
    invalidate_manifest(job_id)
    return manifest


def publish_failure(
    store: VideoStore,
    *,
    job_id: str,
    video_uid: str,
    source_name: str,
    created_at: str,
    completed_at: str,
    error: str,
    error_code: Optional[str],
    known_files: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """A failed job is recorded so History shows it and the user can delete it. Only files already uploaded are listed."""
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "job_id": job_id,
        "video_uid": video_uid,
        "bucket": store.bucket,
        "source": source_name,
        "status": "error",
        "created_at": created_at,
        "completed_at": completed_at,
        "updated_at": utc_now(),
        "error": error,
        "error_code": error_code,
        "results": None,
        "files": dict(known_files or {}),
    }
    store.put_json(manifest_key(job_id, video_uid), manifest)
    invalidate_manifest(job_id)
    return manifest


def upload_job_file(store: VideoStore, job_id: str, video_uid: str, path: Path, rel: str) -> dict[str, Any]:
    """Upload one file and check its size in S3 before it is listed in a manifest."""
    key = artifact_key(job_id, video_uid, rel)
    size = path.stat().st_size
    content_type = content_type_for(rel)
    store.upload_file(path, key, content_type=content_type)
    head = store.head(key)
    if int(head.get("ContentLength", -1)) != size:
        raise HighlightStoreError("size_mismatch", f"The uploaded file {Path(rel).name} does not match the local file.")
    return {"key": key, "size": size, "content_type": content_type}


def download_job_files(store: VideoStore, manifest: dict[str, Any], dest_dir: Path) -> Path:
    """Copy every file of a job from S3 into dest_dir. Used by retries, which re-render from the job's own files."""
    job_id, video_uid = manifest["job_id"], manifest["video_uid"]
    dest_dir = Path(dest_dir)
    for rel, entry in manifest.get("files", {}).items():
        store.download(artifact_key(job_id, video_uid, rel), dest_dir / rel)
    return dest_dir


def file_link(
    store: VideoStore,
    manifest: dict[str, Any],
    rel: str,
    *,
    attachment: bool = False,
) -> dict[str, Any]:
    """A fresh presigned GET URL for one file of a job, valid for HIGHLIGHT_URL_EXPIRES seconds.

    The URL is returned to the caller and never written anywhere. The stored key must match the layout for this job
    and video, and the bucket must match the configured bucket.
    """
    entry = (manifest.get("files") or {}).get(rel)
    if not entry:
        raise HighlightStoreError("not_in_s3", "This file is not available for this job.")
    job_id, video_uid = manifest["job_id"], manifest["video_uid"]
    key = entry.get("key")
    if key != artifact_key(job_id, video_uid, rel) or manifest.get("bucket") != store.bucket:
        raise HighlightStoreError("invalid_record", "The stored file record is not valid, so it was not used.")

    problem = store.signing_credentials_problem(HIGHLIGHT_URL_EXPIRES)
    if problem:
        raise HighlightStoreError(problem, S3_MESSAGES[problem])

    try:
        store.head(key)
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HighlightStoreError("object_missing", "This file is no longer available in S3.") from None
        raise HighlightStoreError(
            "s3_access", f"S3 refused the request ({code}). Check the video-summarizer AWS profile."
        ) from None

    name = Path(rel).name
    disposition = f'{"attachment" if attachment else "inline"}; filename="{name}"'
    try:
        url = store.presign_get(
            key,
            expires=HIGHLIGHT_URL_EXPIRES,
            content_type=entry.get("content_type") or content_type_for(rel),
            disposition=disposition,
        )
    except Exception:  # noqa: BLE001
        raise HighlightStoreError("presign_failed", "The download link could not be created. Please try again.") from None

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=HIGHLIGHT_URL_EXPIRES)
    return {
        "url": url,
        "expires_in": HIGHLIGHT_URL_EXPIRES,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def delete_job(store: VideoStore, job_id: str) -> int:
    """Delete every file of a job, then its manifest last. A job whose delete fails midway keeps its manifest, so it
    stays visible and can be deleted again. Returns the number of objects removed."""
    if not JOB_ID_RE.match(job_id or ""):
        raise HighlightStoreError("job_not_found", "Job not found.")
    removed = 0
    for prefix in store.list_common_prefixes(f"{JOB_ROOT}{job_id}/"):
        keys = store.list_keys(prefix)
        manifest = prefix + MANIFEST_NAME
        others = [k for k in keys if k != manifest]
        store.delete_keys(others)
        removed += len(others)
        if manifest in keys:
            store.delete_keys([manifest])
            removed += 1
    invalidate_manifest(job_id)
    return removed


def _should_upload(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in _SCRATCH_DIR_NAMES for part in parts[:-1]):
        return False
    name = parts[-1]
    if name in _LOCAL_ONLY_NAMES or name.endswith(_SCRATCH_SUFFIXES):
        return False
    return bool(_REL_PATH_RE.match(rel))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
