"""Rule-based GPU/CPU recommendations by workload category (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from config import SUMMARIZATION_MODEL, WHISPER_MODEL

from src.gpu_selection import MIN_VRAM_GB, ComputeOffer, offer_to_dict

# Rough VRAM budget for concurrent GPU residents in this repo (Whisper + Ollama + NLLB).
_PIPELINE_VRAM_COMFORT_GB = 24
_PIPELINE_VRAM_MIN_GB = 16
_PIPELINE_VRAM_FLOOR_GB = MIN_VRAM_GB  # 12


@dataclass(frozen=True)
class RecommendationCategory:
    id: str
    title: str
    description: str
    kind: str  # "gpu" | "cpu"
    picker: Callable[[list[ComputeOffer]], ComputeOffer | None]


def _gpus(offers: list[ComputeOffer]) -> list[ComputeOffer]:
    return [o for o in offers if o.kind == "gpu"]


def _cpus(offers: list[ComputeOffer]) -> list[ComputeOffer]:
    return [o for o in offers if o.kind == "cpu"]


def _cheapest_matching(
    offers: list[ComputeOffer],
    *,
    min_vram: int | None = None,
    max_vram: int | None = None,
    provider: str | None = None,
    cloud_contains: str | None = None,
) -> ComputeOffer | None:
    pool = offers
    if min_vram is not None:
        pool = [o for o in pool if (o.vram_gb or 0) >= min_vram]
    if max_vram is not None:
        pool = [o for o in pool if (o.vram_gb or 0) <= max_vram]
    if provider:
        pool = [o for o in pool if o.provider == provider]
    if cloud_contains:
        pool = [o for o in pool if cloud_contains in o.cloud]
    if not pool:
        return None
    pool.sort(key=lambda o: (o.price_per_hour, -(o.vram_gb or 0)))
    return pool[0]


def _pick_full_pipeline(offers: list[ComputeOffer]) -> ComputeOffer | None:
    g = _gpus(offers)
    for min_vram in (_PIPELINE_VRAM_COMFORT_GB, _PIPELINE_VRAM_MIN_GB, _PIPELINE_VRAM_FLOOR_GB):
        pick = _cheapest_matching(g, min_vram=min_vram)
        if pick:
            return pick
    return None


def _pick_transcribe_summarize(offers: list[ComputeOffer]) -> ComputeOffer | None:
    g = _gpus(offers)
    return _cheapest_matching(g, min_vram=_PIPELINE_VRAM_FLOOR_GB) or _cheapest_matching(g)


def _pick_budget_gpu(offers: list[ComputeOffer]) -> ComputeOffer | None:
    return _cheapest_matching(_gpus(offers))


def _pick_comfort_24gb(offers: list[ComputeOffer]) -> ComputeOffer | None:
    return _cheapest_matching(_gpus(offers), min_vram=24)


def _pick_runpod_community_value(offers: list[ComputeOffer]) -> ComputeOffer | None:
    return _cheapest_matching(
        _gpus(offers),
        min_vram=_PIPELINE_VRAM_FLOOR_GB,
        provider="runpod",
        cloud_contains="community",
    )


def _pick_aws_reliable_gpu(offers: list[ComputeOffer]) -> ComputeOffer | None:
    return _cheapest_matching(_gpus(offers), min_vram=16, provider="aws")


def _pick_cpu_budget(offers: list[ComputeOffer]) -> ComputeOffer | None:
    return _cheapest_matching(_cpus(offers))


def _category_definitions() -> list[RecommendationCategory]:
    whisper = WHISPER_MODEL
    ollama = SUMMARIZATION_MODEL
    return [
        RecommendationCategory(
            id="full_pipeline",
            title="Full video summarizer pipeline",
            description=(
                f"Rules: Whisper ({whisper}) + Ollama ({ollama}) + NLLB on GPU — "
                f"prefer ≥{_PIPELINE_VRAM_COMFORT_GB} GB VRAM, else ≥{_PIPELINE_VRAM_MIN_GB}, "
                f"else ≥{_PIPELINE_VRAM_FLOOR_GB}; lowest $/hr in catalog."
            ),
            kind="gpu",
            picker=_pick_full_pipeline,
        ),
        RecommendationCategory(
            id="transcribe_and_summarize",
            title="Transcribe + summarize only",
            description=(
                f"Rules: GPU for Whisper + Ollama only (NLLB on CPU or sequential); "
                f"≥{_PIPELINE_VRAM_FLOOR_GB} GB VRAM preferred; lowest $/hr."
            ),
            kind="gpu",
            picker=_pick_transcribe_summarize,
        ),
        RecommendationCategory(
            id="comfort_24gb",
            title="24 GB comfort tier",
            description="Rules: ≥24 GB VRAM, lowest hourly rate (room for all local models).",
            kind="gpu",
            picker=_pick_comfort_24gb,
        ),
        RecommendationCategory(
            id="budget_gpu",
            title="Lowest-cost GPU",
            description="Rules: any in-stock GPU with a listed price; absolute cheapest $/hr.",
            kind="gpu",
            picker=_pick_budget_gpu,
        ),
        RecommendationCategory(
            id="runpod_community",
            title="Community cloud (cheapest GPU)",
            description=(
                f"Rules: community cloud only, ≥{_PIPELINE_VRAM_FLOOR_GB} GB VRAM, lowest $/hr."
            ),
            kind="gpu",
            picker=_pick_runpod_community_value,
        ),
        RecommendationCategory(
            id="aws_on_demand",
            title="AWS always-on GPU",
            description="Rules: AWS EC2 GPU instance, ≥16 GB VRAM, lowest on-demand $/hr.",
            kind="gpu",
            picker=_pick_aws_reliable_gpu,
        ),
        RecommendationCategory(
            id="cpu_only_budget",
            title="CPU-only fallback",
            description="Rules: no GPU — cheapest CPU pod/instance for I/O-bound or tiny models.",
            kind="cpu",
            picker=_pick_cpu_budget,
        ),
    ]


def _alternates(
    offers: list[ComputeOffer],
    picked: ComputeOffer | None,
    *,
    kind: str,
    limit: int = 3,
) -> list[ComputeOffer]:
    pool = [o for o in offers if o.kind == kind]
    if picked:
        pool = [o for o in pool if offer_to_dict(o)["selection_id"] != offer_to_dict(picked)["selection_id"]]
    pool.sort(key=lambda o: o.price_per_hour)
    return pool[:limit]


def _serverless_row_to_offer(row: dict[str, Any]) -> ComputeOffer:
    return ComputeOffer(
        kind="gpu",
        provider="runpod",
        cloud="runpod-serverless",
        name=str(row.get("name") or "GPU"),
        price_per_hour=float(row.get("price_per_hour") or 0),
        availability=str(row.get("availability") or "Unknown"),
        vram_gb=row.get("vram_gb"),
        resource_id=str(row.get("resource_id") or ""),
        notes=str(row.get("notes") or ""),
        data_center_id=row.get("data_center_id"),
        data_center_name=row.get("data_center_name"),
        serverless_pool=row.get("serverless_pool"),
        gpu_catalog_id=row.get("gpu_catalog_id"),
    )


def _serverless_category_definitions() -> list[RecommendationCategory]:
    whisper = WHISPER_MODEL
    ollama = SUMMARIZATION_MODEL
    return [
        RecommendationCategory(
            id="full_pipeline",
            title="Full video summarizer pipeline",
            description=(
                f"Serverless: Whisper ({whisper}) + Ollama ({ollama}) + NLLB — "
                f"prefer ≥{_PIPELINE_VRAM_COMFORT_GB} GB VRAM in selected region; lowest serverless $/hr."
            ),
            kind="gpu",
            picker=_pick_full_pipeline,
        ),
        RecommendationCategory(
            id="transcribe_and_summarize",
            title="Transcribe + summarize only",
            description=(
                f"Serverless GPU with ≥{_PIPELINE_VRAM_FLOOR_GB} GB VRAM; lowest serverless $/hr in region."
            ),
            kind="gpu",
            picker=_pick_transcribe_summarize,
        ),
        RecommendationCategory(
            id="comfort_24gb",
            title="24 GB comfort tier",
            description="Serverless: ≥24 GB VRAM in selected region; lowest serverless $/hr.",
            kind="gpu",
            picker=_pick_comfort_24gb,
        ),
        RecommendationCategory(
            id="budget_gpu",
            title="Lowest-cost serverless GPU",
            description="Any serverless GPU in the selected region; absolute lowest $/hr.",
            kind="gpu",
            picker=_pick_budget_gpu,
        ),
    ]


def build_serverless_category_recommendations(
    serverless_rows: list[dict[str, Any]],
    *,
    data_center_id: str,
) -> dict[str, Any]:
    """Rule-based picks from RunPod serverless catalog (one region at a time)."""
    dc = (data_center_id or "").strip()
    regional = [r for r in serverless_rows if not dc or r.get("data_center_id") == dc]
    gpu_offers = [_serverless_row_to_offer(r) for r in regional]
    categories_out: list[dict[str, Any]] = []

    for cat in _serverless_category_definitions():
        pick = cat.picker(gpu_offers)
        pick_dict = offer_to_dict(pick) if pick else None
        if pick_dict:
            pick_dict["cloud_badges"] = next(
                (
                    r.get("cloud_badges")
                    for r in regional
                    if r.get("selection_id") == pick_dict.get("selection_id")
                ),
                None,
            )
        categories_out.append(
            {
                "id": cat.id,
                "title": cat.title,
                "description": cat.description,
                "kind": cat.kind,
                "pick": pick_dict,
                "alternates": [],
            }
        )

    primary = next((c for c in categories_out if c["id"] == "full_pipeline"), None)
    return {
        "engine": "rule_based_serverless",
        "region_data_center_id": dc,
        "models": {
            "whisper_model": WHISPER_MODEL,
            "summarization_model": SUMMARIZATION_MODEL,
            "vram_comfort_gb": _PIPELINE_VRAM_COMFORT_GB,
            "vram_min_gb": _PIPELINE_VRAM_MIN_GB,
            "vram_floor_gb": _PIPELINE_VRAM_FLOOR_GB,
        },
        "categories": categories_out,
        "recommended_gpu_for_pipeline": primary.get("pick") if primary else None,
    }


def build_category_recommendations(
    gpu_offers: list[ComputeOffer],
    cpu_offers: list[ComputeOffer],
) -> dict[str, Any]:
    """Compute all category picks from live offer models (rule engine only)."""
    all_offers = gpu_offers + cpu_offers
    categories_out: list[dict[str, Any]] = []

    for cat in _category_definitions():
        pool = gpu_offers if cat.kind == "gpu" else cpu_offers
        pick = cat.picker(all_offers)
        categories_out.append(
            {
                "id": cat.id,
                "title": cat.title,
                "description": cat.description,
                "kind": cat.kind,
                "pick": offer_to_dict(pick) if pick else None,
                "alternates": [
                    offer_to_dict(o)
                    for o in _alternates(pool, pick, kind=cat.kind)
                ],
            }
        )

    primary = next((c for c in categories_out if c["id"] == "full_pipeline"), None)
    return {
        "engine": "rule_based",
        "models": {
            "whisper_model": WHISPER_MODEL,
            "summarization_model": SUMMARIZATION_MODEL,
            "vram_comfort_gb": _PIPELINE_VRAM_COMFORT_GB,
            "vram_min_gb": _PIPELINE_VRAM_MIN_GB,
            "vram_floor_gb": _PIPELINE_VRAM_FLOOR_GB,
        },
        "categories": categories_out,
        # Back-compat field used by older UI copy
        "recommended_gpu_for_pipeline": primary.get("pick") if primary else None,
    }
