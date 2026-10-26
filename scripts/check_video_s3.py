#!/usr/bin/env python3
"""Read-only S3 checks for the video-summarizer bucket. Uploads, deletes, and writes nothing.

Credentials come from the standard AWS chain: the profile in VIDEO_S3_PROFILE (or AWS_PROFILE) when set,
otherwise the default chain. No secret is read into this script or printed.

Usage (from the project root):
  VIDEO_S3_PROFILE=video-summarizer venv-mac/bin/python scripts/check_video_s3.py
"""

from __future__ import annotations
from src.log_safety import safe_exc

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.gpu_selection import load_env_file  # noqa: E402
from src.s3_video_store import settings_from_env  # noqa: E402


def run_checks(session, settings: dict) -> list[tuple[str, bool, str]]:
    """Return (check name, passed, detail). Only read-only API calls are made."""
    bucket = settings["bucket"]
    region = settings["region"]
    s3 = session.client("s3", region_name=region)
    results: list[tuple[str, bool, str]] = []

    try:
        ident = session.client("sts", region_name=region).get_caller_identity()
        results.append(("caller identity", True, f"account {ident['Account']}, user {ident['Arn'].split('/')[-1]}"))
    except Exception as exc:  # noqa: BLE001
        results.append(("caller identity", False, f"{type(exc).__name__}: {exc}"))
        return results  # nothing else can succeed without credentials

    try:
        s3.head_bucket(Bucket=bucket)
        results.append(("bucket reachable", True, bucket))
    except Exception as exc:  # noqa: BLE001
        results.append(("bucket reachable", False, f"{type(exc).__name__}: {exc}"))
        return results

    loc = (s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1")
    results.append(("bucket region", loc == region, f"{loc} (expected {region})"))

    try:
        rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
        algo = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        results.append(("default encryption", algo == "AES256", f"{algo} (expected AES256 / SSE-S3)"))
    except Exception as exc:  # noqa: BLE001
        results.append(("default encryption", False, f"{type(exc).__name__}: {exc}"))

    try:
        block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        all_blocked = all(block.values())
        results.append(("public access blocked", all_blocked, str(block)))
    except Exception as exc:  # noqa: BLE001
        results.append(("public access blocked", False, f"{type(exc).__name__}: {exc}"))

    try:
        count = s3.list_objects_v2(Bucket=bucket, MaxKeys=1).get("KeyCount", 0)
        results.append(("list objects (read-only)", True, f"{count} object(s) visible in first page"))
    except Exception as exc:  # noqa: BLE001
        results.append(("list objects (read-only)", False, f"{type(exc).__name__}: {exc}"))

    # Signing is local cryptography: no request is sent. The URL itself is never printed.
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": "readiness-check-does-not-exist"},
            ExpiresIn=int(settings["presign_expires"]),
        )
        ok = url.startswith("https://") and f"X-Amz-Expires={int(settings['presign_expires'])}" in url
        results.append(("presigned URL signs", ok, f"expiry {settings['presign_expires']} s (URL not shown)"))
    except Exception as exc:  # noqa: BLE001
        results.append(("presigned URL signs", False, f"{type(exc).__name__}: {exc}"))
    return results


def main() -> int:
    load_env_file()
    settings = settings_from_env()
    try:
        import boto3
    except ImportError:
        print("boto3 is not installed. Run: uv pip install --python venv-mac/bin/python --require-hashes -r requirements/mac-cpu.lock")
        return 2
    profile = settings.get("profile")
    if profile:
        try:
            session = boto3.Session(profile_name=profile)
        except Exception as exc:  # noqa: BLE001
            print(f"Profile '{profile}' could not be loaded: {safe_exc(exc)}")
            return 2
    else:
        session = boto3.Session()
    print(f"profile: {profile or '(standard credential chain)'}  bucket: {settings['bucket']}  region: {settings['region']}")
    results = run_checks(session, settings)
    failed = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
