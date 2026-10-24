"""
RunPod Serverless endpoint settings (platform defaults + documented workload exceptions).

Canonical reference: docs/RUNPOD_SERVERLESS.md
RunPod docs: https://docs.runpod.io/serverless/endpoints/endpoint-configurations
"""

from __future__ import annotations

# Workers — scale to zero (RunPod default active workers = 0)
WORKERS_MIN = 0
WORKERS_MAX = 3

# Idle timeout — RunPod platform default 5s (worker billed until scale-down)
IDLE_TIMEOUT_SEC = 5

# Execution timeout — RunPod default 600s; long lecture transcribe+summarize needs more.
# Set on endpoint only (typical GPU job duration + buffer), not tied to local machine uptime.
EXECUTION_TIMEOUT_MS = 3_600_000  # 1 hour

# Autoscaling — RunPod defaults
SCALING_TYPE = "QUEUE_DELAY"
QUEUE_DELAY_SEC = 4

# FlashBoot — enabled per RunPod default for faster revival
FLASHBOOT = "FLASHBOOT"

# Worker init — extend when multi-model cold start can exceed ~7 minutes
RUNPOD_INIT_TIMEOUT_ENV = "800"

# Network volume for model caches
NETWORK_VOLUME_SIZE_GB = 100
NETWORK_VOLUME_TYPE = "HIGH_PERFORMANCE"
NETWORK_VOLUME_NAME_PREFIX = "video-summarizer-models"

# Container disk on worker (hydrate + temp files)
CONTAINER_DISK_GB = 64

ENDPOINT_NAME_PREFIX = "video-summarizer"

DOCS_URL = "https://docs.runpod.io/serverless/endpoints/endpoint-configurations"


def endpoint_env_vars() -> dict[str, str]:
    return {
        "RUNPOD_INIT_TIMEOUT": RUNPOD_INIT_TIMEOUT_ENV,
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "WHISPER_DEVICE": "cuda",
        "NLLB_VIA_RUNPOD": "0",
    }


def workers_block() -> dict[str, int]:
    return {
        "min": WORKERS_MIN,
        "max": WORKERS_MAX,
        "idleTimeout": IDLE_TIMEOUT_SEC,
    }


def scaling_block() -> dict[str, int | str]:
    return {"type": SCALING_TYPE, "queueDelay": QUEUE_DELAY_SEC}
