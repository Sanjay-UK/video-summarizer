"""Fetch cloud GPU and CPU offers with hourly pricing for the selection UI."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

PROJECT_DIR = Path(__file__).resolve().parent.parent
RUNPOD_GPU_CATALOG_URL = "https://api.runpod.io/v2/catalog/gpus"
RUNPOD_CPU_CATALOG_URL = "https://api.runpod.io/v2/catalog/cpus"
MIN_VRAM_GB = 12
AVAIL_OK = frozenset({"HIGH", "MEDIUM", "LOW"})

# Linux on-demand us-east-1 reference rates (allocatable in most regions).
AWS_GPU_INSTANCES = [
    {
        "name": "g4dn.xlarge",
        "gpu_label": "NVIDIA T4",
        "vram_gb": 16,
        "vcpus": 4,
        "price_per_hour": 0.526,
    },
    {
        "name": "g6.xlarge",
        "gpu_label": "NVIDIA L4",
        "vram_gb": 24,
        "vcpus": 4,
        "price_per_hour": 0.805,
    },
    {
        "name": "g5.xlarge",
        "gpu_label": "NVIDIA A10G",
        "vram_gb": 24,
        "vcpus": 4,
        "price_per_hour": 1.006,
    },
]

AWS_CPU_INSTANCES = [
    {"name": "c6i.large", "vcpus": 2, "memory_gb": 4, "price_per_hour": 0.085},
    {"name": "c6i.xlarge", "vcpus": 4, "memory_gb": 8, "price_per_hour": 0.17},
    {"name": "c7i.xlarge", "vcpus": 4, "memory_gb": 8, "price_per_hour": 0.1785},
    {"name": "m6i.xlarge", "vcpus": 4, "memory_gb": 16, "price_per_hour": 0.192},
    {"name": "c6i.2xlarge", "vcpus": 8, "memory_gb": 16, "price_per_hour": 0.34},
    {"name": "m6i.2xlarge", "vcpus": 8, "memory_gb": 32, "price_per_hour": 0.384},
]


def load_env_file() -> None:
    env_path = PROJECT_DIR / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def env_file_get(key: str) -> str:
    """Read a single key from .env (for UI display; avoids stale os.environ)."""
    env_path = PROJECT_DIR / ".env"
    if not env_path.is_file():
        return os.environ.get(key, "").strip()
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, value = line.partition("=")
        if k.strip() == key:
            return value.strip().strip('"').strip("'")
    return os.environ.get(key, "").strip()


def force_env_updates(updates: dict[str, str]) -> None:
    """Apply keys to os.environ after .env was updated elsewhere."""
    for key, value in updates.items():
        if key:
            os.environ[key] = value


@dataclass
class ComputeOffer:
    kind: Literal["gpu", "cpu"]
    provider: str
    cloud: str
    name: str
    price_per_hour: float
    availability: str
    vram_gb: int | None = None
    vcpus: int | None = None
    vcpu_min: int | None = None
    vcpu_max: int | None = None
    price_per_vcpu: float | None = None
    memory_gb: int | None = None
    resource_id: str = ""
    notes: str = ""
    data_center_id: str | None = None
    data_center_name: str | None = None
    serverless_pool: str | None = None
    gpu_catalog_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def offer_selection_id(offer: ComputeOffer) -> str:
    """Stable id for form selection (one GPU or CPU, not both)."""
    return f"{offer.kind}:{offer.provider}:{offer.cloud}:{offer.resource_id}"


def parse_selection_id(selection_id: str) -> dict[str, Any] | None:
    """Rebuild a minimal offer from selection_id (kind:provider:cloud:resource_id)."""
    raw = (selection_id or "").strip()
    parts = raw.split(":", 3)
    if len(parts) != 4:
        return None
    kind, provider, cloud, resource_id = parts
    if kind not in ("gpu", "cpu") or provider not in ("runpod", "aws") or not resource_id:
        return None
    display_name = resource_id
    data_center_id: str | None = None
    serverless_pool: str | None = None
    gpu_catalog_id: str | None = None
    if cloud == "runpod-serverless" and "|" in resource_id:
        dc_part, pool_part, gpu_part = resource_id.split("|", 2)
        data_center_id = dc_part
        serverless_pool = pool_part
        gpu_catalog_id = gpu_part
        display_name = gpu_part.replace("NVIDIA ", "", 1) if gpu_part.startswith("NVIDIA ") else gpu_part
    elif provider == "runpod" and resource_id.startswith("NVIDIA "):
        display_name = resource_id.replace("NVIDIA ", "", 1)
    elif provider == "aws":
        display_name = resource_id
    parsed: dict[str, Any] = {
        "kind": kind,
        "provider": provider,
        "cloud": cloud,
        "name": display_name,
        "resource_id": resource_id,
        "selection_id": raw,
        "price_per_hour": 0.0,
        "availability": "Unknown",
        "vram_gb": None,
        "vcpus": None,
        "vcpu_min": None,
        "vcpu_max": None,
        "price_per_vcpu": None,
        "memory_gb": None,
        "notes": "Resolved from selection_id (not in latest in-stock catalog)",
    }
    if data_center_id:
        parsed["data_center_id"] = data_center_id
    if serverless_pool:
        parsed["serverless_pool"] = serverless_pool
    if gpu_catalog_id:
        parsed["gpu_catalog_id"] = gpu_catalog_id
    return parsed


def offer_to_dict(offer: ComputeOffer) -> dict[str, Any]:
    row = offer.to_dict()
    row["selection_id"] = offer_selection_id(offer)
    return row


def _http_get_json(url: str, api_key: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_runpod_gpus(cloud: str, api_key: str) -> list[dict[str, Any]]:
    params = f"include=AVAILABILITY&product=POD&cloud={cloud.upper()}&count=1"
    payload = _http_get_json(f"{RUNPOD_GPU_CATALOG_URL}?{params}", api_key)
    return payload.get("gpus") or []


def _runpod_gpu_offers(api_key: str) -> tuple[list[ComputeOffer], str | None]:
    if not api_key:
        return [], "RUNPOD_API_KEY not set (add to .env)"
    offers: list[ComputeOffer] = []
    errors: list[str] = []
    for cloud in ("COMMUNITY", "SECURE"):
        price_key = "community" if cloud == "COMMUNITY" else "secure"
        label = "runpod-community" if cloud == "COMMUNITY" else "runpod-secure"
        try:
            gpus = _fetch_runpod_gpus(cloud, api_key)
        except urllib.error.HTTPError as exc:
            errors.append(f"RunPod GPU {cloud}: HTTP {exc.code}")
            continue
        except OSError as exc:
            errors.append(f"RunPod GPU {cloud}: {exc}")
            continue
        for g in gpus:
            avail = (g.get("availability") or "").upper()
            if avail not in AVAIL_OK:
                continue
            price_obj = g.get("price") or {}
            price = price_obj.get(price_key)
            if price is None:
                continue
            mem = int(g.get("memory") or 0)
            name = g.get("name") or g.get("id") or "GPU"
            offers.append(
                ComputeOffer(
                    kind="gpu",
                    provider="runpod",
                    cloud=label,
                    name=name,
                    price_per_hour=float(price),
                    availability=avail,
                    vram_gb=mem,
                    resource_id=g.get("id") or name,
                    notes=f"RunPod {cloud.title()} Cloud",
                )
            )
    return offers, ("; ".join(errors) if errors else None)


def _runpod_cpu_offers(api_key: str) -> tuple[list[ComputeOffer], str | None]:
    if not api_key:
        return [], None
    url = f"{RUNPOD_CPU_CATALOG_URL}?include=AVAILABILITY&product=POD"
    try:
        payload = _http_get_json(url, api_key)
    except urllib.error.HTTPError as exc:
        return [], f"RunPod CPU catalog: HTTP {exc.code}"
    except OSError as exc:
        return [], f"RunPod CPU catalog: {exc}"

    offers: list[ComputeOffer] = []
    for c in payload.get("cpus") or []:
        avail = (c.get("availability") or "").upper()
        if avail not in AVAIL_OK:
            continue
        price_obj = c.get("price") or {}
        per_vcpu = price_obj.get("securePerVcpu")
        if per_vcpu is None:
            continue
        vcpu_spec = c.get("vcpu") or {}
        vmin = int(vcpu_spec.get("min") or 0)
        vmax = int(vcpu_spec.get("max") or vmin)
        if vmin <= 0:
            continue
        group = c.get("group") or ""
        name = c.get("name") or c.get("id") or "CPU pod"
        ram_per = c.get("ramGbPerVcpu")
        ram_note = f"{ram_per} GB RAM per vCPU" if ram_per else ""
        offers.append(
            ComputeOffer(
                kind="cpu",
                provider="runpod",
                cloud="runpod-cpu-secure",
                name=f"{name} ({group})".strip() if group else name,
                price_per_hour=round(float(per_vcpu) * vmin, 4),
                availability=avail,
                vcpus=vmin,
                vcpu_min=vmin,
                vcpu_max=vmax,
                price_per_vcpu=float(per_vcpu),
                memory_gb=int(ram_per * vmin) if ram_per else None,
                resource_id=c.get("id") or name,
                notes=f"Hourly at {vmin} vCPU (min). {ram_note}".strip(),
            )
        )
    offers.sort(key=lambda o: o.price_per_hour)
    return offers, None


def _aws_gpu_offers() -> list[ComputeOffer]:
    out: list[ComputeOffer] = []
    for row in AWS_GPU_INSTANCES:
        out.append(
            ComputeOffer(
                kind="gpu",
                provider="aws",
                cloud="aws-on-demand",
                name=row["name"],
                price_per_hour=float(row["price_per_hour"]),
                availability="Regional",
                vram_gb=int(row["vram_gb"]),
                vcpus=int(row["vcpus"]),
                resource_id=row["name"],
                notes=row["gpu_label"],
            )
        )
    return out


def _aws_cpu_offers() -> list[ComputeOffer]:
    out: list[ComputeOffer] = []
    for row in AWS_CPU_INSTANCES:
        out.append(
            ComputeOffer(
                kind="cpu",
                provider="aws",
                cloud="aws-on-demand",
                name=row["name"],
                price_per_hour=float(row["price_per_hour"]),
                availability="Regional",
                vcpus=int(row["vcpus"]),
                memory_gb=int(row["memory_gb"]),
                resource_id=row["name"],
                notes="EC2 compute (no GPU)",
            )
        )
    return out


def _group_by_cloud(offers: list[ComputeOffer]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[ComputeOffer]] = {}
    for o in offers:
        grouped.setdefault(o.cloud, []).append(o)
    return {
        cloud: [offer_to_dict(x) for x in sorted(rows, key=lambda r: r.price_per_hour)]
        for cloud, rows in sorted(grouped.items())
    }


def build_gpu_selection_report() -> dict[str, Any]:
    from src.compute_deploy import load_saved_selection
    from src.gpu_recommendations import build_category_recommendations

    load_env_file()
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()

    runpod_gpus, runpod_gpu_error = _runpod_gpu_offers(api_key)
    runpod_cpus, runpod_cpu_error = _runpod_cpu_offers(api_key)
    aws_gpus = _aws_gpu_offers()
    aws_cpus = _aws_cpu_offers()

    gpu_offers = runpod_gpus + aws_gpus
    cpu_offers = runpod_cpus + aws_cpus
    gpu_offers.sort(key=lambda o: o.price_per_hour)
    cpu_offers.sort(key=lambda o: o.price_per_hour)

    errors = [e for e in (runpod_gpu_error, runpod_cpu_error) if e]

    saved = load_saved_selection()
    from src.compute_deploy import COMPUTE_MODE_SERVERLESS, load_compute_mode

    compute_mode = load_compute_mode()
    from src.runpod_platform_controls import platform_controls_status
    from src.serverless_model_catalog import catalog_for_ui, load_saved_serverless_models

    env_dc = os.environ.get("RUNPOD_DATA_CENTER_ID", "").strip()
    env_pools = os.environ.get("RUNPOD_SERVERLESS_GPU_POOLS", "ADA_24,AMPERE_16").strip()
    env_serverless_image = os.environ.get("RUNPOD_SERVERLESS_IMAGE", "").strip()
    from src.runpod_serverless import serverless_image_registry_ready
    serverless_deploy_env: dict[str, str] | None = None
    serverless_gpu_rows: list[dict[str, Any]] = []
    serverless_datacenters: list[dict[str, str]] = []
    serverless_sections: list[dict[str, Any]] = []
    serverless_selected_data_center_id = ""
    serverless_default_selection_id: str | None = None

    serverless_saved_display: dict[str, Any] | None = None

    if compute_mode == COMPUTE_MODE_SERVERLESS:
        from src.runpod_serverless_catalog import (
            fetch_serverless_gpu_offers,
            list_serverless_datacenters,
            resolve_initial_serverless_datacenter,
        )
        from src.serverless_gpu_region import assemble_serverless_region_payload

        serverless_deploy_env = {
            "data_center_id": env_dc,
            "gpu_pools": env_pools,
        }
        serverless_gpu_rows, sl_err = fetch_serverless_gpu_offers(api_key)
        if sl_err:
            errors.append(sl_err)
        serverless_datacenters = list_serverless_datacenters(serverless_gpu_rows)
        serverless_selected_data_center_id = resolve_initial_serverless_datacenter(
            saved=saved,
            env_data_center_id=env_dc,
            datacenters=serverless_datacenters,
        )
        deploy_status = (saved or {}).get("deploy")
        region_bundle = assemble_serverless_region_payload(
            serverless_gpu_rows,
            sl_err,
            serverless_selected_data_center_id,
            saved=saved,
            deploy_status=deploy_status if isinstance(deploy_status, dict) else None,
            env_gpu_pools=env_pools,
        )
        serverless_sections = region_bundle.get("serverless_sections") or []
        serverless_default_selection_id = region_bundle.get("default_selection_id")
        serverless_saved_display = region_bundle.get("saved_display")
        from src.serverless_gpu_region import EMPTY_SERVERLESS_RECOMMENDATIONS

        recommendations = region_bundle.get("recommendations") or dict(
            EMPTY_SERVERLESS_RECOMMENDATIONS
        )
    else:
        from src.gpu_recommendations import build_category_recommendations

        recommendations = build_category_recommendations(
            runpod_gpus + aws_gpus, runpod_cpus + aws_cpus
        )

    return {
        "min_vram_gb": MIN_VRAM_GB,
        "errors": errors,
        "runpod_error": runpod_gpu_error,
        "gpu_offers": [offer_to_dict(o) for o in gpu_offers],
        "cpu_offers": [offer_to_dict(o) for o in cpu_offers],
        "gpus_by_cloud": _group_by_cloud(gpu_offers),
        "cpus_by_cloud": _group_by_cloud(cpu_offers),
        "recommendations": recommendations,
        "recommended_gpu_for_pipeline": recommendations.get("recommended_gpu_for_pipeline"),
        "saved_selection": saved,
        "saved_selection_id": (saved or {}).get("selection_id"),
        "deploy_status": (saved or {}).get("deploy"),
        "fetched_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "runpod_platform": platform_controls_status(),
        "serverless_model_catalog": catalog_for_ui(),
        "serverless_models_saved": load_saved_serverless_models(),
        "compute_mode": compute_mode,
        "serverless_deploy_env": serverless_deploy_env,
        "serverless_gpu_rows": serverless_gpu_rows,
        "serverless_datacenters": serverless_datacenters,
        "serverless_sections": serverless_sections,
        "serverless_selected_data_center_id": serverless_selected_data_center_id,
        "serverless_default_selection_id": serverless_default_selection_id,
        "serverless_saved_display": serverless_saved_display,
        "serverless_image_configured": serverless_image_registry_ready(env_serverless_image),
        "serverless_image_value": env_serverless_image if env_serverless_image else None,
        "docker_hub_user": env_file_get("DOCKER_HUB_USER"),
    }
