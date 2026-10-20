"""Cache wrapper for scene translators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.locales import LocaleSpec
from src.term_protection import SceneTranslator
from src.translation_cache import JobTranslationCache

if TYPE_CHECKING:
    from src.translation_log import TranslationTermLog


class CachingSceneTranslator:
    def __init__(
        self,
        inner: SceneTranslator,
        cache: JobTranslationCache,
        *,
        backend: str,
        term_log: Optional["TranslationTermLog"] = None,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.backend = backend
        self.term_log = term_log

    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        if locale.code == "en":
            return english_text.strip()

        cached = self.cache.get(locale.code, self.backend, english_text)
        if cached is not None:
            if self.term_log is not None:
                self.term_log.record_scene(
                    scene_index=scene_index,
                    language=locale.code,
                    backend=self.backend,
                    terms=[],
                    from_cache=True,
                )
            return cached

        out = self.inner.translate(english_text, locale, scene_index=scene_index)
        self.cache.set(locale.code, self.backend, english_text, out)
        return out
