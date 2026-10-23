"""Persist compute selection and trigger provider deploy."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.gpu_selection import (
    build_gpu_selection_report,
    load_env_file,
    parse_selection_id,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
SELECTION_PATH = PROJECT_DIR / "data" / "compute_selection.json"
RUNPOD_PODS_URL = "https://api.runpod.io/v2/pods"
DEFAULT_RUNPOD_IMAGE = (
    "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_saved_selection() -> dict[str, Any] | None:
    if not SELECTION_PATH.is_file():
        return None
    try:
        return json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_selection(payload: dict[str, Any]) -> None:
    SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELECTION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _find_offer_in_catalog(selection_id: str) -> dict[str, Any] | None:
    report = build_gpu_selection_report()
    for key in ("gpu_offers", "cpu_offers"):
        for row in report.get(key) or []:
            if row.get("selection_id") == selection_id:
                return row
    return None


def _resolve_offer(
    selection_id: str,
    client_offer: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return offer for deploy and whether it came from live in-stock catalog."""
    selection_id = (selection_id or "").strip()
    if client_offer and client_offer.get("selection_id") == selection_id:
        live = _find_offer_in_catalog(selection_id)
        if live:
            return live, True
        merged = {**client_offer, "selection_id": selection_id}
        return merged, False

    live = _find_offer_in_catalog(selection_id)
    if live:
        return live, True

    parsed = parse_selection_id(selection_id)
    if parsed:
        return parsed, False
    return None, False


def _http_delete(url: str, api_key: str) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            detail = {"detail": raw or exc.reason}
        return exc.code, detail


def _http_post_json(url: str, api_key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            detail = {"detail": raw or exc.reason}
        return exc.code, detail


def _runpod_cloud_type(cloud: str) -> str:
    if "community" in cloud:
        return "COMMUNITY"
    return "SECURE"


def _runpod_ports() -> list[str]:
    """RunPod v2 expects ports as a JSON array, e.g. ["8888/http", "22/tcp"]."""
    raw = os.environ.get(
        "RUNPOD_POD_PORTS",
        "8888/http,22/tcp,8765/http",
    ).strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts


def _build_runpod_pod_body(offer: dict[str, Any]) -> dict[str, Any]:
    """Build POST /v2/pods body (GraphQL-era field names are rejected with 422)."""
    image = os.environ.get("RUNPOD_POD_IMAGE", DEFAULT_RUNPOD_IMAGE)
    cloud = _runpod_cloud_type(offer.get("cloud", ""))
    body: dict[str, Any] = {
        "name": os.environ.get("RUNPOD_POD_NAME", "video-summarizer"),
        "image": image,
        "disk": int(os.environ.get("RUNPOD_POD_DISK_GB", "40")),
    }
    ports = _runpod_ports()
    if ports:
        body["ports"] = ports
    body["startSsh"] = os.environ.get("RUNPOD_START_SSH", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    body["startJupyter"] = os.environ.get("RUNPOD_START_JUPYTER", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    # Secure is default when omitted; Community must be set explicitly.
    if cloud == "COMMUNITY":
        body["cloud"] = "COMMUNITY"

    gpu_id = (offer.get("resource_id") or "").strip()
    if not gpu_id and offer.get("kind") == "gpu":
        raise ValueError("Missing GPU resource_id for RunPod deploy")

    if offer.get("kind") == "gpu":
        body["gpu"] = {"id": gpu_id, "count": 1}
    else:
        vcpus = int(offer.get("vcpus") or offer.get("vcpu_min") or 2)
        cpu_id = (offer.get("resource_id") or "").strip()
        if not cpu_id:
            raise ValueError("Missing CPU flavor resource_id for RunPod deploy")
        body["cpu"] = {"id": cpu_id, "vcpuCount": vcpus}
    return body


def _deploy_runpod(offer: dict[str, Any], api_key: str) -> dict[str, Any]:
    cloud_type = _runpod_cloud_type(offer.get("cloud", ""))
    try:
        body = _build_runpod_pod_body(offer)
    except ValueError as exc:
        return {
            "status": "failed",
            "message": str(exc),
            "pod_id": "",
            "provider_response": {},
        }

    status, payload = _http_post_json(RUNPOD_PODS_URL, api_key, body)
    if status == 201:
        pod_id = payload.get("id") or payload.get("podId") or ""
        return {
            "status": "deployed",
            "message": f"RunPod pod created ({payload.get('cloud', cloud_type)}).",
            "pod_id": pod_id,
            "provider_response": payload,
            "request_body": body,
        }
    errors = payload.get("errors")
    detail = payload.get("detail") or payload.get("title") or str(payload)
    if errors:
        detail = f"{detail} ({'; '.join(str(e) for e in errors)})"
    return {
        "status": "failed",
        "message": f"RunPod deploy failed (HTTP {status}): {detail}",
        "pod_id": "",
        "provider_response": payload,
        "request_body": body,
    }


def _deploy_aws(offer: dict[str, Any]) -> dict[str, Any]:
    instance = offer.get("resource_id") or offer.get("name")
    return {
        "status": "manual",
        "message": (
            f"AWS selection saved. Launch instance type {instance} in EC2 "
            f"(on-demand ~${offer.get('price_per_hour', '?')}/hr). "
            "Automated EC2 create is not enabled in this demo."
        ),
        "pod_id": "",
        "provider_response": {"instance_type": instance},
    }


def mark_saved_deploy_stopped(
    reason: str,
    *,
    live_status: str = "TERMINATED",
) -> bool:
    """Persist stopped/terminated state when RunPod no longer has the pod."""
    saved = load_saved_selection()
    if not saved:
        return False
    deploy = saved.get("deploy") or {}
    if deploy.get("status") == "stopped":
        return False
    deploy["status"] = "stopped"
    deploy["message"] = reason
    deploy["updated_at"] = _utc_now()
    deploy["runpod_live_status"] = live_status
    saved["deploy"] = deploy
    _write_selection(saved)
    return True


def stop_saved_runpod_pod() -> dict[str, Any]:
    """Terminate the RunPod pod from the saved selection file (DELETE /v2/pods/{id})."""
    load_env_file()
    saved = load_saved_selection()
    if not saved:
        return {"ok": False, "error": "No saved deployment in data/compute_selection.json"}

    offer = saved.get("offer") or {}
    if offer.get("provider") != "runpod":
        return {"ok": False, "error": "Saved selection is not a RunPod pod"}

    deploy = saved.get("deploy") or {}
    pod_id = (deploy.get("pod_id") or "").strip()
    if not pod_id:
        return {"ok": False, "error": "No pod_id in saved deploy record"}

    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "RUNPOD_API_KEY missing in .env"}

    status, payload = _http_delete(f"{RUNPOD_PODS_URL}/{pod_id}", api_key)
    if status not in (200, 204):
        detail = payload.get("detail") or payload.get("title") or str(payload)
        return {
            "ok": False,
            "error": f"RunPod terminate failed (HTTP {status}): {detail}",
            "provider_response": payload,
        }

    deploy["status"] = "stopped"
    deploy["message"] = f"Pod {pod_id} terminated."
    deploy["updated_at"] = _utc_now()
    deploy["runpod_live_status"] = "TERMINATED"
    saved["deploy"] = deploy
    _write_selection(saved)

    return {"ok": True, "pod_id": pod_id, "message": deploy["message"]}


def save_selection_and_deploy(
    selection_id: str,
    client_offer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save one GPU or CPU choice and run the provider deploy flow."""
    load_env_file()
    selection_id = (selection_id or "").strip()
    if not selection_id:
        return {"ok": False, "error": "selection_id is required"}

    offer, in_stock = _resolve_offer(selection_id, client_offer)
    if not offer:
        return {
            "ok": False,
            "error": "Unknown selection_id format. Refresh the page and pick a row again.",
        }
    catalog_note = None
    if not in_stock:
        catalog_note = (
            "This GPU/CPU is not in the latest in-stock catalog snapshot; "
            "deploying using the selection_id you chose."
        )

    saved_at = _utc_now()
    prior = load_saved_selection() or {}
    record: dict[str, Any] = {
        "selection_id": selection_id,
        "saved_at": saved_at,
        "offer": offer,
        "in_stock_at_save": in_stock,
        "catalog_note": catalog_note,
        "pipeline_runtime": prior.get("pipeline_runtime") or "local",
        "deploy": {"status": "pending", "message": "Starting deploy…", "pod_id": "", "updated_at": saved_at},
    }
    _write_selection(record)

    provider = offer.get("provider")
    if provider == "runpod":
        api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
        if not api_key:
            deploy = {
                "status": "failed",
                "message": "RUNPOD_API_KEY missing in .env",
                "pod_id": "",
                "updated_at": _utc_now(),
            }
        else:
            deploy = _deploy_runpod(offer, api_key)
            deploy["updated_at"] = _utc_now()
    elif provider == "aws":
        deploy = _deploy_aws(offer)
        deploy["updated_at"] = _utc_now()
    else:
        deploy = {
            "status": "failed",
            "message": f"Unsupported provider: {provider}",
            "pod_id": "",
            "updated_at": _utc_now(),
        }

    record["deploy"] = deploy
    _write_selection(record)

    return {
        "ok": deploy.get("status") in ("deployed", "manual"),
        "selection_id": selection_id,
        "in_stock_at_save": in_stock,
        "catalog_note": catalog_note,
        "saved_selection": record,
        "deploy": deploy,
    }
