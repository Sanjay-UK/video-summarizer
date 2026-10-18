"""Deterministic Ollama options for highlight segmentation and narration."""

from __future__ import annotations

from config import HIGHLIGHT_OLLAMA_SEED, HIGHLIGHT_OLLAMA_TEMPERATURE


def highlight_ollama_options(*, num_ctx: int = 8192) -> dict:
    return {
        "temperature": HIGHLIGHT_OLLAMA_TEMPERATURE,
        "seed": HIGHLIGHT_OLLAMA_SEED,
        "top_p": 1.0,
        "num_ctx": num_ctx,
    }
