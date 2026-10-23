"""Pipeline model configuration and lightweight local runtime probes."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import config
from src.translators.nllb_backend import _MODEL_ID as NLLB_MODEL_ID


def _probe_ollama_tags(host: str, timeout: float = 2.0) -> tuple[list[str], str | None]:
    base = host.rstrip("/")
    url = f"{base}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = []
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model")
            if name:
                names.append(str(name))
        return names, None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        return [], str(exc)


def _ollama_has_model(tags: list[str], wanted: str) -> bool:
    wanted = wanted.strip().lower()
    for tag in tags:
        t = tag.lower()
        if t == wanted or t.startswith(wanted + ":"):
            return True
    return False


def get_pipeline_model_status() -> dict[str, Any]:
    """Models the app uses; probe Ollama locally when reachable."""
    whisper_device = os.environ.get("WHISPER_DEVICE", config.WHISPER_DEVICE)
    ollama_host = os.environ.get("OLLAMA_HOST", config.OLLAMA_HOST)
    ollama_tags, ollama_err = _probe_ollama_tags(ollama_host)
    summary_model = os.environ.get("SUMMARIZATION_MODEL", config.SUMMARIZATION_MODEL)

    def ollama_status(model_name: str) -> str:
        if ollama_err:
            return "not reachable (start Ollama locally or on pod)"
        if _ollama_has_model(ollama_tags, model_name):
            return "loaded (Ollama)"
        return "configured — pull on pod before jobs"

    models: list[dict[str, Any]] = [
        {
            "role": "Transcription",
            "engine": "faster-whisper",
            "model": os.environ.get("WHISPER_MODEL", config.WHISPER_MODEL),
            "detail": f"short clips: {config.WHISPER_MODEL_SHORT}",
            "device": whisper_device,
            "status": "configured for pod GPU" if whisper_device == "cuda" else "configured (CPU in config)",
        },
        {
            "role": "Summarization",
            "engine": "Ollama",
            "model": summary_model,
            "detail": ollama_host,
            "device": "ollama",
            "status": ollama_status(summary_model),
        },
        {
            "role": "Summary translation",
            "engine": "Google + NLLB fallback",
            "model": NLLB_MODEL_ID,
            "detail": "chain: google → nllb",
            "device": "GPU if NLLB used",
            "status": "configured (NLLB lazy-load on first use)",
        },
        {
            "role": "Highlight translation",
            "engine": config.HIGHLIGHT_TRANSLATOR,
            "model": config.HIGHLIGHT_TRANSLATION_MODEL
            if config.HIGHLIGHT_TRANSLATOR == "ollama"
            else config.HIGHLIGHT_TRANSLATOR,
            "detail": "highlights / multilang",
            "device": "network" if config.HIGHLIGHT_TRANSLATOR == "google" else "ollama",
            "status": (
                ollama_status(config.HIGHLIGHT_TRANSLATION_MODEL)
                if config.HIGHLIGHT_TRANSLATOR == "ollama"
                else "configured (Google API)"
            ),
        },
        {
            "role": "Highlight TTS",
            "engine": "Edge TTS",
            "model": config.HIGHLIGHT_TTS_VOICE,
            "detail": "narration audio",
            "device": "network",
            "status": "configured",
        },
    ]

    return {
        "source": "config.py + local Ollama probe",
        "ollama_reachable": ollama_err is None,
        "ollama_error": ollama_err,
        "models": models,
        "note": (
            "RunPod does not expose model load state on the pod API; "
            "Ollama/Whisper status here reflects this Flask host unless you run Ollama on the pod and tunnel."
        ),
    }
