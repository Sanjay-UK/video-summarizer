"""Live deployment status from compute_selection.json + RunPod pod API."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from src.compute_deploy import (
    COMPUTE_MODE_SERVERLESS,
    SELECTION_PATH,
    load_compute_mode,
    load_saved_selection,
    mark_saved_deploy_stopped,
)
from src.gpu_selection import load_env_file
from src.pipeline_model_status import get_pipeline_model_status
from src.runpod_platform_controls import platform_controls_status
from src.runpod_serverless_endpoint_config import IDLE_TIMEOUT_SEC, WORKERS_MAX

RUNPOD_PODS_URL = "https://api.runpod.io/v2/pods"
RUNPOD_CONSOLE_POD_URL = "https://www.runpod.io/console/pods/{pod_id}"
RUNPOD_CONSOLE_SERVERLESS_URL = "https://www.runpod.io/console/serverless/{endpoint_id}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_runpod_pod_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # PUBLIC_KEY env may contain raw newlines and break strict JSON.
        out: dict[str, Any] = {}
        for key in ("id", "name", "status"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', raw)
            if match:
                out[key] = match.group(1)
        cost = re.search(r'"cost"\s*:\s*([0-9.]+)', raw)
        if cost:
            out["cost"] = float(cost.group(1))
        proxy_cmd = re.search(
            r'"proxy"\s*:\s*\{[^}]*"command"\s*:\s*"(ssh [^"]+)"',
            raw,
            re.DOTALL,
        )
        direct_cmd = re.search(
            r'"direct"\s*:\s*\{[^}]*"command"\s*:\s*"(ssh [^"]+)"',
            raw,
            re.DOTALL,
        )
        ssh: dict[str, Any] = {}
        if proxy_cmd:
            ssh["proxy"] = {"command": proxy_cmd.group(1)}
        if direct_cmd:
            ssh["direct"] = {"command": direct_cmd.group(1)}
        if ssh:
            out["ssh"] = ssh
        dc = re.search(r'"dataCenterId"\s*:\s*"([^"]+)"', raw)
        if dc:
            out["dataCenterId"] = dc.group(1)
        ip = re.search(r'"publicIp"\s*:\s*"([^"]*)"', raw)
        if ip:
            out["publicIp"] = ip.group(1)
        return out


def _http_get_json(url: str, api_key: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                if "/pods/" in url:
                    return resp.status, _parse_runpod_pod_json(raw)
                raise
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            body = {"detail": raw or exc.reason}
        return exc.code, body


def _phase_from_runpod_status(status: str | None) -> str:
    s = (status or "").upper()
    if s in ("RUNNING",):
        return "running"
    if s in (
        "CREATED",
        "STARTING",
        "INITIALIZING",
        "PENDING",
        "PROVISIONING",
    ):
        return "deploying"
    if s in ("EXITED", "FAILED", "ERROR"):
        return "failed"
    if s in ("TERMINATED", "STOPPED"):
        return "stopped"
    return "unknown"


def _fetch_runpod_pod(pod_id: str, api_key: str) -> tuple[dict[str, Any] | None, str | None]:
    status_code, payload = _http_get_json(f"{RUNPOD_PODS_URL}/{pod_id}", api_key)
    if status_code == 200:
        return payload, None
    detail = payload.get("detail") or payload.get("title") or f"HTTP {status_code}"
    return None, str(detail)


_ACTIVE_BANNER_PHASES = frozenset({"running", "deploying"})


def _label_for_phase(phase: str) -> str:
    return {
        "running": "Running",
        "deploying": "Deploying",
        "failed": "Failed",
        "stopped": "Stopped",
        "manual": "Manual setup",
        "unknown": "Unknown",
    }.get(phase, "Unknown")


def _finalize_deploy_status(out: dict[str, Any]) -> dict[str, Any]:
    """Set banner visibility flags from lifecycle phase (module-level helper)."""
    phase = out.get("phase") or ""
    if (
        phase == "unknown"
        and out.get("deploy_record_status") == "deployed"
        and out.get("pod_id")
    ):
        phase = "deploying"
        out["phase"] = phase
        out["phase_label"] = _label_for_phase(phase)
    if (
        out.get("compute_mode") == COMPUTE_MODE_SERVERLESS
        and out.get("deploy_record_status") == "deployed"
        and out.get("endpoint_id")
    ):
        out["show_deploy_banner"] = True
        out["has_active_pod"] = bool(out.get("workers_max", 0) and out.get("workers_max", 0) > 0)
        return out
    active = phase in _ACTIVE_BANNER_PHASES
    out["show_deploy_banner"] = active
    out["has_active_pod"] = active
    return out


def _apply_last_job_metrics(out: dict[str, Any], deploy: dict[str, Any]) -> None:
    metrics = deploy.get("last_job_metrics") or {}
    if metrics.get("delayTime") is not None:
        out["last_delay_time_ms"] = metrics.get("delayTime")
    if metrics.get("executionTime") is not None:
        out["last_execution_time_ms"] = metrics.get("executionTime")
    out["last_runpod_job_id"] = metrics.get("runpod_job_id")


def _fetch_endpoint_snapshot(endpoint_id: str) -> dict[str, Any]:
    """Read-only provider reads for the status cache: settings first, then health only if settings succeeded."""
    from src.runpod_serverless import get_serverless_endpoint, get_serverless_health

    fetched_at = _utc_now()
    config, config_err = get_serverless_endpoint(endpoint_id, timeout=10)
    if config_err:
        return {"config": None, "config_error": config_err, "health": None,
                "health_error": None, "fetched_at": fetched_at}
    health, health_err = get_serverless_health(endpoint_id, timeout=10)
    return {"config": config, "config_error": None, "health": health,
            "health_error": health_err, "fetched_at": fetched_at}


def _serverless_deploy_status(
    out: dict[str, Any],
    deploy: dict[str, Any],
    saved: dict[str, Any] | None,
    *,
    refresh_live: bool,
) -> dict[str, Any]:
    # Show the endpoint the app will use. A later failed save must not hide a known endpoint.
    from src.compute_deploy import saved_serverless_endpoint_id

    endpoint_id = (deploy.get("endpoint_id") or "").strip() or saved_serverless_endpoint_id(saved)
    out["endpoint_id"] = endpoint_id or None
    out["network_volume_id"] = (deploy.get("network_volume_id") or "").strip() or None
    out["stop_action_label"] = "Stop serverless"
    out["can_delete_volume"] = bool(out.get("network_volume_id"))
    _apply_last_job_metrics(out, deploy)
    out["warmup_status"] = deploy.get("warmup_status")
    out["serverless_models"] = saved.get("serverless_models") if saved else None

    hw_price = float((out.get("hardware") or {}).get("price_per_hour") or 0)
    workers_max = out.get("workers_max")
    if workers_max is None:
        workers_max = WORKERS_MAX
    out["serverless_idle_timeout_sec"] = out.get("workers_idle_timeout") or IDLE_TIMEOUT_SEC
    out["serverless_max_cost_per_hour_usd"] = round(hw_price * float(workers_max or 0), 3)

    deploy_result = out.get("deploy_record_status")
    if deploy_result in ("failed", "pending"):
        return _finalize_deploy_status(out)

    if not endpoint_id:
        out["phase"] = "failed"
        out["phase_label"] = "No endpoint ID"
        return _finalize_deploy_status(out)

    out["console_url"] = RUNPOD_CONSOLE_SERVERLESS_URL.format(endpoint_id=endpoint_id)
    out["can_stop"] = deploy_result == "deployed"
    out["phase"] = "ready"
    out["phase_label"] = "Serverless (scale to zero)"
    out["data_center_id"] = deploy.get("data_center_id") or out.get("data_center_id")

    if not refresh_live:
        return _finalize_deploy_status(out)

    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        out["runpod_error"] = "RUNPOD_API_KEY not set; showing saved serverless deploy only."
        return _finalize_deploy_status(out)

    from src.serverless_live_status import map_live_status
    from src.serverless_status_cache import get_live_snapshot

    snapshot = get_live_snapshot(endpoint_id, fetch=_fetch_endpoint_snapshot)
    live = snapshot.get("config")
    err = snapshot.get("config_error")
    if err:
        out["runpod_error"] = err
    workers = {} if err else ((live or {}).get("workers") or {})
    out["workers_min"] = workers.get("min")
    out["workers_max"] = workers.get("max")
    out["workers_idle_timeout"] = workers.get("idleTimeout")
    out["live"] = map_live_status(
        endpoint_configured=True,
        config=live,
        config_error=err,
        health=snapshot.get("health"),
        health_error=snapshot.get("health_error"),
        checked_at=snapshot.get("fetched_at") or _utc_now(),
    )
    out["phase_label"] = out["live"]["label"]
    if workers.get("max") == 0 and not err:
        out["phase"] = "stopped"
    return _finalize_deploy_status(out)


def get_compute_deploy_status(refresh_live: bool = True) -> dict[str, Any]:
    """Build UI/API payload from saved selection and optional RunPod live poll."""
    load_env_file()
    saved = None
    if SELECTION_PATH.is_file():
        try:
            saved = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = None

    if not saved:
        return {
            "has_deploy": False,
            "show_deploy_banner": False,
            "checked_at": _utc_now(),
            "model_status": get_pipeline_model_status(),
            **platform_controls_status(),
        }

    offer = saved.get("offer") or {}
    deploy = saved.get("deploy") or {}
    provider = offer.get("provider", "")
    pod_id = (deploy.get("pod_id") or "").strip()
    deploy_result = deploy.get("status", "")

    compute_mode = saved.get("compute_mode") or load_compute_mode()

    out: dict[str, Any] = {
        "has_deploy": True,
        "show_deploy_banner": False,
        "checked_at": _utc_now(),
        "compute_mode": compute_mode,
        "selection_id": saved.get("selection_id"),
        "saved_at": saved.get("saved_at"),
        "hardware": {
            "name": offer.get("name"),
            "kind": offer.get("kind"),
            "cloud": offer.get("cloud"),
            "vram_gb": offer.get("vram_gb"),
            "price_per_hour": offer.get("price_per_hour"),
        },
        "deploy_record_status": deploy_result,
        "deploy_message": deploy.get("message"),
        "pod_id": pod_id or None,
        "phase": "unknown",
        "phase_label": "Unknown",
        "runpod_live_status": None,
        "runpod_error": None,
        "cost_per_hour": None,
        "data_center_id": None,
        "ssh_command": None,
        "ssh_host": None,
        "console_url": None,
        "public_ip": None,
        "can_stop": False,
        "can_delete_volume": False,
        "endpoint_id": None,
        "network_volume_id": None,
        "stop_action_label": "Stop pod",
        "last_delay_time_ms": None,
        "last_execution_time_ms": None,
        "last_runpod_job_id": None,
        "model_status": get_pipeline_model_status(),
        **platform_controls_status(),
    }

    if compute_mode == COMPUTE_MODE_SERVERLESS and provider == "runpod":
        # Set before the early returns below, so a failed or pending save still shows the known endpoint.
        from src.compute_deploy import saved_serverless_endpoint_id

        out["endpoint_id"] = saved_serverless_endpoint_id(saved) or None
        if deploy_result == "stopped":
            out["phase"] = "stopped"
            out["phase_label"] = "Stopped"
            _apply_last_job_metrics(out, deploy)
            return _finalize_deploy_status(out)
        if deploy_result == "failed":
            out["phase"] = "failed"
            out["phase_label"] = "Deploy failed"
            return _finalize_deploy_status(out)
        if deploy_result == "pending":
            out["phase"] = "deploying"
            out["phase_label"] = "Deploying serverless"
            return _finalize_deploy_status(out)
        return _serverless_deploy_status(out, deploy, saved, refresh_live=refresh_live)

    if provider == "aws" or deploy_result == "manual":
        out["phase"] = "manual"
        out["phase_label"] = "Manual (AWS)"
        return _finalize_deploy_status(out)

    if deploy_result == "stopped":
        out["phase"] = "stopped"
        out["phase_label"] = "Stopped"
        out["runpod_live_status"] = deploy.get("runpod_live_status") or "TERMINATED"
        return _finalize_deploy_status(out)

    if deploy_result == "failed":
        out["phase"] = "failed"
        out["phase_label"] = "Deploy failed"
        return _finalize_deploy_status(out)

    if deploy_result == "pending":
        out["phase"] = "deploying"
        out["phase_label"] = "Deploying"
        return _finalize_deploy_status(out)

    if not pod_id:
        out["phase"] = "failed"
        out["phase_label"] = "No pod ID"
        return _finalize_deploy_status(out)

    out["console_url"] = RUNPOD_CONSOLE_POD_URL.format(pod_id=pod_id)
    out["can_stop"] = deploy_result == "deployed"

    # Seed from create response if present
    create_resp = deploy.get("provider_response") or {}
    if create_resp.get("status"):
        out["runpod_live_status"] = create_resp.get("status")
        out["phase"] = _phase_from_runpod_status(create_resp.get("status"))
    if create_resp.get("cost") is not None:
        out["cost_per_hour"] = create_resp.get("cost")
    ssh = create_resp.get("ssh") or {}
    proxy = ssh.get("proxy") or {}
    if proxy.get("command"):
        out["ssh_command"] = proxy.get("command")
        out["ssh_host"] = proxy.get("host")

    out.update(platform_controls_status())
    if not refresh_live:
        out["phase_label"] = _label_for_phase(out["phase"])
        return _finalize_deploy_status(out)

    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        out["runpod_error"] = "RUNPOD_API_KEY not set; showing last saved create response only."
        out["phase_label"] = _label_for_phase(out["phase"])
        return _finalize_deploy_status(out)

    live, err = _fetch_runpod_pod(pod_id, api_key)
    if err:
        out["runpod_error"] = err
        if "404" in err or "not found" in err.lower():
            mark_saved_deploy_stopped(
                "The pod is no longer available.",
                live_status="TERMINATED",
            )
            out["deploy_record_status"] = "stopped"
            out["deploy_message"] = "The pod is no longer available."
            out["phase"] = "stopped"
            out["runpod_live_status"] = "TERMINATED"
        out["can_stop"] = False
        out["phase_label"] = _label_for_phase(out["phase"])
        return _finalize_deploy_status(out)

    live_status = live.get("status") if live else None
    out["runpod_live_status"] = live_status
    out["phase"] = _phase_from_runpod_status(live_status)
    out["phase_label"] = _label_for_phase(out["phase"])
    out["can_stop"] = out["phase"] in ("running", "deploying")
    if live:
        out["cost_per_hour"] = live.get("cost", out["cost_per_hour"])
        out["data_center_id"] = live.get("dataCenterId")
        out["public_ip"] = live.get("publicIp")
        ssh = live.get("ssh") or {}
        proxy = ssh.get("proxy") or {}
        if proxy.get("command"):
            out["ssh_command"] = proxy.get("command")
            out["ssh_host"] = proxy.get("host")

    return _finalize_deploy_status(out)
