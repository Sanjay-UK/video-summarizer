"""RunPod catalog: serverless GPU availability per data center (GET /v2/catalog/gpus)."""

from __future__ import annotations

import urllib.error
from typing import Any

from src.gpu_selection import (
    AVAIL_OK,
    RUNPOD_GPU_CATALOG_URL,
    ComputeOffer,
    _http_get_json,
    offer_to_dict,
)

AVAIL_SKIP = frozenset({"NONE"})


def _cloud_badges(secure: bool, community: bool) -> str:
    parts: list[str] = []
    if secure:
        parts.append("Secure")
    if community:
        parts.append("Community")
    return "+".join(parts) if parts else "—"


def fetch_serverless_gpu_offers(api_key: str) -> tuple[list[dict[str, Any]], str | None]:
    """
    Flatten SERVERLESS availability into one selectable row per (GPU type, data center).
    """
    if not api_key:
        return [], "RUNPOD_API_KEY not set (add to .env)"
    params = "include=AVAILABILITY&product=SERVERLESS&count=1"
    try:
        payload = _http_get_json(f"{RUNPOD_GPU_CATALOG_URL}?{params}", api_key)
    except urllib.error.HTTPError as exc:
        return [], f"RunPod serverless GPU catalog: HTTP {exc.code}"
    except OSError as exc:
        return [], f"RunPod serverless GPU catalog: {exc}"

    rows: list[dict[str, Any]] = []
    for g in payload.get("gpus") or []:
        pool = (g.get("pool") or "").strip()
        if not pool:
            continue
        price_obj = g.get("price") or {}
        price = price_obj.get("serverless")
        if price is None:
            continue
        gpu_id = g.get("id") or g.get("name") or ""
        name = g.get("name") or gpu_id
        vram = g.get("memory")
        secure = bool(g.get("secure"))
        community = bool(g.get("community"))
        for dc in g.get("dataCenters") or []:
            dc_avail = (dc.get("availability") or "").upper()
            if dc_avail in AVAIL_SKIP:
                continue
            dc_id = (dc.get("id") or "").strip()
            if not dc_id:
                continue
            offer = ComputeOffer(
                kind="gpu",
                provider="runpod",
                cloud="runpod-serverless",
                name=name,
                price_per_hour=float(price),
                availability=dc_avail or (g.get("availability") or "Unknown"),
                vram_gb=int(vram) if vram is not None else None,
                resource_id=f"{dc_id}|{pool}|{gpu_id}",
                notes=f"pool {pool} · {_cloud_badges(secure, community)}",
                data_center_id=dc_id,
                data_center_name=dc.get("name") or dc_id,
                serverless_pool=pool,
                gpu_catalog_id=gpu_id,
            )
            row = offer_to_dict(offer)
            row["secure_cloud"] = secure
            row["community_cloud"] = community
            row["cloud_badges"] = _cloud_badges(secure, community)
            rows.append(row)

    rows.sort(
        key=lambda r: (
            r.get("data_center_name") or r.get("data_center_id") or "",
            r.get("name") or "",
        )
    )
    return rows, None


def group_serverless_rows_by_datacenter(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """[{data_center_id, data_center_name, rows: [...]}] sorted by name."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        dc_id = row.get("data_center_id") or "unknown"
        if dc_id not in buckets:
            buckets[dc_id] = {
                "data_center_id": dc_id,
                "data_center_name": row.get("data_center_name") or dc_id,
                "rows": [],
            }
        buckets[dc_id]["rows"].append(row)
    return sorted(buckets.values(), key=lambda b: b.get("data_center_name") or "")


def filter_serverless_cloud_rows(
    rows: list[dict[str, Any]], *, secure: bool, community: bool
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if secure and row.get("secure_cloud"):
            out.append(row)
        elif community and row.get("community_cloud"):
            out.append(row)
    return out


def list_serverless_datacenters(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Unique data centers present in the serverless catalog."""
    names: dict[str, str] = {}
    for row in rows:
        dc_id = (row.get("data_center_id") or "").strip()
        if not dc_id:
            continue
        names.setdefault(dc_id, row.get("data_center_name") or dc_id)
    return [
        {"data_center_id": dc_id, "data_center_name": names[dc_id]}
        for dc_id in sorted(names, key=lambda i: names[i].lower())
    ]


def filter_rows_by_datacenter(
    rows: list[dict[str, Any]], data_center_id: str
) -> list[dict[str, Any]]:
    dc = (data_center_id or "").strip()
    if not dc:
        return list(rows)
    return [r for r in rows if r.get("data_center_id") == dc]


def resolve_initial_serverless_datacenter(
    *,
    saved: dict[str, Any] | None,
    env_data_center_id: str,
    datacenters: list[dict[str, str]],
) -> str:
    """Saved region → offer/deploy → .env → first catalog DC."""
    valid = {d["data_center_id"] for d in datacenters}
    candidates: list[str] = []
    if saved:
        candidates.append(str(saved.get("serverless_data_center_id") or "").strip())
        offer = saved.get("offer") if isinstance(saved.get("offer"), dict) else {}
        candidates.append(str(offer.get("data_center_id") or "").strip())
        deploy = saved.get("deploy") if isinstance(saved.get("deploy"), dict) else {}
        candidates.append(str(deploy.get("data_center_id") or "").strip())
    candidates.append((env_data_center_id or "").strip())
    for dc in candidates:
        if dc and dc in valid:
            return dc
    if datacenters:
        return datacenters[0]["data_center_id"]
    return ""


def serverless_sections_for_ui(
    rows: list[dict[str, Any]], *, data_center_id: str | None = None
) -> list[dict[str, Any]]:
    """
    Secure + Community sections (like pod UI). Optional data_center_id filters rows.
    """
    if data_center_id:
        rows = filter_rows_by_datacenter(rows, data_center_id)
    sections: list[dict[str, Any]] = []
    for key, title, flag in (
        ("runpod-serverless-secure", "RunPod Secure Cloud — Serverless GPUs", "secure"),
        ("runpod-serverless-community", "RunPod Community Cloud — Serverless GPUs", "community"),
    ):
        filtered = filter_serverless_cloud_rows(
            rows,
            secure=(flag == "secure"),
            community=(flag == "community"),
        )
        if not filtered:
            continue
        filtered.sort(key=lambda r: (r.get("price_per_hour") or 0, r.get("name") or ""))
        sections.append({"key": key, "title": title, "rows": filtered})
    return sections


def default_serverless_selection_id(
    rows: list[dict[str, Any]],
    *,
    data_center_id: str,
    gpu_pools: str,
) -> str | None:
    """Match .env defaults to a catalog row when possible."""
    dc = (data_center_id or "").strip()
    pools = [p.strip() for p in (gpu_pools or "").split(",") if p.strip()]
    if dc and pools:
        for row in rows:
            if row.get("data_center_id") == dc and row.get("serverless_pool") in pools:
                return row.get("selection_id")
    if dc:
        for row in rows:
            if row.get("data_center_id") == dc:
                return row.get("selection_id")
    if rows:
        return rows[0].get("selection_id")
    return None
