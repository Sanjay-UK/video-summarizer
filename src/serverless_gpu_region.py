"""Live RunPod serverless catalog for one data center (no caching)."""

from __future__ import annotations

from typing import Any

from src.gpu_recommendations import build_serverless_category_recommendations
from src.runpod_serverless_catalog import (
    default_serverless_selection_id,
    fetch_serverless_gpu_offers,
    filter_rows_by_datacenter,
    list_serverless_datacenters,
    serverless_sections_for_ui,
)

EMPTY_SERVERLESS_RECOMMENDATIONS: dict[str, Any] = {
    "engine": "rule_based_serverless",
    "categories": [],
    "recommended_gpu_for_pipeline": None,
}


def build_serverless_saved_display(
    *,
    saved: dict[str, Any] | None,
    deploy_status: dict[str, Any] | None,
    viewing_data_center_id: str,
    catalog_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Context for currently-selected UI in serverless mode."""
    viewing_dc = (viewing_data_center_id or "").strip()
    if not saved or not isinstance(saved.get("offer"), dict):
        return {"kind": "none"}

    offer = saved["offer"]
    cloud = str(offer.get("cloud") or "")
    if cloud != "runpod-serverless":
        return {
            "kind": "legacy_pod",
            "offer": offer,
            "saved_at": saved.get("saved_at"),
        }

    saved_dc = (
        str(saved.get("serverless_data_center_id") or "").strip()
        or str(offer.get("data_center_id") or "").strip()
    )
    selection_id = str(saved.get("selection_id") or "")
    live = next((r for r in catalog_rows if r.get("selection_id") == selection_id), None)
    display_offer = {**offer, **live} if live else offer

    if saved_dc and viewing_dc and saved_dc != viewing_dc:
        return {
            "kind": "serverless_other_region",
            "offer": display_offer,
            "saved_at": saved.get("saved_at"),
            "deploy_status": deploy_status,
            "saved_data_center_id": saved_dc,
            "viewing_data_center_id": viewing_dc,
        }

    return {
        "kind": "serverless",
        "offer": display_offer,
        "saved_at": saved.get("saved_at"),
        "deploy_status": deploy_status,
        "viewing_data_center_id": viewing_dc,
    }


def assemble_serverless_region_payload(
    catalog_rows: list[dict[str, Any]],
    catalog_error: str | None,
    data_center_id: str,
    *,
    saved: dict[str, Any] | None,
    deploy_status: dict[str, Any] | None,
    env_gpu_pools: str,
) -> dict[str, Any]:
    """Build UI payload for one DC from a catalog snapshot."""
    from datetime import datetime, timezone

    dc = (data_center_id or "").strip()
    datacenters = list_serverless_datacenters(catalog_rows)
    valid_dcs = {d["data_center_id"] for d in datacenters}
    if dc and valid_dcs and dc not in valid_dcs:
        return {
            "ok": False,
            "error": f"Unknown data center {dc!r}. Refresh the page for the latest region list.",
            "data_center_id": dc,
            "datacenters": datacenters,
            "recommendations": dict(EMPTY_SERVERLESS_RECOMMENDATIONS),
            "serverless_sections": [],
            "saved_display": {"kind": "none"},
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    regional_rows = filter_rows_by_datacenter(catalog_rows, dc) if dc else catalog_rows
    sections = serverless_sections_for_ui(catalog_rows, data_center_id=dc or None)
    recommendations = build_serverless_category_recommendations(catalog_rows, data_center_id=dc)
    default_selection_id = default_serverless_selection_id(
        regional_rows,
        data_center_id=dc,
        gpu_pools=env_gpu_pools,
    )
    saved_id = (saved or {}).get("selection_id")
    if saved_id and any(r.get("selection_id") == saved_id for r in regional_rows):
        default_selection_id = saved_id

    saved_display = build_serverless_saved_display(
        saved=saved,
        deploy_status=deploy_status,
        viewing_data_center_id=dc,
        catalog_rows=catalog_rows,
    )

    return {
        "ok": not catalog_error,
        "error": catalog_error,
        "data_center_id": dc,
        "datacenters": datacenters,
        "serverless_sections": sections,
        "regional_row_count": len(regional_rows),
        "recommendations": recommendations,
        "default_selection_id": default_selection_id,
        "saved_display": saved_display,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def fetch_live_serverless_region(
    api_key: str,
    data_center_id: str,
    *,
    saved: dict[str, Any] | None,
    deploy_status: dict[str, Any] | None,
    env_gpu_pools: str,
) -> dict[str, Any]:
    """Fresh RunPod HTTP GET, then region payload for one data center."""
    rows, catalog_error = fetch_serverless_gpu_offers(api_key)
    return assemble_serverless_region_payload(
        rows,
        catalog_error,
        data_center_id,
        saved=saved,
        deploy_status=deploy_status,
        env_gpu_pools=env_gpu_pools,
    )
