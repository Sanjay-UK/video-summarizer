"""Where summarization runs: local Flask host vs hybrid RunPod GPU pod."""

from __future__ import annotations

import os
from typing import Any

from src.compute_deploy import (
    COMPUTE_MODE_SERVERLESS,
    load_compute_mode,
    load_saved_selection,
    _write_selection,
)
from src.compute_status import get_compute_deploy_status
from src.gpu_selection import load_env_file

RUNTIME_LOCAL = "local"
RUNTIME_RUNPOD = "runpod"
VALID_RUNTIMES = frozenset({RUNTIME_LOCAL, RUNTIME_RUNPOD})


def load_pipeline_runtime() -> str:
    saved = load_saved_selection()
    if not saved:
        return RUNTIME_LOCAL
    value = (saved.get("pipeline_runtime") or RUNTIME_LOCAL).strip().lower()
    return value if value in VALID_RUNTIMES else RUNTIME_LOCAL


def save_pipeline_runtime(runtime: str) -> dict[str, Any]:
    runtime = (runtime or "").strip().lower()
    if runtime not in VALID_RUNTIMES:
        return {"ok": False, "error": f"runtime must be one of: {', '.join(sorted(VALID_RUNTIMES))}"}

    saved = load_saved_selection() or {}
    saved["pipeline_runtime"] = runtime
    _write_selection(saved)
    return {"ok": True, "pipeline_runtime": runtime}


def get_pipeline_runtime_status() -> dict[str, Any]:
    runtime = load_pipeline_runtime()
    deploy = get_compute_deploy_status(refresh_live=True)
    phase = deploy.get("phase")
    compute_mode = deploy.get("compute_mode") or load_compute_mode()
    if compute_mode == COMPUTE_MODE_SERVERLESS:
        endpoint_ready = bool(deploy.get("endpoint_id")) and deploy.get("deploy_record_status") == "deployed"
        heavy_remote = runtime == RUNTIME_RUNPOD and endpoint_ready
    else:
        endpoint_ready = False
        heavy_remote = runtime == RUNTIME_RUNPOD and phase == "running"
    return {
        "pipeline_runtime": runtime,
        "compute_mode": compute_mode,
        "execution_mode": "hybrid_runpod" if runtime == RUNTIME_RUNPOD else "local",
        "runpod_pod_running": phase == "running",
        "runpod_phase": phase,
        "runpod_pod_id": deploy.get("pod_id"),
        "runpod_endpoint_id": deploy.get("endpoint_id"),
        "runpod_hardware": deploy.get("hardware"),
        "runpod_ssh_command": deploy.get("ssh_command"),
        "heavy_steps_remote": heavy_remote,
        "light_steps_local": True,
    }


def resolve_runtime_for_job(requested: str | None = None) -> dict[str, Any]:
    """
    local — all steps on this machine.

    hybrid_runpod (GPU toggle) — heavy inference on pod via SSH:
      Whisper (CUDA), Ollama summarization, Google+NLLB summary translation;
      highlight NLLB fallbacks also route to pod when chain uses NLLB.
    Light steps always local: download, audio extract, Edge TTS, highlight
    visuals/assembly, FFmpeg, orchestration.
    """
    load_env_file()
    runtime = (requested or load_pipeline_runtime()).strip().lower()
    if runtime not in VALID_RUNTIMES:
        runtime = RUNTIME_LOCAL

    local_ollama = os.environ.get("OLLAMA_HOST", "")

    if runtime == RUNTIME_LOCAL:
        return {
            "runtime": RUNTIME_LOCAL,
            "mode": "local",
            "whisper_device": "cpu",
            "ollama_host": local_ollama,
            "location_label": "this machine (full pipeline local)",
            "heavy_location": "local",
            "light_location": "local",
            "error": None,
        }

    deploy = get_compute_deploy_status(refresh_live=True)
    compute_mode = deploy.get("compute_mode") or load_compute_mode()

    if compute_mode == COMPUTE_MODE_SERVERLESS:
        endpoint_id = (deploy.get("endpoint_id") or "").strip()
        if deploy.get("deploy_record_status") != "deployed" or not endpoint_id:
            msg = (
                "Serverless GPU mode requires a deployed endpoint. "
                "Save GPU selection on the GPU page (set RUNPOD_SERVERLESS_IMAGE and RUNPOD_DATA_CENTER_ID)."
            )
            return {"runtime": RUNTIME_RUNPOD, "mode": "hybrid_runpod", "error": msg}
        return {
            "runtime": RUNTIME_RUNPOD,
            "mode": "hybrid_runpod",
            "compute_mode": COMPUTE_MODE_SERVERLESS,
            "whisper_device": "cuda",
            "ollama_host": os.environ.get("RUNPOD_OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434",
            "location_label": (
                "hybrid — heavy steps on GPU, highlight FFmpeg local"
            ),
            "heavy_location": "runpod_serverless",
            "light_location": "local",
            "runpod_endpoint_id": endpoint_id,
            "error": None,
        }

    if deploy.get("phase") != "running":
        msg = (
            "GPU pod mode requires a running pod. "
            "Deploy from GPU selection or choose This machine."
        )
        return {"runtime": RUNTIME_RUNPOD, "mode": "hybrid_runpod", "error": msg}

    pod_id = deploy.get("pod_id") or "unknown"
    # On-pod Ollama for remote summarize step (worker sets default 127.0.0.1:11434).
    runpod_ollama = os.environ.get("RUNPOD_OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"

    return {
        "runtime": RUNTIME_RUNPOD,
        "mode": "hybrid_runpod",
        "whisper_device": "cuda",
        "ollama_host": runpod_ollama,
        "location_label": "hybrid — heavy steps on GPU pod, light steps local",
        "heavy_location": "runpod",
        "light_location": "local",
        "runpod_pod_id": pod_id,
        "runpod_ssh_command": deploy.get("ssh_command"),
        "error": None,
    }
