"""Per-job translation term log (transliterated vs kept)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class TranslationTermLog:
    def __init__(self, log_path: Path) -> None:
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._scenes: list[dict[str, Any]] = []
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data.get("scenes"), list):
                    self._scenes = list(data["scenes"])
            except (json.JSONDecodeError, OSError):
                self._scenes = []

    def record_scene(
        self,
        *,
        scene_index: int,
        language: str,
        backend: str,
        terms: list[dict[str, Any]],
        from_cache: bool = False,
    ) -> None:
        entry = {
            "scene_index": scene_index,
            "language": language,
            "backend": backend,
            "from_cache": from_cache,
            "terms": terms,
        }
        with self._lock:
            self._scenes = [
                e
                for e in self._scenes
                if not (
                    e.get("scene_index") == scene_index
                    and e.get("language") == language
                    and e.get("backend") == backend
                )
            ]
            self._scenes.append(entry)
            self._flush_unlocked()

    def record_language_failure(self, language: str, backend: str, message: str) -> None:
        with self._lock:
            self._scenes.append(
                {
                    "language": language,
                    "backend": backend,
                    "status": "language_failed",
                    "message": message,
                }
            )
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        payload = {"version": 1, "scenes": self._scenes}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
