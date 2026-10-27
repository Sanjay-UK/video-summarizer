#!/usr/bin/env python3
"""One-shot worker diagnostic for v1.0.6: patch endpoint, run one diagnostic job, write report.

No model loads, no warm request. Writes output/_deploy_v106_diagnostic.json and refuses to overwrite it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.gpu_selection import load_env_file  # noqa: E402
from src.runpod_serverless import (  # noqa: E402
    get_serverless_endpoint,
    patch_serverless_endpoint,
    poll_job_until_done,
    submit_job,
)

ENDPOINT = "4jxzzc08sr5z0w"
IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.6"
IMAGE_DIGEST = "sha256:9ff7ca86748e497edf27db40289b793459612114a7772f141f7f931202cefd1a"
PREV_IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.5"
REPORT_PATH = ROOT / "output" / "_deploy_v106_diagnostic.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def main() -> int:
    if REPORT_PATH.exists():
        print(f"Refusing to run: {REPORT_PATH} already exists.")
        return 2
    load_env_file()
    report: dict = {"image": IMAGE, "image_digest": IMAGE_DIGEST, "endpoint_id": ENDPOINT, "status": "in_progress"}

    def save() -> None:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    try:
        out = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", IMAGE, "--format", "{{json .Manifest.Digest}}"],
            capture_output=True, text=True, timeout=120,
        )
        digest = json.loads(out.stdout.strip()) if out.returncode == 0 else None
        report["remote_digest_ok"] = digest == IMAGE_DIGEST
        if digest != IMAGE_DIGEST:
            raise RuntimeError(f"Remote digest {digest} != expected {IMAGE_DIGEST}")

        ep, err = get_serverless_endpoint(ENDPOINT)
        if err or not ep:
            raise RuntimeError(f"GET endpoint failed: {err}")
        if ep.get("image") != PREV_IMAGE:
            raise RuntimeError(f"Endpoint image is {ep.get('image')}, expected {PREV_IMAGE}")

        report["patch_start_utc"] = utc_now()
        t0 = time.monotonic()
        patch_serverless_endpoint(ENDPOINT, {"image": IMAGE})
        report["patch_sec"] = round(time.monotonic() - t0, 3)
        report["patch_end_utc"] = utc_now()

        job_id = submit_job(ENDPOINT, {"job_type": "diagnostic"})
        report["job_id"] = job_id
        report["submit_utc"] = utc_now()
        t1 = time.monotonic()
        job = poll_job_until_done(ENDPOINT, job_id, log=lambda m: print(f"[{utc_now()}] {m}", flush=True), max_wait_sec=900)
        report["job_wall_clock_sec"] = round(time.monotonic() - t1, 3)
        report["runpod_status"] = job.get("status")
        report["delay_time_ms"] = job.get("delayTime")
        report["execution_time_ms"] = job.get("executionTime")
        report["diagnostic_output"] = job.get("output")
        report["status"] = "success"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:2000]}"
        print(f"FAILED: {exc}", flush=True)
        save()
        return 1
    save()
    print(f"DONE report={REPORT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
