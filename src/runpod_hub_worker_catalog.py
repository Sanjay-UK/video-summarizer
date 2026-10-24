"""RunPod Hub / community serverless workers (reference — not our combined handler)."""

from __future__ import annotations

from typing import Any

# Official RunPod Hub repos (each is its own endpoint + API contract).
RUNPOD_HUB_SERVERLESS_WORKERS: list[dict[str, str]] = [
    {
        "id": "worker-faster_whisper",
        "name": "Faster Whisper (RunPod Hub)",
        "hub_path": "runpod-workers/worker-faster_whisper",
        "console_url": "https://console.runpod.io/hub/runpod-workers/worker-faster_whisper",
        "models": "Whisper transcribe only",
        "notes": "Input: audio URL/base64 — not compatible with this app's heavy_phase job.",
    },
    {
        "id": "worker-ollama",
        "name": "Ollama (RunPod Hub)",
        "hub_path": "runpod-workers/worker-ollama",
        "console_url": "https://console.runpod.io/hub/runpod-workers/worker-ollama",
        "models": "Ollama chat/completion only",
        "notes": "Default llama3.2:3b — not compatible with this app's summarize transcript flow.",
    },
    {
        "id": "worker-vllm",
        "name": "vLLM (RunPod Hub)",
        "hub_path": "runpod-workers/worker-vllm",
        "console_url": "https://console.runpod.io/hub/runpod-workers/worker-vllm",
        "models": "LLM inference",
        "notes": "OpenAI-style API — separate from video-summarizer pipeline.",
    },
    {
        "id": "runpod-translate-nllb",
        "name": "NLLB translate (community Hub)",
        "hub_path": "dwin-gharibi/runpod-translate",
        "console_url": "https://github.com/dwin-gharibi/runpod-translate",
        "models": "NLLB-200 / M2M-100 / MarianMT",
        "notes": "Translation-only tasks schema — not this app's nllb_batch jobs.",
    },
]

WORKER_IMAGE_SOURCES: list[dict[str, str]] = [
    {
        "id": "custom_build",
        "label": "Custom worker — auto build & push (Docker Desktop)",
    },
    {
        "id": "registry",
        "label": "Existing registry image — no local Docker",
    },
]

DEFAULT_WORKER_IMAGE_SOURCE = "custom_build"


def hub_catalog_for_ui() -> dict[str, Any]:
    return {
        "worker_image_sources": WORKER_IMAGE_SOURCES,
        "runpod_hub_workers": RUNPOD_HUB_SERVERLESS_WORKERS,
        "combined_worker_note": (
            "RunPod Hub lists separate workers (Whisper-only, Ollama-only, NLLB community repos). "
            "This app uses one custom image (runpod_worker/Dockerfile) that runs Whisper + Ollama + NLLB "
            "with job types heavy_phase, nllb_batch, and warmup. "
            "Deploy Hub templates from the RunPod console for standalone APIs, or paste a Docker image "
            "you built from our Dockerfile via Hub/GitHub without using local Docker."
        ),
    }
