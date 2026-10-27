"""User-facing live status for a RunPod serverless endpoint, derived only from read-only provider data.

Inputs: the endpoint settings (GET /v2/serverless/{id}, workers.max) and the endpoint health
(GET /v2/{id}/health). The documented health fields are jobs.{completed, failed, inProgress, inQueue, retried}
and workers.{idle, running}.

The documentation does not say whether workers.idle and workers.running are mutually exclusive, or whether they count
distinct workers. So they are shown as separately reported counts and are never added together. Their meaning is not
documented beyond the field names, so no state claims more than the counts show.

Inferred states are labelled as inferences in their explanations. A worker being reported is never presented as proof
that a video is being processed.
"""

from __future__ import annotations

from typing import Any, Optional

NOT_DEPLOYED = "not_deployed"
UNAVAILABLE = "unavailable"
IDLE = "idle"
RUNNING = "running"
WORKERS_IDLE_ONLY = "workers_idle_only"
WAITING_CAPACITY = "waiting_for_capacity"
QUEUED_MAX_ZERO = "queued_max_zero"

_LABELS = {
    NOT_DEPLOYED: "Not deployed",
    UNAVAILABLE: "Status unavailable",
    IDLE: "Idle — no workers running",
    RUNNING: "Running",
    WORKERS_IDLE_ONLY: "Workers reported idle",
    WAITING_CAPACITY: "Waiting for worker capacity",
    QUEUED_MAX_ZERO: "Queued while max workers is 0",
}

_TONES = {
    NOT_DEPLOYED: "neutral",
    UNAVAILABLE: "amber",
    IDLE: "gray",
    RUNNING: "green",
    WORKERS_IDLE_ONLY: "gray",
    WAITING_CAPACITY: "blue",
    QUEUED_MAX_ZERO: "amber",
}


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _result(key: str, explanation: str, *, checked_at: str, max_workers: Optional[int],
            workers: Optional[dict[str, int]], jobs: Optional[dict[str, int]]) -> dict[str, Any]:
    return {
        "key": key,
        "label": _LABELS[key],
        "tone": _TONES[key],
        "explanation": explanation,
        "max_workers": max_workers,
        "workers": workers,
        "jobs": jobs,
        "checked_at": checked_at,
    }


def map_live_status(
    *,
    endpoint_configured: bool,
    config: Optional[dict[str, Any]],
    config_error: Optional[str],
    health: Optional[dict[str, Any]],
    health_error: Optional[str],
    checked_at: str,
) -> dict[str, Any]:
    """Return the user-facing state. A healthy-looking state needs both reads to succeed."""
    if not endpoint_configured:
        return _result(NOT_DEPLOYED, "No serverless endpoint is saved yet.", checked_at=checked_at,
                       max_workers=None, workers=None, jobs=None)

    workers_cfg = config.get("workers") if isinstance(config, dict) else None
    max_workers = _count(workers_cfg.get("max")) if isinstance(workers_cfg, dict) else None
    if config_error or max_workers is None:
        return _result(UNAVAILABLE,
                       "Could not read the deployment settings. Nothing has been changed.",
                       checked_at=checked_at, max_workers=None, workers=None, jobs=None)

    if health_error or not isinstance(health, dict):
        return _result(UNAVAILABLE,
                       "Could not read live worker data. A failed check does not mean the service is down.",
                       checked_at=checked_at, max_workers=max_workers, workers=None, jobs=None)

    raw_workers = health.get("workers")
    raw_jobs = health.get("jobs")
    if not isinstance(raw_workers, dict) or not isinstance(raw_jobs, dict):
        return _result(UNAVAILABLE, "Worker data was in an unexpected format.",
                       checked_at=checked_at, max_workers=max_workers, workers=None, jobs=None)
    idle = _count(raw_workers.get("idle"))
    running = _count(raw_workers.get("running"))
    in_queue = _count(raw_jobs.get("inQueue"))
    in_progress = _count(raw_jobs.get("inProgress"))
    if None in (idle, running, in_queue, in_progress):
        return _result(UNAVAILABLE, "Worker data was in an unexpected format.",
                       checked_at=checked_at, max_workers=max_workers, workers=None, jobs=None)

    workers = {"idle": idle, "running": running}
    jobs = {"inQueue": in_queue, "inProgress": in_progress}

    if in_queue > 0 and idle == 0 and running == 0:
        if max_workers == 0:
            return _result(
                QUEUED_MAX_ZERO,
                "Jobs are queued, but max workers is 0, so processing may be blocked until worker capacity "
                "is enabled. This is an inference from the queue count and the endpoint setting.",
                checked_at=checked_at, max_workers=max_workers, workers=workers, jobs=jobs)
        return _result(
            WAITING_CAPACITY,
            "Jobs are queued and no workers are running yet. Waiting for capacity is an inference "
            "from the queue count, not a reported status.",
            checked_at=checked_at, max_workers=max_workers, workers=workers, jobs=jobs)
    if running > 0:
        return _result(
            RUNNING,
            f"{running} worker(s) running. That does not by itself show that a video is being processed.",
            checked_at=checked_at, max_workers=max_workers, workers=workers, jobs=jobs)
    if idle > 0:
        return _result(
            WORKERS_IDLE_ONLY,
            f"{idle} idle worker(s) and none running. What 'idle' covers is not documented.",
            checked_at=checked_at, max_workers=max_workers, workers=workers, jobs=jobs)
    return _result(
        IDLE,
        "No workers are reported and no jobs are queued. Zero workers is normal: serverless workers scale to zero.",
        checked_at=checked_at, max_workers=max_workers, workers=workers, jobs=jobs)
