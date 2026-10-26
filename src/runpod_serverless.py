"""RunPod Serverless API: deploy, jobs, status (delayTime), workers, volumes."""

from __future__ import annotations
from src.log_safety import redact

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from src.gpu_selection import load_env_file
from src.runpod_serverless_endpoint_config import (
    CONTAINER_DISK_GB,
    ENDPOINT_NAME_PREFIX,
    EXECUTION_TIMEOUT_MS,
    FLASHBOOT,
    NETWORK_VOLUME_NAME_PREFIX,
    NETWORK_VOLUME_SIZE_GB,
    NETWORK_VOLUME_TYPE,
    endpoint_env_vars,
    scaling_block,
    workers_block,
)
from src.runpod_serverless_payload import (
    build_heavy_phase_input,
    build_highlight_phase_input,
    build_nllb_batch_input,
    decode_gzip_b64_to_path,
)

RUNPOD_API_BASE = "https://api.runpod.io/v2"
RUNPOD_INFERENCE_BASE = "https://api.runpod.ai/v2"
RUNPOD_CONSOLE_SERVERLESS = "https://www.runpod.io/console/serverless/{endpoint_id}"

TERMINAL_JOB_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})

SERVERLESS_IMAGE_ERROR = (
    "RUNPOD_SERVERLESS_IMAGE must be set to your pushed worker Docker image "
    "(see runpod_worker/Dockerfile and docs/RUNPOD_SERVERLESS.md)"
)


def serverless_image_registry_ready(image: str) -> bool:
    """RunPod must pull from a registry (image ref must include a repository path)."""
    img = (image or "").strip()
    if not img or img.startswith("your-registry/"):
        return False
    return "/" in img


def serverless_worker_image() -> str:
    """Resolved worker image from .env, or raises with a clear deploy error."""
    load_env_file()
    image = os.environ.get("RUNPOD_SERVERLESS_IMAGE", "").strip()
    if not serverless_image_registry_ready(image):
        raise RuntimeError(SERVERLESS_IMAGE_ERROR)
    return image


def _api_key() -> str:
    load_env_file()
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RuntimeError("RUNPOD_API_KEY missing in .env")
    return key


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, Any]]:
    url = f"{RUNPOD_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw and resp.status in (200, 204):
                return resp.status, {}
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            detail = {"detail": raw or exc.reason}
        return exc.code, detail


def _gpu_pools_from_env() -> list[str]:
    raw = os.environ.get("RUNPOD_SERVERLESS_GPU_POOLS", "ADA_24,AMPERE_16").strip()
    pools = [p.strip() for p in raw.split(",") if p.strip()]
    if not pools:
        raise RuntimeError("RUNPOD_SERVERLESS_GPU_POOLS must list at least one GPU pool")
    return pools


def _data_center_id() -> str:
    dc = os.environ.get("RUNPOD_DATA_CENTER_ID", "").strip()
    if not dc:
        raise RuntimeError(
            "RUNPOD_DATA_CENTER_ID is required for serverless deploy (network volume is DC-pinned)"
        )
    return dc


def create_network_volume(*, name_suffix: str = "", data_center_id: str | None = None) -> dict[str, Any]:
    dc = (data_center_id or "").strip() or _data_center_id()
    name = f"{NETWORK_VOLUME_NAME_PREFIX}{name_suffix}".strip("-")
    body = {
        "name": name,
        "dataCenter": dc,
        "size": NETWORK_VOLUME_SIZE_GB,
        "type": NETWORK_VOLUME_TYPE,
    }
    status, payload = _request("POST", "/network-volumes", body=body)
    if status not in (200, 201):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        raise RuntimeError(f"Create network volume failed (HTTP {status}): {detail}")
    return payload


def delete_network_volume(volume_id: str) -> None:
    volume_id = volume_id.strip()
    if not volume_id:
        raise RuntimeError("network_volume_id is required")
    status, payload = _request("DELETE", f"/network-volumes/{volume_id}")
    if status not in (200, 204):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        raise RuntimeError(f"Delete network volume failed (HTTP {status}): {detail}")


def endpoint_name_for(gpu_offer_name: str = "") -> str:
    """Deterministic endpoint name for a GPU, e.g. 'video-summarizer-RTX A5000'."""
    name = os.environ.get("RUNPOD_SERVERLESS_NAME", ENDPOINT_NAME_PREFIX)
    if gpu_offer_name:
        name = f"{name}-{gpu_offer_name}"[:63].rstrip("-")
    return name


def list_serverless_endpoints() -> list[dict[str, Any]]:
    """Read-only: every serverless endpoint on the account."""
    status, payload = _request("GET", "/serverless")
    if status != 200:
        detail = payload.get("detail") or payload.get("title") or f"HTTP {status}"
        raise RuntimeError(f"List serverless endpoints failed (HTTP {status}): {detail}")
    return list(payload.get("endpoints") or [])


def find_serverless_endpoint(*, endpoint_id: str = "", name: str = "") -> dict[str, Any] | None:
    """Discover an existing endpoint: first by saved id, then by name. Never creates anything."""
    if endpoint_id:
        ep, _err = get_serverless_endpoint(endpoint_id)
        if ep and str(ep.get("id") or "") == endpoint_id:
            return ep
    if name:
        for ep in list_serverless_endpoints():
            if ep.get("name") == name:
                return ep
    return None


def unique_endpoint_name(base: str, taken: set[str]) -> str:
    """Return base if free; otherwise base with a UTC timestamp suffix. Used only when creating new."""
    if base not in taken:
        return base
    suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    return f"{base}-{suffix}"[:63].rstrip("-")


def create_serverless_endpoint(
    *,
    network_volume_id: str,
    gpu_offer_name: str = "",
    data_center_id: str | None = None,
    gpu_pools: list[str] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    image = serverless_worker_image()
    dc = (data_center_id or "").strip() or _data_center_id()
    pools = gpu_pools if gpu_pools else _gpu_pools_from_env()
    name = name or endpoint_name_for(gpu_offer_name)

    body: dict[str, Any] = {
        "name": name,
        "image": image,
        "type": "QUEUE",
        "gpu": {"pools": pools, "count": 1},
        # Created at max_workers=0: no worker can start until a job holds a lease and raises capacity.
        "workers": {"min": 0, "max": 0, "idleTimeout": workers_block()["idleTimeout"]},
        "scaling": scaling_block(),
        "dataCenterIds": [dc],
        "timeout": EXECUTION_TIMEOUT_MS,
        "flashboot": FLASHBOOT,
        "disk": CONTAINER_DISK_GB,
        "env": endpoint_env_vars(),
        "mounts": {"network": [{"volumeId": network_volume_id}]},
    }
    status, payload = _request("POST", "/serverless", body=body, timeout=180)
    if status not in (200, 201):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        raise RuntimeError(f"Create serverless endpoint failed (HTTP {status}): {detail}")
    return payload


def get_serverless_endpoint(
    endpoint_id: str, *, timeout: float | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    if timeout is None:
        status, payload = _request("GET", f"/serverless/{endpoint_id.strip()}")
    else:
        status, payload = _request("GET", f"/serverless/{endpoint_id.strip()}", timeout=timeout)
    if status == 200:
        return payload, None
    detail = payload.get("detail") or payload.get("title") or f"HTTP {status}"
    return None, str(detail)


def patch_serverless_endpoint(
    endpoint_id: str, body: dict[str, Any], *, timeout: float | None = None
) -> dict[str, Any]:
    if timeout is None:
        status, payload = _request("PATCH", f"/serverless/{endpoint_id.strip()}", body=body)
    else:
        status, payload = _request("PATCH", f"/serverless/{endpoint_id.strip()}", body=body, timeout=timeout)
    if status not in (200, 201):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        raise RuntimeError(f"Update serverless endpoint failed (HTTP {status}): {detail}")
    return payload


def stop_serverless_workers(endpoint_id: str) -> dict[str, Any]:
    """Scale workers to zero (stop billing for running workers)."""
    return patch_serverless_endpoint(
        endpoint_id,
        {"workers": {"min": 0, "max": 0, "idleTimeout": workers_block()["idleTimeout"]}},
    )


class EndpointPausedError(RuntimeError):
    """RunPod rejected a job because the endpoint has max_workers=0. The job was not accepted."""


class EndpointNotReadyError(RuntimeError):
    """The endpoint stayed paused past the readiness timeout. No job was accepted."""


JOB_ACCEPT_TIMEOUT_SEC = 240.0
JOB_ACCEPT_RETRY_SEC = 10.0


def _enable_workers_if_paused(endpoint_id: str, log: Callable[[str], None]) -> bool:
    """Raise max workers only when the endpoint is paused. Returns True if this call changed it."""
    live, err = get_serverless_endpoint(endpoint_id)
    if err or not live:
        log(f"Could not read endpoint before submit ({redact(err)}); submitting and letting RunPod decide.")
        return False
    if (live.get("workers") or {}).get("max", 0) > 0:
        return False
    patch_serverless_endpoint(endpoint_id, {"workers": workers_block()})
    log("Endpoint was paused (max workers 0); raised max workers to restore capacity.")
    return True


def submit_job_when_ready(
    endpoint_id: str,
    job_input: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
    timeout_sec: float = JOB_ACCEPT_TIMEOUT_SEC,
    retry_sec: float = JOB_ACCEPT_RETRY_SEC,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Submit one job, waiting for a paused endpoint to start accepting jobs.

    Only an explicit 'paused' rejection (HTTP 409) is retried. Any other error is raised at once, and a
    timeout is raised as EndpointNotReadyError. A job RunPod accepted is never resubmitted.

    This function does not lower capacity on failure. The caller holds a job lease (endpoint_capacity.job_lease),
    and the lease releases capacity only when no other job from this app is active or pending.
    """
    _log = log or (lambda _m: None)
    _enable_workers_if_paused(endpoint_id, _log)
    deadline = clock() + timeout_sec
    attempt = 0
    while True:
        attempt += 1
        try:
            job_id = submit_job(endpoint_id, job_input)
        except EndpointPausedError as exc:
            if clock() >= deadline:
                raise EndpointNotReadyError(
                    f"Endpoint still paused after {attempt} submit attempt(s) over {timeout_sec:.0f}s; "
                    f"no job was accepted. Last RunPod response: {exc}"
                ) from exc
            _log(f"Endpoint not accepting jobs yet (attempt {attempt}); retrying in {retry_sec:.0f}s.")
            sleep(retry_sec)
            continue
        _log(f"RunPod accepted the job on attempt {attempt}: {job_id}")
        return job_id


def submit_and_wait(
    endpoint_id: str,
    job_input: dict[str, Any],
    *,
    label: str,
    log: Callable[[str], None] | None = None,
    max_wait_sec: float = 7200,
) -> dict[str, Any]:
    """Submit one job and poll it to completion while holding a capacity lease.

    The lease covers submission and polling, and it is released on success, failure, and timeout. Capacity
    goes back to 0 only when no other job from this app is still active or pending on the endpoint.
    """
    from src.endpoint_capacity import job_lease

    _log = log or (lambda _m: None)
    with job_lease(endpoint_id, label, _log):
        job_id = submit_job_when_ready(endpoint_id, job_input, log=_log)
        _log(f"RunPod {label} job submitted: {job_id}")
        result = poll_job_until_done(endpoint_id, job_id, log=_log, max_wait_sec=max_wait_sec)
        # Always carry the ID that RunPod accepted, so callers never need a separate variable for it.
        result.setdefault("id", job_id)
        return result


def build_warmup_input(*, whisper_model: str, ollama_model: str, nllb_model: str) -> dict[str, Any]:
    return {
        "job_type": "warmup",
        "whisper_model": whisper_model,
        "summarization_model": ollama_model,
        "ollama_model": ollama_model,
        "nllb_model": nllb_model,
    }


def run_warmup_cold_start(
    endpoint_id: str,
    *,
    whisper_model: str,
    ollama_model: str,
    nllb_model: str,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Submit warmup job to load Whisper, Ollama, and NLLB on a cold worker."""
    _log = log or (lambda _m: None)
    job_input = build_warmup_input(
        whisper_model=whisper_model,
        ollama_model=ollama_model,
        nllb_model=nllb_model,
    )
    return submit_and_wait(endpoint_id, job_input, label="warmup", log=_log)


def detach_network_volume_from_endpoint(endpoint_id: str) -> dict[str, Any]:
    return patch_serverless_endpoint(
        endpoint_id,
        {"mounts": {"network": []}, "networkVolumes": []},
    )


def _inference_request(
    method: str,
    endpoint_id: str,
    subpath: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, Any]]:
    url = f"{RUNPOD_INFERENCE_BASE}/{endpoint_id.strip()}{subpath}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw and resp.status in (200, 204):
                return resp.status, {}
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            detail = {"detail": raw or exc.reason}
        return exc.code, detail


def submit_job(endpoint_id: str, job_input: dict[str, Any]) -> str:
    status, payload = _inference_request(
        "POST",
        endpoint_id,
        "/run",
        body={"input": job_input},
        timeout=60,
    )
    if status not in (200, 201):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        if status == 409 and "paused" in str(detail).lower():
            # Rejected, not accepted: safe to retry without risking a duplicate job.
            raise EndpointPausedError(f"RunPod /run rejected: endpoint paused (HTTP 409): {detail}")
        raise RuntimeError(f"RunPod /run failed (HTTP {status}): {detail}")
    job_id = payload.get("id")
    if not job_id:
        raise RuntimeError(f"RunPod /run returned no job id: {payload}")
    return str(job_id)


def get_serverless_health(endpoint_id: str, *, timeout: float = 10.0) -> tuple[dict[str, Any] | None, str | None]:
    """Read-only: GET /v2/{id}/health (job and worker counts). Never changes the endpoint."""
    try:
        status, payload = _inference_request("GET", endpoint_id, "/health", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — the caller reports 'unavailable'; the message stays short
        return None, f"health check failed ({type(exc).__name__})"
    if status != 200:
        return None, f"health check returned HTTP {status}"
    return payload, None


def get_job_status(endpoint_id: str, job_id: str) -> dict[str, Any]:
    status, payload = _inference_request(
        "GET",
        endpoint_id,
        f"/status/{job_id.strip()}",
        timeout=60,
    )
    if status != 200:
        detail = payload.get("detail") or payload.get("title") or f"HTTP {status}"
        raise RuntimeError(f"RunPod /status failed: {detail}")
    return payload


def poll_job_until_done(
    endpoint_id: str,
    job_id: str,
    *,
    log: Callable[[str], None] | None = None,
    poll_interval_sec: float = 2.0,
    max_wait_sec: float = 7200,
) -> dict[str, Any]:
    """Poll job status; log delayTime/executionTime when available."""
    deadline = time.monotonic() + max_wait_sec
    last_status = ""
    while time.monotonic() < deadline:
        data = get_job_status(endpoint_id, job_id)
        st = data.get("status") or ""
        if st != last_status and log:
            log(f"RunPod job {job_id}: {st}")
            last_status = st
        if st in TERMINAL_JOB_STATUSES:
            delay_ms = data.get("delayTime")
            exec_ms = data.get("executionTime")
            if log and delay_ms is not None:
                log(
                    f"RunPod metrics — delayTime: {int(delay_ms)} ms "
                    f"({int(delay_ms) / 1000:.1f}s), "
                    f"executionTime: {int(exec_ms or 0)} ms "
                    f"({int(exec_ms or 0) / 1000:.1f}s)"
                )
            if st != "COMPLETED":
                err = data.get("error") or data
                raise RuntimeError(f"RunPod job {job_id} ended with {st}: {err}")
            return data
        time.sleep(poll_interval_sec)
    raise RuntimeError(f"RunPod job {job_id} timed out after {max_wait_sec}s")


def _write_artifacts_from_output(job_output_dir: Path, output: Any) -> None:
    if not isinstance(output, dict):
        raise RuntimeError("Serverless job output must be a JSON object")
    job_output_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "transcript": "transcript.txt",
        "segments_json": "segments.json",
        "summary": "summary.txt",
        "summary_segments_json": "summary_segments.json",
        "summary_translations_json": "summary_translations.json",
        "heavy_done_json": "heavy_done.json",
    }
    for key, filename in mapping.items():
        if key not in output:
            continue
        val = output[key]
        path = job_output_dir / filename
        if key.endswith("_json"):
            if isinstance(val, str):
                path.write_text(val, encoding="utf-8")
            else:
                path.write_text(json.dumps(val, indent=2), encoding="utf-8")
        else:
            path.write_text(str(val), encoding="utf-8")


def _write_highlight_artifacts_from_output(job_output_dir: Path, output: Any) -> None:
    if not isinstance(output, dict):
        raise RuntimeError("Serverless highlight output must be a JSON object")
    job_output_dir.mkdir(parents=True, exist_ok=True)
    status_raw = output.get("highlight_status_json")
    if status_raw:
        status_path = job_output_dir / "highlights" / "highlight_status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(status_raw, str):
            status_path.write_text(status_raw, encoding="utf-8")
        else:
            status_path.write_text(json.dumps(status_raw, indent=2), encoding="utf-8")
    highlights = output.get("highlights_gzip_b64")
    if not isinstance(highlights, dict):
        return
    from src.highlight_jobs import highlight_file_for_lang

    for lang, b64 in highlights.items():
        if not b64 or not isinstance(lang, str):
            continue
        code = lang.strip().lower()
        dest = highlight_file_for_lang(job_output_dir, code)
        decode_gzip_b64_to_path(str(b64), dest)


def run_highlight_phase_via_serverless(
    *,
    endpoint_id: str,
    job_id: str,
    video_path: Path,
    segments_json: str,
    job_output_dir: Path,
    summarization_model: str,
    compression: str,
    languages: str,
    log: Callable[[str], None],
    video_url: str | None = None,
) -> dict[str, Any]:
    """Submit highlight_phase job (FFmpeg + TTS on GPU worker), poll, write MP4s locally."""
    log("Highlight video: RunPod Serverless (FFmpeg, TTS, scene assembly on GPU worker)")
    job_input = build_highlight_phase_input(
        video_path=video_path,
        segments_json=segments_json,
        languages=languages,
        job_id=job_id,
        summarization_model=summarization_model,
        compression=compression,
        log=log,
        video_url=video_url,
    )
    result = submit_and_wait(endpoint_id, job_input, label="highlight", log=log)
    output = result.get("output")
    if output is None:
        raise RuntimeError("RunPod highlight job completed with no output")
    _write_highlight_artifacts_from_output(job_output_dir, output)
    highlights = output.get("highlights_gzip_b64") if isinstance(output, dict) else None
    if not isinstance(highlights, dict) or not highlights:
        raise RuntimeError("Serverless highlight job did not produce highlight MP4s")
    return result


def run_heavy_phase_via_serverless(
    *,
    endpoint_id: str,
    job_id: str,
    audio_path: Path,
    job_output_dir: Path,
    whisper_model: str,
    summarization_model: str,
    compression: str,
    highlight_languages: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Submit heavy phase job, poll, write artifacts locally, return status payload with metrics."""
    log("Heavy steps: RunPod Serverless (Whisper + Ollama + NLLB on GPU worker)")
    job_input = build_heavy_phase_input(
        audio_path=audio_path,
        whisper_model=whisper_model,
        summarization_model=summarization_model,
        compression=compression,
        highlight_languages=highlight_languages,
        job_id=job_id,
        log=log,
    )
    result = submit_and_wait(endpoint_id, job_input, label="heavy", log=log)
    output = result.get("output")
    if output is None:
        raise RuntimeError("RunPod job completed with no output")
    _write_artifacts_from_output(job_output_dir, output)
    if not (job_output_dir / "transcript.txt").is_file():
        raise RuntimeError("Serverless heavy job did not produce transcript.txt")
    metrics = {
        "delayTime": result.get("delayTime"),
        "executionTime": result.get("executionTime"),
        "runpod_job_id": result.get("id"),
    }
    metrics_path = job_output_dir / "runpod_job_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return result


def run_nllb_batch_via_serverless(
    endpoint_id: str,
    language_code: str,
    texts: list[str],
    *,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    if not texts:
        return []
    job_input = build_nllb_batch_input(language_code=language_code, texts=texts)
    result = submit_and_wait(endpoint_id, job_input, label="nllb", log=log)
    output = result.get("output") or {}
    out_texts = output.get("texts") or []
    if len(out_texts) != len(texts):
        raise RuntimeError("RunPod NLLB batch returned unexpected text count")
    return [str(t) for t in out_texts]


def save_job_metrics_to_selection(metrics: dict[str, Any]) -> None:
    from src.compute_deploy import load_saved_selection, _write_selection

    saved = load_saved_selection() or {}
    deploy = saved.get("deploy") or {}
    deploy["last_job_metrics"] = metrics
    deploy["updated_at"] = metrics.get("recorded_at")
    saved["deploy"] = deploy
    _write_selection(saved)


def load_endpoint_id_from_selection() -> str:
    from src.compute_deploy import load_saved_selection

    saved = load_saved_selection()
    if not saved:
        raise RuntimeError("No compute selection saved. Deploy from GPU selection first.")
    from src.compute_deploy import saved_serverless_endpoint_id

    endpoint_id = saved_serverless_endpoint_id(saved)
    if not endpoint_id:
        raise RuntimeError("No serverless endpoint_id in saved deploy record")
    return endpoint_id
