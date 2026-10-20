"""Backend chain: google -> nllb (if installed) -> ollama."""

from __future__ import annotations

import os

from config import HIGHLIGHT_TRANSLATOR
from src.translators.factory import available_translator_backends

DEFAULT_CHAIN = ("google", "nllb", "ollama")


def translation_backend_chain() -> tuple[str, ...]:
    raw = os.environ.get("HIGHLIGHT_TRANSLATION_CHAIN", "").strip()
    if raw:
        parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
        return parts if parts else DEFAULT_CHAIN
    # Single-backend env still wins for explicit nllb/ollama-only jobs
    primary = (HIGHLIGHT_TRANSLATOR or "google").strip().lower()
    if primary in ("fixture",):
        return (primary,)
    if primary != "google":
        return (primary, "ollama") if primary != "ollama" else ("ollama",)
    return DEFAULT_CHAIN


def available_chain() -> list[str]:
    avail = available_translator_backends()
    avail["fixture"] = True
    avail["google"] = True
    out: list[str] = []
    for name in translation_backend_chain():
        if name == "nllb" and not avail.get("nllb"):
            continue
        if name == "ollama":
            out.append(name)
        elif name == "google":
            out.append(name)
        elif name == "fixture":
            out.append(name)
        elif avail.get(name):
            out.append(name)
    return out
