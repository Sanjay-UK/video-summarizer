"""Persist compute selection and trigger provider deploy."""

from __future__ import annotations
from src.log_safety import redact

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
COMPUTE_MODE_POD = "pod"
COMPUTE_MODE_SERVERLESS = "serverless"
VALID_COMPUTE_MODES = frozenset({COMPUTE_MODE_POD, COMPUTE_MODE_SERVERLESS})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_compute_mode() -> str:
    """RunPod deploy target: serverless (default) or legacy pod."""
    saved = load_saved_selection()
    if saved and saved.get("compute_mode") in VALID_COMPUTE_MODES:
        return str(saved["compute_mode"])
    mode = os.environ.get("RUNPOD_COMPUTE_MODE", COMPUTE_MODE_SERVERLESS).strip().lower()
    return mode if mode in VALID_COMPUTE_MODES else COMPUTE_MODE_SERVERLESS


def saved_serverless_endpoint_id(saved: dict[str, Any] | None) -> str:
    """The known serverless endpoint id: the explicit field, else the last deploy record's id."""
    saved = saved or {}
    explicit = str(saved.get("serverless_endpoint_id") or "").strip()
    if explicit:
        return explicit
    return str((saved.get("deploy") or {}).get("endpoint_id") or "").strip()


def warmup_required(deploy: dict[str, Any], models_normalized: dict[str, Any] | None) -> bool:
    """A warmup job (which starts a paid worker) runs only for a newly created endpoint with models chosen."""
    if deploy.get("status") != "deployed" or not models_normalized:
        return False
    if deploy.get("reused"):
        return False
    return bool(str(deploy.get("endpoint_id") or "").strip())


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
    for key in ("gpu_offers", "cpu_offers", "serverless_gpu_rows"):
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


def _serverless_placement_from_offer(offer: dict[str, Any]) -> tuple[str, list[str]]:
    dc = (offer.get("data_center_id") or "").strip() or os.environ.get(
        "RUNPOD_DATA_CENTER_ID", ""
    ).strip()
    pool = (offer.get("serverless_pool") or "").strip()
    if pool:
        pools = [pool]
        extra = os.environ.get("RUNPOD_SERVERLESS_GPU_POOLS", "").strip()
        for p in extra.split(","):
            p = p.strip()
            if p and p not in pools:
                pools.append(p)
    else:
        from src.runpod_serverless import _gpu_pools_from_env

        pools = _gpu_pools_from_env()
    if not dc:
        raise RuntimeError(
            "Serverless data center required: pick a serverless GPU row or set RUNPOD_DATA_CENTER_ID"
        )
    return dc, pools


def _deploy_runpod_serverless(offer: dict[str, Any]) -> dict[str, Any]:
    from src.runpod_serverless import (
        create_network_volume,
        create_serverless_endpoint,
        endpoint_name_for,
        find_serverless_endpoint,
        list_serverless_endpoints,
        serverless_worker_image,
        unique_endpoint_name,
    )

    try:
        dc, pools = _serverless_placement_from_offer(offer)
    except RuntimeError as exc:
        return {
            "status": "failed",
            "message": redact(str(exc)),
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": "",
            "provider_response": {},
        }

    try:
        serverless_worker_image()
    except RuntimeError as exc:
        return {
            "status": "failed",
            "message": redact(str(exc)),
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": "",
            "data_center_id": dc,
            "serverless_gpu_pools": pools,
            "provider_response": {},
        }

    saved = load_saved_selection() or {}
    prior_deploy = saved.get("deploy") or {}
    volume_id = os.environ.get("RUNPOD_NETWORK_VOLUME_ID", "").strip()
    if not volume_id:
        volume_id = (prior_deploy.get("network_volume_id") or "").strip()
    prior_dc = (prior_deploy.get("data_center_id") or "").strip()
    if not prior_dc and volume_id:
        prior_dc = (
            str(saved.get("serverless_data_center_id") or "").strip()
            or str(offer.get("data_center_id") or "").strip()
        )
    if volume_id and prior_dc and prior_dc != dc:
        return {
            "status": "failed",
            "message": (
                f"Saved network volume is in {prior_dc}; selected data center is {dc}. "
                "Delete the volume or pick a GPU in the same data center."
            ),
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": volume_id,
            "provider_response": {},
        }
    if not volume_id:
        suffix = (offer.get("name") or "gpu")[:24].replace(" ", "-")
        vol = create_network_volume(name_suffix=f"-{suffix}", data_center_id=dc)
        volume_id = str(vol.get("id") or "")
        if not volume_id:
            return {
                "status": "failed",
                "message": "The network volume could not be created.",
                "pod_id": "",
                "endpoint_id": "",
                "network_volume_id": "",
                "provider_response": vol,
            }

    # Discover first: reuse the saved endpoint (or one with the same name) instead of creating a duplicate.
    # Reuse never changes the endpoint's image or workers; a mismatch is reported, not applied.
    target_name = endpoint_name_for(str(offer.get("name") or ""))
    prior_endpoint_id = saved_serverless_endpoint_id(saved)
    try:
        existing = find_serverless_endpoint(endpoint_id=prior_endpoint_id, name=target_name)
        if existing:
            existing_id = str(existing.get("id") or "")
            configured_image = serverless_worker_image()
            current_image = str(existing.get("image") or "")
            note = ""
            if current_image and current_image != configured_image:
                note = (
                    f" Endpoint image is {current_image}; configured image is {configured_image}. "
                    "The image was not changed automatically."
                )
            from src.endpoint_capacity import lower_if_idle

            capacity = lower_if_idle(existing_id, lambda _m: None)
            if not capacity.ok:
                return {
                    "status": "failed",
                    "message": (
                        f"Found an existing deployment ({target_name}) but could not verify it is idle "
                        f"(max workers 0): {capacity.message}. Check it in your cloud console."
                    ),
                    "pod_id": "",
                    "endpoint_id": "",
                    "network_volume_id": volume_id,
                    "data_center_id": dc,
                    "serverless_gpu_pools": pools,
                    "provider_response": {},
                }
            return {
                "status": "deployed",
                "reused": True,
                # The endpoint is already warm or was warmed before; a save must not launch a warmup job.
                "warmup_status": "skipped_reused",
                "message": f"Reused the existing deployment ({target_name}); not recreated.{note}",
                "pod_id": "",
                "endpoint_id": existing_id,
                "network_volume_id": volume_id,
                "request_urls": existing.get("requestUrls") or {},
                "data_center_id": dc,
                "serverless_gpu_pools": pools,
                "provider_response": existing,
            }
        taken = {str(e.get("name") or "") for e in list_serverless_endpoints()}
        ep = create_serverless_endpoint(
            network_volume_id=volume_id,
            gpu_offer_name=str(offer.get("name") or ""),
            data_center_id=dc,
            gpu_pools=pools,
            name=unique_endpoint_name(target_name, taken),
        )
    except RuntimeError as exc:
        return {
            "status": "failed",
            "message": redact(str(exc)),
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": volume_id,
            "data_center_id": dc,
            "serverless_gpu_pools": pools,
            "provider_response": {},
        }

    endpoint_id = str(ep.get("id") or "")
    # The create body sets max workers to 3. Set it to 0 and read it back. A failed check is a failed deploy:
    # the endpoint exists but may be running at max 3, so the user must see that.
    from src.endpoint_capacity import lower_if_idle

    capacity = lower_if_idle(endpoint_id, lambda _m: None)
    if not capacity.ok:
        return {
            "status": "failed",
            "message": (
                f"The deployment was created, but its idle setting could not be verified as 0: "
                f"{capacity.message}. Set max workers to 0 in your cloud console, then save again."
            ),
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": volume_id,
            "data_center_id": dc,
            "serverless_gpu_pools": pools,
            "provider_response": ep,
        }
    return {
        "status": "deployed",
        "reused": False,
        "message": "Serverless deployment created. Max workers verified at 0 (idle).",
        "pod_id": "",
        "endpoint_id": endpoint_id,
        "network_volume_id": volume_id,
        "request_urls": ep.get("requestUrls") or {},
        "data_center_id": dc,
        "serverless_gpu_pools": pools,
        "provider_response": ep,
    }


def _deploy_runpod(offer: dict[str, Any], api_key: str) -> dict[str, Any]:
    cloud_type = _runpod_cloud_type(offer.get("cloud", ""))
    try:
        body = _build_runpod_pod_body(offer)
    except ValueError as exc:
        return {
            "status": "failed",
            "message": redact(str(exc)),
            "pod_id": "",
            "provider_response": {},
        }

    status, payload = _http_post_json(RUNPOD_PODS_URL, api_key, body)
    if status == 201:
        pod_id = payload.get("id") or payload.get("podId") or ""
        return {
            "status": "deployed",
            "message": f"GPU pod created ({payload.get('cloud', cloud_type)}).",
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
        "message": f"Deploy failed (HTTP {status}). Check the cloud GPU settings and try again.",
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


def stop_saved_runpod_workers() -> dict[str, Any]:
    """Scale serverless workers to zero (stop GPU billing for workers)."""
    load_env_file()
    saved = load_saved_selection()
    if not saved:
        return {"ok": False, "error": "No saved deployment in data/compute_selection.json"}
    if load_compute_mode() != COMPUTE_MODE_SERVERLESS:
        return {"ok": False, "error": "Not a serverless deployment"}
    deploy = saved.get("deploy") or {}
    endpoint_id = (deploy.get("endpoint_id") or "").strip()
    if not endpoint_id:
        return {"ok": False, "error": "No endpoint_id in saved deploy record"}

    from src.runpod_serverless import stop_serverless_workers

    try:
        stop_serverless_workers(endpoint_id)
    except RuntimeError as exc:
        return {"ok": False, "error": redact(str(exc))}

    deploy["message"] = f"Serverless workers scaled to 0 for endpoint {endpoint_id}."
    deploy["updated_at"] = _utc_now()
    deploy["workers_stopped_at"] = deploy["updated_at"]
    saved["deploy"] = deploy
    _write_selection(saved)
    return {"ok": True, "endpoint_id": endpoint_id, "message": deploy["message"]}


def delete_saved_network_volume(*, confirm: str) -> dict[str, Any]:
    """Detach volume from endpoint and permanently delete it (cached models lost)."""
    load_env_file()
    if confirm.strip().upper() != "DELETE":
        return {
            "ok": False,
            "error": 'Confirmation required: send {"confirm": "DELETE"} to delete the network volume.',
        }
    saved = load_saved_selection()
    if not saved:
        return {"ok": False, "error": "No saved deployment"}
    deploy = saved.get("deploy") or {}
    volume_id = (deploy.get("network_volume_id") or "").strip()
    if not volume_id:
        return {"ok": False, "error": "No network_volume_id in saved deploy record"}
    endpoint_id = (deploy.get("endpoint_id") or "").strip()

    from src.runpod_serverless import (
        delete_network_volume,
        detach_network_volume_from_endpoint,
        stop_serverless_workers,
    )

    try:
        if endpoint_id:
            stop_serverless_workers(endpoint_id)
            detach_network_volume_from_endpoint(endpoint_id)
        delete_network_volume(volume_id)
    except RuntimeError as exc:
        return {"ok": False, "error": redact(str(exc))}

    deploy["network_volume_id"] = ""
    deploy["message"] = f"Network volume {volume_id} deleted. Model cache on volume is gone."
    deploy["updated_at"] = _utc_now()
    saved["deploy"] = deploy
    _write_selection(saved)
    return {"ok": True, "volume_id": volume_id, "message": deploy["message"]}


def stop_saved_runpod_pod() -> dict[str, Any]:
    """Stop RunPod compute: serverless workers → 0, or terminate legacy pod."""
    load_env_file()
    if load_compute_mode() == COMPUTE_MODE_SERVERLESS:
        return stop_saved_runpod_workers()

    saved = load_saved_selection()
    if not saved:
        return {"ok": False, "error": "No saved deployment in data/compute_selection.json"}

    offer = saved.get("offer") or {}
    if offer.get("provider") != "runpod":
        return {"ok": False, "error": "Saved selection is not a GPU pod"}

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
            "error": f"Stop failed (HTTP {status}). Try again or stop it in your cloud console.",
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
    serverless_models: dict[str, Any] | None = None,
    serverless_data_center_id: str | None = None,
    docker_hub_user: str | None = None,
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

    compute_mode = load_compute_mode()
    dc_override = (serverless_data_center_id or "").strip()
    if compute_mode == COMPUTE_MODE_SERVERLESS and dc_override:
        offer_dc = (offer.get("data_center_id") or "").strip()
        if offer_dc and offer_dc != dc_override:
            return {
                "ok": False,
                "error": (
                    f"Selected GPU is in {offer_dc}; region dropdown is {dc_override}. "
                    "Pick a GPU row in the chosen region or change the region."
                ),
            }
        offer = {**offer, "data_center_id": dc_override}

    saved_at = _utc_now()
    prior = load_saved_selection() or {}
    models_normalized: dict[str, str] | None = None
    if compute_mode == COMPUTE_MODE_SERVERLESS:
        from src.serverless_model_catalog import normalize_serverless_models

        models_normalized = normalize_serverless_models(serverless_models)
    record: dict[str, Any] = {
        "selection_id": selection_id,
        "saved_at": saved_at,
        "compute_mode": compute_mode,
        "serverless_data_center_id": dc_override or (offer.get("data_center_id") or ""),
        "serverless_models": models_normalized,
        "offer": offer,
        "in_stock_at_save": in_stock,
        "catalog_note": catalog_note,
        "pipeline_runtime": prior.get("pipeline_runtime") or "local",
        # Keep the known endpoint across saves, so a failed or repeated save never erases the link.
        "serverless_endpoint_id": saved_serverless_endpoint_id(prior),
        "deploy": {
            "status": "pending",
            "message": "Starting deploy…",
            "pod_id": "",
            "endpoint_id": "",
            "network_volume_id": "",
            "updated_at": saved_at,
        },
    }
    _write_selection(record)

    docker_hub_apply: dict[str, Any] | None = None
    if compute_mode == COMPUTE_MODE_SERVERLESS and docker_hub_user is not None:
        from src.serverless_worker_image_publish import apply_docker_hub_user

        docker_hub_apply = apply_docker_hub_user(docker_hub_user)
        if not docker_hub_apply.get("ok"):
            err = docker_hub_apply.get("error") or "Invalid Docker Hub username"
            deploy = {
                "status": "failed",
                "message": err,
                "pod_id": "",
                "endpoint_id": "",
                "network_volume_id": "",
                "updated_at": _utc_now(),
            }
            record["deploy"] = deploy
            record["docker_hub_user"] = docker_hub_apply
            _write_selection(record)
            return {
                "ok": False,
                "error": err,
                "docker_hub_user": docker_hub_apply,
                "selection_id": selection_id,
                "saved_selection": record,
                "deploy": deploy,
            }
        if docker_hub_apply.get("user"):
            record["docker_hub_user"] = docker_hub_apply.get("user")

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
        elif compute_mode == COMPUTE_MODE_SERVERLESS:
            from src.runpod_serverless import serverless_image_registry_ready
            from src.serverless_worker_image_publish import (
                ensure_serverless_worker_image,
                sync_serverless_env_from_selection,
            )

            sync_serverless_env_from_selection()
            publish_info: dict[str, Any] | None = None
            worker_source = (
                (models_normalized or {}).get("worker_image_source") or "custom_build"
            )
            registry_image = ((models_normalized or {}).get("registry_image") or "").strip()
            current_image = os.environ.get("RUNPOD_SERVERLESS_IMAGE", "").strip()
            if worker_source == "registry":
                from src.serverless_worker_image_publish import apply_registry_image

                img = registry_image or current_image
                publish_info = apply_registry_image(img)
                record["docker_publish"] = publish_info
                if not publish_info.get("ok"):
                    err = publish_info.get("error") or "Invalid registry image"
                    deploy = {
                        "status": "failed",
                        "message": err,
                        "pod_id": "",
                        "endpoint_id": "",
                        "network_volume_id": "",
                        "docker_publish": publish_info,
                        "updated_at": _utc_now(),
                    }
                    record["deploy"] = deploy
                    _write_selection(record)
                    return {
                        "ok": False,
                        "error": err,
                        "docker_publish": publish_info,
                        "selection_id": selection_id,
                        "saved_selection": record,
                        "deploy": deploy,
                    }
                os.environ["RUNPOD_SERVERLESS_IMAGE"] = str(publish_info.get("image") or "")
            elif not serverless_image_registry_ready(current_image):
                record["deploy"] = {
                    **record["deploy"],
                    "status": "pending",
                    "message": "Building and pushing serverless worker Docker image…",
                    "updated_at": _utc_now(),
                }
                _write_selection(record)
                publish_info = ensure_serverless_worker_image()
                record["docker_publish"] = publish_info
                if not publish_info.get("ok"):
                    err = publish_info.get("error") or "Docker publish failed"
                    tail = publish_info.get("build_log_tail")
                    if tail:
                        err = f"{redact(err)}\n\n--- build log (tail) ---\n{tail[-2000:]}"
                    deploy = {
                        "status": "failed",
                        "message": err,
                        "pod_id": "",
                        "endpoint_id": "",
                        "network_volume_id": "",
                        "docker_publish": publish_info,
                        "updated_at": _utc_now(),
                    }
                    record["deploy"] = deploy
                    _write_selection(record)
                    return {
                        "ok": False,
                        "error": err,
                        "docker_publish": publish_info,
                        "selection_id": selection_id,
                        "saved_selection": record,
                        "deploy": deploy,
                    }
                os.environ["RUNPOD_SERVERLESS_IMAGE"] = str(publish_info.get("image") or "")

            deploy = _deploy_runpod_serverless(offer)
            if publish_info:
                deploy["docker_publish"] = publish_info
            deploy["updated_at"] = _utc_now()
            if warmup_required(deploy, models_normalized):
                endpoint_id = (deploy.get("endpoint_id") or "").strip()
                if endpoint_id:
                    deploy["warmup_status"] = "running"
                    deploy["message"] = (
                        f"{deploy.get('message', '')} Cold start: loading Whisper, Ollama, NLLB…"
                    )
                    record["deploy"] = deploy
                    _write_selection(record)
                    try:
                        from src.runpod_serverless import run_warmup_cold_start, save_job_metrics_to_selection

                        warmup = run_warmup_cold_start(
                            endpoint_id,
                            whisper_model=models_normalized["whisper"],
                            ollama_model=models_normalized["ollama"],
                            nllb_model=models_normalized["nllb"],
                        )
                        deploy["warmup_status"] = "completed"
                        deploy["message"] = (
                            f"Serverless ready. Models loaded (delayTime "
                            f"{warmup.get('delayTime')} ms)."
                        )
                        metrics = {
                            "delayTime": warmup.get("delayTime"),
                            "executionTime": warmup.get("executionTime"),
                            "runpod_job_id": warmup.get("id"),
                            "recorded_at": _utc_now(),
                            "phase": "warmup",
                        }
                        deploy["last_job_metrics"] = metrics
                        save_job_metrics_to_selection(metrics)
                    except Exception as exc:
                        deploy["warmup_status"] = "failed"
                        deploy["message"] = f"Deploy OK but warmup failed: {exc}"
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

    if deploy.get("status") == "deployed" and str(deploy.get("endpoint_id") or "").strip():
        record["serverless_endpoint_id"] = str(deploy["endpoint_id"]).strip()
    record["deploy"] = deploy
    _write_selection(record)

    out: dict[str, Any] = {
        "ok": deploy.get("status") in ("deployed", "manual"),
        "selection_id": selection_id,
        "in_stock_at_save": in_stock,
        "catalog_note": catalog_note,
        "saved_selection": record,
        "deploy": deploy,
    }
    if record.get("docker_publish"):
        out["docker_publish"] = record["docker_publish"]
    return out
