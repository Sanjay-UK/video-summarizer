"""Video storage in S3 for RunPod highlight jobs.

Security model: this app runs on the user's own machine, which is the trusted backend. AWS credentials come
only from the standard AWS credential chain on that machine (a named profile via AWS_PROFILE/VIDEO_S3_PROFILE,
or environment variables). They are never stored in the repo, the browser, or the RunPod request. The worker
receives only a presigned GET URL for one source object, which expires (default 3600 s).

Highlights: after a worker renders a highlight, the app uploads it here too and records only the object key,
bucket, and metadata in the job folder. Every playback or download link is a fresh presigned GET URL that lasts
HIGHLIGHT_URL_EXPIRES seconds. No URL is ever saved.

Off by default: nothing here uploads unless VIDEO_S3_ENABLED is true. Local mode never touches S3.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from src.locales import LOCALES

DEFAULT_BUCKET = "video-summarizer-757374023708-ap-south-1-an"
DEFAULT_REGION = "ap-south-1"
DEFAULT_PROFILE = "video-summarizer"
DEFAULT_PRESIGN_EXPIRES = 3600
HIGHLIGHT_URL_EXPIRES = 3600  # fixed: every highlight link is a one-hour URL, whatever VIDEO_S3_PRESIGN_EXPIRES says
MP4_CONTENT_TYPE = "video/mp4"
ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv"})
S3_RECORD_NAME = "s3_video.json"
HIGHLIGHT_RECORD_NAME = "s3_highlights.json"

_HIGHLIGHT_KEY_RE = re.compile(
    r"^highlights/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[a-z]{2,3}(?:-[a-z0-9]{2,8})?\.mp4$"
)
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")
# Only these fields are ever read or written. A URL or any other field in an old or edited record is dropped.
_RECORD_FIELDS = ("bucket", "key", "content_type", "size_bytes", "status", "uploaded_at", "failed_at", "error_code")

# HTTP status for each HighlightStoreError code. Messages never include credentials, keys, or URLs.
ERROR_STATUS = {
    "not_in_s3": 404,
    "object_missing": 404,
    "invalid_record": 500,
    "presign_failed": 500,
    "credentials_missing": 503,
    "credentials_expire_early": 503,
    "s3_access": 503,
    "s3_unavailable": 503,
    "invalid_language": 400,
}


class HighlightStoreError(Exception):
    """A user-facing S3 problem. `code` is machine-readable; the message is safe to show and to log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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


def make_highlight_key(lang_code: str) -> str:
    """Stable key for one finished highlight: highlights/<uuid>/<lang>.mp4. Recorded once, never reused."""
    if not _LANG_RE.match(lang_code or ""):
        raise ValueError("invalid language code")
    return f"highlights/{uuid.uuid4()}/{lang_code}.mp4"


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
            import boto3  # imported here so local mode never needs AWS libraries

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

    def upload_source(self, path: Path, key: str, content_type: str = "video/mp4") -> None:
        """Upload with server-side encryption (SSE-S3, AES256). Keys are unique per job, so nothing is overwritten."""
        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256", "ContentType": content_type},
        )

    def head(self, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=self.bucket, Key=key)

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

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def signing_credentials_problem(self, needed_seconds: int) -> Optional[str]:
        """None when the signing credentials stay valid for needed_seconds, otherwise a reason code.

        A long-term access key has no expiry until it is rotated. Temporary credentials (SSO, assumed role) stop
        signing working at their own expiry, even when a URL says it lasts longer. That case is reported here,
        not hidden behind a URL that will fail early. When the client was injected, there is nothing to inspect.
        """
        # Build the client first: the session only exists after that. A store made per request would otherwise
        # skip this check entirely, because nothing has created its session yet.
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


def write_record(job_dir: Path, record: dict[str, Any]) -> None:
    """Source-video job metadata only: bucket, key, and timestamps. Never the presigned URL or any credential."""
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / S3_RECORD_NAME).write_text(json.dumps(record, indent=2), encoding="utf-8")


def read_record(job_dir: Path) -> dict[str, Any]:
    path = job_dir / S3_RECORD_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def highlight_record_path(job_dir: Path) -> Path:
    return Path(job_dir) / HIGHLIGHT_RECORD_NAME


def _clean_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {field: raw[field] for field in _RECORD_FIELDS if field in raw}


def read_highlight_records(job_dir: Path) -> dict[str, dict[str, Any]]:
    """Language code -> whitelisted record. A missing or unreadable file means no records."""
    path = highlight_record_path(job_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    langs = raw.get("languages") if isinstance(raw, dict) else None
    if not isinstance(langs, dict):
        return {}
    return {
        lang: _clean_record(rec)
        for lang, rec in langs.items()
        if isinstance(lang, str) and _LANG_RE.match(lang)
    }


def write_highlight_record(job_dir: Path, lang_code: str, record: dict[str, Any]) -> None:
    if not _LANG_RE.match(lang_code or ""):
        raise ValueError("invalid language code")
    records = read_highlight_records(job_dir)
    records[lang_code] = _clean_record(record)
    Path(job_dir).mkdir(parents=True, exist_ok=True)
    highlight_record_path(job_dir).write_text(
        json.dumps({"languages": records}, indent=2), encoding="utf-8"
    )


def stored_highlight_langs(job_dir: Path) -> list[str]:
    """Languages whose highlight was uploaded and verified. Reads only the record file, never S3."""
    return sorted(
        lang for lang, rec in read_highlight_records(job_dir).items() if rec.get("status") == "uploaded"
    )


def upload_highlight(job_dir: Path, lang_code: str, mp4_path: Path, store: VideoStore) -> dict[str, Any]:
    """Upload one finished highlight, check its size in S3, and record the key.

    Returns the saved record. Upload problems are recorded as status 'failed' and returned, not raised,
    so the caller can report the language as failed. Nothing is deleted.
    """
    mp4_path = Path(mp4_path)
    size = mp4_path.stat().st_size
    key = make_highlight_key(lang_code)
    record: dict[str, Any] = {
        "bucket": store.bucket,
        "key": key,
        "content_type": MP4_CONTENT_TYPE,
        "size_bytes": size,
    }
    try:
        store.upload_source(mp4_path, key, content_type=MP4_CONTENT_TYPE)
        head = store.head(key)
        if int(head.get("ContentLength", -1)) != size:
            raise HighlightStoreError("size_mismatch", "The uploaded highlight size does not match the file.")
        record.update(status="uploaded", uploaded_at=utc_now())
    except Exception as exc:  # noqa: BLE001 — recorded, reported by the caller
        code = exc.code if isinstance(exc, HighlightStoreError) else error_code(exc)
        record.update(status="failed", failed_at=utc_now(), error_code=code)
    write_highlight_record(job_dir, lang_code, record)
    return _clean_record(record)


def highlight_url(
    job_dir: Path,
    lang_code: str,
    store: VideoStore,
    *,
    attachment: bool = False,
) -> dict[str, Any]:
    """A fresh presigned GET URL for a stored highlight, valid for HIGHLIGHT_URL_EXPIRES seconds.

    The URL is returned to the caller and never written anywhere. Only the recorded key is read, and it must match
    the expected pattern and the configured bucket. A record from another bucket is refused, not used.
    """
    if lang_code not in LOCALES:
        raise HighlightStoreError("invalid_language", "Unsupported highlight language.")
    record = read_highlight_records(job_dir).get(lang_code)
    if not record or record.get("status") != "uploaded":
        raise HighlightStoreError("not_in_s3", "This highlight is not stored in S3.")
    key = record.get("key")
    if not isinstance(key, str) or not _HIGHLIGHT_KEY_RE.match(key) or record.get("bucket") != store.bucket:
        raise HighlightStoreError("invalid_record", "The stored highlight record is not valid, so it was not used.")

    problem = store.signing_credentials_problem(HIGHLIGHT_URL_EXPIRES)
    if problem:
        raise HighlightStoreError(
            problem,
            "The signing credentials expire before a one-hour link would. Use long-term keys for the "
            "video-summarizer profile, or refresh the temporary credentials.",
        )

    try:
        store.head(key)
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HighlightStoreError("object_missing", "The highlight is missing from S3.") from None
        raise HighlightStoreError(
            "s3_access", f"S3 refused the request ({code}). Check the video-summarizer AWS profile."
        ) from None

    disposition = f'{"attachment" if attachment else "inline"}; filename="highlight_{lang_code}.mp4"'
    try:
        url = store.presign_get(
            key,
            expires=HIGHLIGHT_URL_EXPIRES,
            content_type=MP4_CONTENT_TYPE,
            disposition=disposition,
        )
    except Exception as exc:  # noqa: BLE001
        raise HighlightStoreError("presign_failed", f"Could not sign the link ({error_code(exc)}).") from None

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=HIGHLIGHT_URL_EXPIRES)
    return {
        "url": url,
        "expires_in": HIGHLIGHT_URL_EXPIRES,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def highlight_store() -> VideoStore:
    """The store for the configured bucket and profile. Created per call; the boto3 client is built lazily."""
    return VideoStore(settings_from_env())


def has_stored_highlight(job_dir: Path, lang_code: str) -> bool:
    return lang_code in stored_highlight_langs(job_dir)


def upload_failed_for_lang(job_dir: Path, lang_code: str) -> bool:
    """True when this highlight's S3 upload failed. The local copy must not be served in that case."""
    from src.highlight_jobs import read_highlight_status

    row = (read_highlight_status(job_dir).get("languages") or {}).get(lang_code) or {}
    if row.get("status") == "failed" and row.get("failure_kind") == "s3_upload":
        return True
    return read_highlight_records(job_dir).get(lang_code, {}).get("status") == "failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
