#!/usr/bin/env python3
"""Deploy worker image v1.0.5 to RunPod endpoint 4jxzzc08sr5z0w, with stage timings.

Usage:
  python3 scripts/deploy_v104_timed.py --dry-run   # read-only checks, writes nothing
  python3 scripts/deploy_v104_timed.py             # PATCH endpoint + cold warmup + warm warmup

Mutating / paid operations (only in the non-dry-run mode):
  1. PATCH endpoint image -> v1.0.4 (nothing else in the endpoint body)
  2. One cold warmup job (launches a GPU worker)
  3. One warm warmup job immediately after (reuses that worker if still alive)

Report: output/_deploy_v105.json. Written on every exit path. Refuses to run if it already exists.
Timestamps are UTC (ISO 8601). Durations use a monotonic clock. Values RunPod does not expose
are null with a reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.gpu_selection import load_env_file  # noqa: E402
from src.runpod_serverless import (  # noqa: E402
    build_warmup_input,
    get_serverless_endpoint,
    patch_serverless_endpoint,
    poll_job_until_done,
    submit_job,
)
from src.serverless_model_catalog import load_saved_serverless_models  # noqa: E402

ENDPOINT = "4jxzzc08sr5z0w"
IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.5"
IMAGE_DIGEST = "sha256:2e08e7dfca70119509c0afc6bf07ecdecafbdd37c3d664b1286acd4c2c8acc3b"
PREV_IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.4"
GPU_POOL = "AMPERE_24"
GPU_NAME = "NVIDIA RTX A5000"
ACCEPT_TIMEOUT_SEC = 180
REPORT_PATH = ROOT / "output" / "_deploy_v105.json"
DRY_RUN = False

report: dict[str, Any] = {
    "image": IMAGE,
    "image_digest": IMAGE_DIGEST,
    "endpoint_id": ENDPOINT,
    "status": "in_progress",
    "stages": [],
    "cold_request": None,
    "warm_request": None,
    "costs": None,
    "errors": [],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(msg: str) -> None:
    print(f"[{utc_now()}] {msg}", flush=True)


def write_report() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def stage(name: str, source: str, fn: Callable[[], Any]) -> Any:
    """Run one stage with UTC start/end, monotonic duration, and success/error capture."""
    rec: dict[str, Any] = {
        "name": name,
        "source": source,
        "start_utc": utc_now(),
        "end_utc": None,
        "duration_sec": None,
        "success": None,
        "error": None,
    }
    report["stages"].append(rec)
    t0 = time.monotonic()
    log(f"stage start: {name}")
    try:
        result = fn()
        rec["success"] = True
        return result
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        rec["success"] = False
        rec["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        rec["end_utc"] = utc_now()
        rec["duration_sec"] = round(time.monotonic() - t0, 3)
        log(f"stage end: {name} success={rec['success']} duration={rec['duration_sec']}s")
        if not DRY_RUN:
            write_report()


def null_stage(name: str, reason: str) -> None:
    report["stages"].append(
        {
            "name": name,
            "source": "not exposed by RunPod API or worker logs",
            "start_utc": None,
            "end_utc": None,
            "duration_sec": None,
            "success": None,
            "error": None,
            "unavailable_reason": reason,
        }
    )


def endpoint_snapshot(ep: dict[str, Any]) -> dict[str, Any]:
    """Non-secret fields only. Env vars are excluded on purpose."""
    return {
        "name": ep.get("name"),
        "image": ep.get("image"),
        "gpu": ep.get("gpu"),
        "workers": ep.get("workers"),
        "scaling": ep.get("scaling"),
        "timeout_ms": ep.get("timeout"),
        "network_volumes": ep.get("networkVolumes"),
        "data_centers": ep.get("dataCenterIds"),
    }


def check_image_digest() -> str:
    """Confirm the remote tag resolves to the digest we pushed."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", IMAGE, "--format", "{{json .Manifest.Digest}}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"imagetools inspect failed: {out.stderr.strip()[:300]}")
    digest = json.loads(out.stdout.strip())
    if digest != IMAGE_DIGEST:
        raise RuntimeError(f"Remote digest {digest} != expected {IMAGE_DIGEST}. Stop.")
    return digest


def check_endpoint_pre_state() -> dict[str, Any]:
    ep, err = get_serverless_endpoint(ENDPOINT)
    if err or not ep:
        raise RuntimeError(f"GET endpoint failed: {err}")
    problems = []
    if ep.get("image") != PREV_IMAGE:
        problems.append(f"image is {ep.get('image')}, expected {PREV_IMAGE}")
    workers = ep.get("workers") or {}
    if workers.get("min") != 0 or workers.get("max") != 3 or workers.get("idleTimeout") != 5:
        problems.append(f"workers {workers} != min 0 / max 3 / idleTimeout 5")
    if (ep.get("gpu") or {}).get("pools") != [GPU_POOL]:
        problems.append(f"gpu pools {(ep.get('gpu') or {}).get('pools')} != [{GPU_POOL}]")
    if not ep.get("networkVolumes"):
        problems.append("no network volume attached")
    if problems:
        raise RuntimeError("Endpoint does not match expected pre-state: " + "; ".join(problems))
    return endpoint_snapshot(ep)


def fetch_gpu_rate() -> dict[str, Any]:
    """Public RunPod GPU-type price (read-only GraphQL). Serverless billing rate is NOT verified."""
    from src.runpod_serverless import _api_key

    query = {"query": "query { gpuTypes { id displayName securePrice communityPrice } }"}
    req = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    for g in (data.get("data") or {}).get("gpuTypes", []):
        if g.get("id") == GPU_NAME:
            return {
                "gpu": GPU_NAME,
                "secure_usd_per_hour": g.get("securePrice"),
                "community_usd_per_hour": g.get("communityPrice"),
                "source": "RunPod GraphQL gpuTypes (public on-demand price)",
                "verified_serverless_rate": False,
            }
    raise RuntimeError(f"{GPU_NAME} not found in gpuTypes")


def wait_for_config_accepted() -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < ACCEPT_TIMEOUT_SEC:
        ep, err = get_serverless_endpoint(ENDPOINT)
        if not err and ep and ep.get("image") == IMAGE:
            return round(time.monotonic() - t0, 3)
        time.sleep(5)
    raise RuntimeError(f"Endpoint did not show image {IMAGE} within {ACCEPT_TIMEOUT_SEC}s")


def run_warmup_job(label: str, models: dict[str, str]) -> dict[str, Any]:
    job_input = build_warmup_input(
        whisper_model=models.get("whisper") or "base",
        ollama_model=models.get("ollama") or "llama3.2:3b",
        nllb_model=models.get("nllb") or "facebook/nllb-200-distilled-600M",
    )
    t0 = time.monotonic()
    job_id = submit_job(ENDPOINT, job_input)
    submit_utc = utc_now()
    log(f"{label}: submitted job {job_id}")
    job = poll_job_until_done(ENDPOINT, job_id, log=log, max_wait_sec=7200)
    wall = round(time.monotonic() - t0, 3)
    output = job.get("output") or {}
    ok = isinstance(output, dict) and output.get("ok") is True
    result = {
        "job_id": job_id,
        "submit_utc": submit_utc,
        "end_utc": utc_now(),
        "wall_clock_sec": wall,
        "runpod_status": job.get("status"),
        "delay_time_ms": job.get("delayTime"),
        "execution_time_ms": job.get("executionTime"),
        "output_ok": ok,
        "output": output,
    }
    if not ok:
        raise RuntimeError(f"{label} returned no ok=true output: {str(output)[:300]}")
    return result


def compute_costs(cold: dict[str, Any], warm: dict[str, Any], rate: dict[str, Any]) -> dict[str, Any]:
    """Estimate only. Worker billable time is not the same as request latency."""
    observed = 0.0
    for job in (cold, warm):
        observed += (job.get("delay_time_ms") or 0) / 1000 + (job.get("execution_time_ms") or 0) / 1000
    idle_sec = 5  # endpoint idleTimeout, added once for the final worker
    seconds = observed + idle_sec
    low = rate["community_usd_per_hour"] / 3600 * seconds
    high = rate["secure_usd_per_hour"] / 3600 * seconds
    return {
        "workers_launched": 1 if (cold.get("delay_time_ms") or 0) > 1000 else "unverified",
        "observed_job_seconds": round(observed, 3),
        "estimated_worker_seconds_incl_idle": round(seconds, 3),
        "estimated_usd_range": [round(low, 5), round(high, 5)],
        "basis": "delayTime+executionTime per job plus idleTimeout, at public on-demand GPU price. Estimate, not billing data.",
        "rate": rate,
    }


def main(dry_run: bool) -> int:
    global DRY_RUN
    DRY_RUN = dry_run
    load_env_file()
    if not dry_run and REPORT_PATH.exists():
        print(f"Refusing to run: {REPORT_PATH} already exists. Move it aside first.")
        return 2

    try:
        stage("pre_validation_image_digest", "docker buildx imagetools inspect (remote tag)", check_image_digest)
        snapshot = stage("pre_validation_endpoint_state", "RunPod GET /serverless/{id}", check_endpoint_pre_state)
        report["endpoint_before"] = snapshot
        report["costs"] = {"rate": stage("pre_validation_gpu_rate", "RunPod GraphQL gpuTypes", fetch_gpu_rate)}
        log(f"pre-validation OK. Endpoint before: {json.dumps(snapshot, default=str)[:400]}")
        if dry_run:
            print("DRY RUN: checks passed. No PATCH, no GPU jobs, no report written.")
            return 0

        stage("endpoint_update_patch", "RunPod PATCH /serverless/{id} body={image}",
              lambda: patch_serverless_endpoint(ENDPOINT, {"image": IMAGE}))
        report["accept_config_sec"] = stage(
            "endpoint_accepts_config", "RunPod GET poll until image == v1.0.4",
            wait_for_config_accepted,
        )
        models = load_saved_serverless_models()
        # Cold request: first job after the update. Whether a new worker starts is read from delayTime.
        report["cold_request"] = stage(
            "cold_request_end_to_end", "submit to COMPLETED (wall clock) + RunPod job status",
            lambda: run_warmup_job("cold", models),
        )
        cold = report["cold_request"]
        stage_delay_ms = cold.get("delay_time_ms")
        report["stages"].append({
            "name": "worker_allocation_and_startup",
            "source": "RunPod job status delayTime (queue wait incl. allocation, pull and init combined)",
            "start_utc": cold["submit_utc"], "end_utc": None,
            "duration_sec": round(stage_delay_ms / 1000, 3) if stage_delay_ms is not None else None,
            "success": True, "error": None,
        })
        null_stage("container_image_pull", "RunPod does not expose pull duration per worker")
        null_stage("container_initialization", "Not exposed separately from model load")
        report["stages"].append({
            "name": "model_download_and_load",
            "source": "RunPod job status executionTime (warmup handler, incl. Whisper, Ollama, NLLB load)",
            "start_utc": None, "end_utc": None,
            "duration_sec": round((cold.get("execution_time_ms") or 0) / 1000, 3),
            "success": True, "error": None,
        })
        null_stage("worker_readiness", "Not exposed; readiness is implied by job start")
        warm = stage("warm_request", "second warmup job submitted right after cold (wall clock)",
                     lambda: run_warmup_job("warm", models))
        report["warm_request"] = warm
        report["warm_request"]["worker_reused_evidence"] = (warm.get("delay_time_ms") or 0) < 1000
        null_stage("worker_shutdown", "Not performed. Endpoint idleTimeout=5s handles termination; no settings changed")

        report["costs"] = compute_costs(cold, warm, report["costs"]["rate"])
        report["status"] = "success"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        log(f"FAILED: {exc}")
        return 1
    finally:
        if not dry_run:
            write_report()
    log(f"DONE status={report['status']} report={REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
