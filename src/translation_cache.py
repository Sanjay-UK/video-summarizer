"""Per-job highlight translation cache (language + backend + source text hash)."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Optional


def translation_cache_key(language: str, backend: str, english_text: str) -> str:
    digest = hashlib.sha256(english_text.strip().encode("utf-8")).hexdigest()[:20]
    return f"{language}:{backend}:{digest}"


class JobTranslationCache:
    def __init__(self, cache_path: Path) -> None:
        self.path = Path(cache_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: dict[str, str] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data.get("entries"), dict):
                    self._entries = {str(k): str(v) for k, v in data["entries"].items()}
            except (json.JSONDecodeError, OSError):
                self._entries = {}

    def get(self, language: str, backend: str, english_text: str) -> Optional[str]:
        key = translation_cache_key(language, backend, english_text)
        with self._lock:
            return self._entries.get(key)

    def set(self, language: str, backend: str, english_text: str, translated: str) -> None:
        key = translation_cache_key(language, backend, english_text)
        with self._lock:
            self._entries[key] = translated
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        payload = {"version": 1, "entries": self._entries}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
