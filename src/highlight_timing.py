"""Single source of truth for highlight scene display duration."""

from __future__ import annotations

from config import HIGHLIGHT_MIN_DISPLAY_SEC


def scene_display_duration(narration_seconds: float) -> float:
    """Seconds each scene stays on screen: max(narration, 5.5)."""
    return max(float(narration_seconds), HIGHLIGHT_MIN_DISPLAY_SEC)
