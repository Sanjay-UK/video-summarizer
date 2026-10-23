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
        if key and key not in os.environ:
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
    if provider == "runpod" and resource_id.startswith("NVIDIA "):
        display_name = resource_id.replace("NVIDIA ", "", 1)
    elif provider == "aws":
        display_name = resource_id
    return {
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
    recommendations = build_category_recommendations(runpod_gpus + aws_gpus, runpod_cpus + aws_cpus)

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
    }
