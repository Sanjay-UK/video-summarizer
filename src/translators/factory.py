"""Select highlight translator backend from config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from config import HIGHLIGHT_TRANSLATOR, HIGHLIGHT_TRANSLATION_MODEL
from src.translators.caching import CachingSceneTranslator
from src.translators.google_backend import GoogleSceneTranslator
from src.translators.fixture_backend import FixtureSceneTranslator
from src.translators.nllb_backend import NllbNotAvailableError, NllbSceneTranslator, nllb_backend_available
from src.translators.ollama_backend import OllamaSceneTranslator
from src.term_protection import SceneTranslator, TermProtectedTranslator
from src.translation_cache import JobTranslationCache

if TYPE_CHECKING:
    from src.translation_log import TranslationTermLog


def available_translator_backends() -> dict[str, bool]:
    return {
        "google": True,
        "nllb": nllb_backend_available(),
        "ollama": True,
        "fixture": True,
    }


def get_highlight_translator(
    backend: Optional[str] = None,
    *,
    wrap_term_protection: bool = True,
    term_log: Optional["TranslationTermLog"] = None,
    cache: Optional[JobTranslationCache] = None,
) -> SceneTranslator:
    name = (backend or HIGHLIGHT_TRANSLATOR or "google").strip().lower()
    if name == "google":
        inner: SceneTranslator = GoogleSceneTranslator()
    elif name == "nllb":
        if not nllb_backend_available():
            raise NllbNotAvailableError(
                "NLLB backend not installed. See SETUP.md / translations_compare.md Windows steps."
            )
        inner = NllbSceneTranslator()
    elif name == "ollama":
        inner = OllamaSceneTranslator(model=HIGHLIGHT_TRANSLATION_MODEL)
    elif name == "fixture":
        inner = FixtureSceneTranslator()
    else:
        raise ValueError(f"Unknown HIGHLIGHT_TRANSLATOR: {name!r} (use google, nllb, ollama, or fixture)")

    if wrap_term_protection:
        inner = TermProtectedTranslator(inner, term_log=term_log, backend=name)

    if cache is not None:
        return CachingSceneTranslator(inner, cache, backend=name, term_log=term_log)

    return inner
