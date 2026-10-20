"""Pluggable per-scene narration translation for highlight video."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional

from src.locales import LocaleSpec, text_has_target_script
from src.term_protection import SceneTranslator, TermProtectedTranslator
from src.translators.factory import get_highlight_translator
from src.translators.google_backend import GoogleTranslateError

__all__ = [
    "SceneTranslator",
    "TermProtectedTranslator",
    "SceneTranslationResult",
    "LanguageTranslationAborted",
    "translate_scene_with_retry",
    "translate_all_scenes",
]


@dataclass
class SceneTranslationResult:
    scene_index: int
    source_en: str
    translated: str
    ok: bool
    warning: str = ""
    backend: str = ""
    from_cache: bool = False


class LanguageTranslationAborted(Exception):
    """Stop translating remaining scenes for this language (e.g. Google 429)."""

    def __init__(self, language: str, message: str) -> None:
        self.language = language
        self.message = message
        super().__init__(message)


def _is_backend_hard_failure(backend: str, warning: str) -> bool:
    w = warning.lower()
    if backend == "google":
        return "429" in w or "google translate failed" in w or isinstance(
            warning, str
        ) and "rate limit" in w
    return False


def translate_scene_with_retry(
    english_text: str,
    locale: LocaleSpec,
    *,
    scene_index: int,
    translator: Optional[SceneTranslator] = None,
    backend_name: str = "",
) -> SceneTranslationResult:
    if locale.code == "en":
        return SceneTranslationResult(
            scene_index=scene_index,
            source_en=english_text,
            translated=english_text,
            ok=True,
            backend=backend_name or "en",
        )

    tr = translator or get_highlight_translator()
    last_warning = ""

    for attempt in (1, 2):
        try:
            translated = tr.translate(english_text, locale, scene_index=scene_index)
        except GoogleTranslateError as exc:
            last_warning = f"translation error: {exc}"
            print(
                f"[WARN] Scene {scene_index} {locale.code} attempt {attempt}: {last_warning}",
                file=sys.stderr,
            )
            break
        except Exception as exc:
            last_warning = f"translation error: {exc}"
            print(
                f"[WARN] Scene {scene_index} {locale.code} attempt {attempt}: {last_warning}",
                file=sys.stderr,
            )
            continue

        if not translated.strip():
            last_warning = "empty translation"
        elif not text_has_target_script(translated, locale):
            last_warning = f"missing {locale.name} script characters"
        else:
            return SceneTranslationResult(
                scene_index=scene_index,
                source_en=english_text,
                translated=translated,
                ok=True,
                backend=backend_name,
            )

        print(
            f"[WARN] Scene {scene_index} {locale.code} attempt {attempt}: {last_warning}",
            file=sys.stderr,
        )

    return SceneTranslationResult(
        scene_index=scene_index,
        source_en=english_text,
        translated="",
        ok=False,
        warning=last_warning or "translation failed",
        backend=backend_name,
    )


def translate_all_scenes(
    english_texts: list[str],
    locale: LocaleSpec,
    *,
    translator: Optional[SceneTranslator] = None,
    backend_name: str = "",
    on_scene: Optional[Callable[[SceneTranslationResult], None]] = None,
    google_fail_abort_after: int = 2,
) -> tuple[list[Optional[str]], list[SceneTranslationResult]]:
    """Returns parallel list of spoken text (None = skip scene) and full results."""
    spoken: list[Optional[str]] = []
    results: list[SceneTranslationResult] = []
    consecutive_backend_failures = 0

    for idx, en in enumerate(english_texts):
        res = translate_scene_with_retry(
            en,
            locale,
            scene_index=idx,
            translator=translator,
            backend_name=backend_name,
        )
        results.append(res)
        if on_scene:
            on_scene(res)

        if res.ok:
            consecutive_backend_failures = 0
            spoken.append(res.translated)
            continue

        spoken.append(None)
        if _is_backend_hard_failure(backend_name, res.warning):
            consecutive_backend_failures += 1
            if consecutive_backend_failures >= google_fail_abort_after:
                msg = (
                    f"{backend_name} translation stopped after {consecutive_backend_failures} "
                    f"failures ({res.warning}). Remaining scenes skipped for {locale.code}."
                )
                raise LanguageTranslationAborted(locale.code, msg)

    return spoken, results
