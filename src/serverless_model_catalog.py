"""Models supported on the RunPod serverless worker (GPU selection UI)."""

from __future__ import annotations

import os
from typing import Any

from src.translators.nllb_backend import _MODEL_ID as NLLB_MODEL_ID

SERVERLESS_WHISPER_MODELS: list[dict[str, str]] = [
    {"id": "tiny", "label": "Whisper tiny (fastest, short clips)"},
    {"id": "base", "label": "Whisper base (default balance)"},
    {"id": "large-v3", "label": "Whisper large-v3 (best quality, more VRAM)"},
]

SERVERLESS_OLLAMA_MODELS: list[dict[str, str]] = [
    {"id": "llama3.2:3b", "label": "Llama 3.2 3B (default)"},
    {"id": "qwen2.5:7b-instruct", "label": "Qwen 2.5 7B Instruct"},
]

SERVERLESS_NLLB_MODELS: list[dict[str, str]] = [
    {"id": NLLB_MODEL_ID, "label": "NLLB-200 distilled 600M (fixed worker build)"},
]

DEFAULT_SERVERLESS_MODELS: dict[str, str] = {
    "whisper": "base",
    "ollama": "llama3.2:3b",
    "nllb": NLLB_MODEL_ID,
    "worker_image_source": "custom_build",
    "registry_image": "",
}


def normalize_serverless_models(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and fill defaults for saved/API model picks."""
    allowed_whisper = {m["id"] for m in SERVERLESS_WHISPER_MODELS}
    allowed_ollama = {m["id"] for m in SERVERLESS_OLLAMA_MODELS}
    allowed_nllb = {m["id"] for m in SERVERLESS_NLLB_MODELS}
    src = raw or {}
    whisper = (src.get("whisper") or DEFAULT_SERVERLESS_MODELS["whisper"]).strip()
    ollama = (src.get("ollama") or DEFAULT_SERVERLESS_MODELS["ollama"]).strip()
    nllb = (src.get("nllb") or DEFAULT_SERVERLESS_MODELS["nllb"]).strip()
    if whisper not in allowed_whisper:
        whisper = DEFAULT_SERVERLESS_MODELS["whisper"]
    if ollama not in allowed_ollama:
        ollama = DEFAULT_SERVERLESS_MODELS["ollama"]
    if nllb not in allowed_nllb:
        nllb = DEFAULT_SERVERLESS_MODELS["nllb"]
    from src.runpod_hub_worker_catalog import DEFAULT_WORKER_IMAGE_SOURCE, WORKER_IMAGE_SOURCES

    allowed_sources = {s["id"] for s in WORKER_IMAGE_SOURCES}
    source = str(src.get("worker_image_source") or DEFAULT_WORKER_IMAGE_SOURCE).strip()
    if source not in allowed_sources:
        source = DEFAULT_WORKER_IMAGE_SOURCE
    registry_image = str(src.get("registry_image") or "").strip()
    if source == "registry" and not registry_image:
        env_img = os.environ.get("RUNPOD_SERVERLESS_IMAGE", "").strip()
        if env_img:
            registry_image = env_img
    return {
        "whisper": whisper,
        "ollama": ollama,
        "nllb": nllb,
        "worker_image_source": source,
        "registry_image": registry_image,
    }


def catalog_for_ui() -> dict[str, Any]:
    from src.runpod_hub_worker_catalog import hub_catalog_for_ui

    return {
        "whisper": SERVERLESS_WHISPER_MODELS,
        "ollama": SERVERLESS_OLLAMA_MODELS,
        "nllb": SERVERLESS_NLLB_MODELS,
        "defaults": DEFAULT_SERVERLESS_MODELS,
        **hub_catalog_for_ui(),
    }


def load_saved_serverless_models() -> dict[str, Any]:
    from src.compute_deploy import load_saved_selection

    saved = load_saved_selection()
    if not saved:
        return dict(DEFAULT_SERVERLESS_MODELS)
    return normalize_serverless_models(saved.get("serverless_models"))
