"""In-memory stand-in for the boto3 S3 client, for tests. No network, no AWS account, no real credentials.

It implements only the calls src/s3_video_store.py makes. Objects live in a dict, so tests can inspect exactly what
would have been uploaded, listed, deleted, or presigned.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Optional

from src import s3_video_store as s3


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: s3_video_store reads the AWS code from .response."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.sse: dict[str, Optional[str]] = {}
        self.presigned: list[dict[str, Any]] = []
        self.fail_upload_for: set[str] = set()  # keys whose upload raises
        self.fail_presign = False
        self.bucket_missing = False

    # ---- writes ----
    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: Optional[dict] = None) -> None:
        if key in self.fail_upload_for:
            raise ConnectionError("network down")
        self._store(key, Path(filename).read_bytes(), ExtraArgs or {})

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "", ServerSideEncryption: str = "") -> None:
        self._store(Key, Body, {"ContentType": ContentType, "ServerSideEncryption": ServerSideEncryption})

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:
        for item in Delete.get("Objects", []):
            self.objects.pop(item["Key"], None)
        return {}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def _store(self, key: str, data: bytes, extra: dict) -> None:
        self.objects[key] = data
        self.content_types[key] = extra.get("ContentType", "")
        self.sse[key] = extra.get("ServerSideEncryption")

    # ---- reads ----
    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise FakeClientError("404")
        return {"ContentLength": len(self.objects[Key])}

    def head_bucket(self, Bucket: str) -> dict:
        if self.bucket_missing:
            raise FakeClientError("NoSuchBucket")
        return {}

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        if Key not in self.objects:
            raise FakeClientError("404")
        Path(Filename).parent.mkdir(parents=True, exist_ok=True)
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(self, Bucket: str, Prefix: str = "", Delimiter: Optional[str] = None,
                        ContinuationToken: Optional[str] = None) -> dict:
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        if not Delimiter:
            return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}
        folders: set[str] = set()
        contents: list[dict] = []
        for key in keys:
            rest = key[len(Prefix):]
            if "/" in rest:
                folders.add(Prefix + rest.split("/", 1)[0] + "/")
            else:
                contents.append({"Key": key})
        return {
            "CommonPrefixes": [{"Prefix": f} for f in sorted(folders)],
            "Contents": contents,
            "IsTruncated": False,
        }

    # ---- links ----
    def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:
        if self.fail_presign:
            raise RuntimeError("presign broke")
        self.presigned.append({"op": op, "params": Params, "expires": ExpiresIn})
        return f"https://fake-s3.invalid/{Params['Key']}?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=signed"


def new_store(client: Optional[FakeS3Client] = None) -> s3.VideoStore:
    """A VideoStore on the fake client. Clears the manifest cache so one test cannot see another's jobs."""
    s3.clear_caches()
    return s3.VideoStore(s3.settings_from_env({}), client=client or FakeS3Client())


def manifest(store: s3.VideoStore, job_id: str) -> Optional[dict[str, Any]]:
    """The manifest the app wrote for job_id, read straight from the fake bucket (not through the app)."""
    import json

    for key in store.client.objects:
        if key.startswith(f"{s3.JOB_ROOT}{job_id}/") and key.endswith("/" + s3.MANIFEST_NAME):
            return json.loads(store.client.objects[key].decode("utf-8"))
    return None


def publish_local_job(app_module: Any, store: s3.VideoStore, job_dir: Path, job_id: str,
                      *, source: str = "clip.mp4") -> dict[str, Any]:
    """Publish a finished local job folder to the fake bucket, the same way process_video does."""
    results = app_module._results_payload(Path(job_dir), 0)
    app_module._prepare_downloadable_files(Path(job_dir), results)
    now = "2026-01-01T00:00:00"
    return s3.publish_job(
        store, Path(job_dir), job_id=job_id, video_uid=s3.make_video_uid(), source_name=source,
        created_at=now, completed_at=now, results=results,
    )


def publish_failed_job(store: s3.VideoStore, job_id: str, error: str, error_code: Optional[str] = None,
                       *, source: str = "clip.mp4") -> dict[str, Any]:
    now = "2026-01-01T00:00:00"
    return s3.publish_failure(
        store, job_id=job_id, video_uid=s3.make_video_uid(), source_name=source, created_at=now,
        completed_at=now, error=error, error_code=error_code,
    )


def key_from_link(url: str) -> str:
    """The object key inside a link issued by FakeS3Client.generate_presigned_url."""
    return url.split("https://fake-s3.invalid/", 1)[1].split("?", 1)[0]
