"""Block live Google in tests unless --live (production unaffected)."""

from __future__ import annotations

import os

_BLOCK = os.environ.get("HIGHLIGHT_BLOCK_LIVE_GOOGLE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_LIVE_OVERRIDE = os.environ.get("HIGHLIGHT_LIVE_GOOGLE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def configure_test_google_policy(*, live: bool) -> None:
    """Call from test/benchmark scripts before importing the pipeline."""
    global _BLOCK, _LIVE_OVERRIDE
    os.environ["HIGHLIGHT_BLOCK_LIVE_GOOGLE"] = "1"
    os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "0" if live else "1"
    _BLOCK = True
    _LIVE_OVERRIDE = live
    if live:
        os.environ["HIGHLIGHT_LIVE_GOOGLE"] = "1"
        os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "0"
        os.environ["HIGHLIGHT_TRANSLATION_CHAIN"] = "google"
    else:
        os.environ.pop("HIGHLIGHT_LIVE_GOOGLE", None)
        os.environ["HIGHLIGHT_DISABLE_GOOGLE"] = "1"
        os.environ["HIGHLIGHT_TRANSLATION_CHAIN"] = "fixture"


def require_live_google() -> None:
    if not _BLOCK:
        return
    if _LIVE_OVERRIDE:
        return
    from src.translators.google_backend import GoogleTranslateError

    raise GoogleTranslateError(
        "Live Google Translate blocked in tests (pass --live for one real call)"
    )
