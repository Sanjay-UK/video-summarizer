"""Sequential per-language translation with backend chain and Google circuit breaker."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from src.locales import LocaleSpec, get_locale, text_has_target_script
from src.term_protection import finalize_indic_translation_with_log, translate_text_preserving_english_code
from src.translators.factory import get_highlight_translator
from src.translators.google_backend import (
    GoogleTranslateError,
    google_translate_batch,
    google_translate_text,
    is_rate_limit_error,
)
from src.translators.google_circuit import GoogleJobCircuitBreaker
from src.translators.translation_chain import available_chain
from src.translation import SceneTranslationResult, translate_scene_with_retry
from src.translation_cache import JobTranslationCache
from src.translation_log import TranslationTermLog


@dataclass
class LanguageTranslationOutcome:
    language: str
    spoken_texts: list[Optional[str]]
    scene_results: list[SceneTranslationResult] = field(default_factory=list)
    translation_seconds: float = 0.0
    ok: bool = False
    message: str = ""
    failure_kind: Optional[str] = None  # rate_limit | error
    translator_backend: str = ""


def _failure_kind_from_exc(exc: Exception) -> str:
    return "rate_limit" if is_rate_limit_error(exc) else "error"


def _should_try_next_backend(backend: str, outcome: LanguageTranslationOutcome) -> bool:
    if outcome.ok:
        return False
    if backend == "google":
        return True
    return True


def _translate_google_batch_for_backend(
    locale: LocaleSpec,
    english_texts: list[str],
    *,
    cache: JobTranslationCache,
    term_log: TranslationTermLog,
    backend_name: str = "google",
) -> LanguageTranslationOutcome:
    t0 = time.perf_counter()
    spoken: list[Optional[str]] = [None] * len(english_texts)
    results: list[SceneTranslationResult] = []
    pending_indices: list[int] = []
    pending_texts: list[str] = []

    for idx, en in enumerate(english_texts):
        hit = cache.get(locale.code, backend_name, en)
        if hit is not None:
            spoken[idx] = hit
            results.append(
                SceneTranslationResult(
                    scene_index=idx,
                    source_en=en,
                    translated=hit,
                    ok=True,
                    backend=backend_name,
                    from_cache=True,
                )
            )
            term_log.record_scene(
                scene_index=idx,
                language=locale.code,
                backend=backend_name,
                terms=[],
                from_cache=True,
            )
        else:
            pending_indices.append(idx)
            pending_texts.append(en)

    try:
        if pending_texts:
            needs_code_split = any("`" in t for t in pending_texts)
            try:
                if needs_code_split:

                    def _google_prose(seg: str, lc: str = locale.code) -> str:
                        hit = cache.get(lc, backend_name, seg)
                        if hit is not None:
                            return hit
                        return google_translate_text(seg, lc, throttle=True)

                    raw_batch = [
                        translate_text_preserving_english_code(en, _google_prose)
                        for en in pending_texts
                    ]
                else:
                    raw_batch = google_translate_batch(
                        pending_texts,
                        locale.code,
                        throttle=True,
                    )
            except GoogleTranslateError as exc:
                elapsed = time.perf_counter() - t0
                kind = "rate_limit" if is_rate_limit_error(exc) else "error"
                return LanguageTranslationOutcome(
                    language=locale.code,
                    spoken_texts=spoken,
                    scene_results=results,
                    translation_seconds=elapsed,
                    ok=False,
                    message=str(exc),
                    failure_kind=kind,
                    translator_backend=backend_name,
                )

            if raw_batch is not None and len(raw_batch) == len(pending_texts):
                for idx, raw, en in zip(pending_indices, raw_batch, pending_texts):
                    final, term_entries = finalize_indic_translation_with_log(en, raw, locale)
                    if not final.strip() or not text_has_target_script(final, locale):
                        raise ValueError("batch scene failed script check")
                    spoken[idx] = final
                    cache.set(locale.code, backend_name, en, final)
                    results.append(
                        SceneTranslationResult(
                            scene_index=idx,
                            source_en=en,
                            translated=final,
                            ok=True,
                            backend=backend_name,
                        )
                    )
                    term_log.record_scene(
                        scene_index=idx,
                        language=locale.code,
                        backend=backend_name,
                        terms=term_entries,
                    )
            else:
                print(
                    f"[WARN] Batch translation malformed for {locale.code}; per-scene fallback",
                    file=sys.stderr,
                )
                translator = get_highlight_translator(
                    backend_name,
                    term_log=term_log,
                    cache=cache,
                )
                for idx in pending_indices:
                    en = english_texts[idx]
                    res = translate_scene_with_retry(
                        en,
                        locale,
                        scene_index=idx,
                        translator=translator,
                        backend_name=backend_name,
                    )
                    results.append(res)
                    spoken[idx] = res.translated if res.ok else None

        ok = all(s is not None and str(s).strip() for s in spoken)
        elapsed = time.perf_counter() - t0
        if ok:
            return LanguageTranslationOutcome(
                language=locale.code,
                spoken_texts=spoken,
                scene_results=results,
                translation_seconds=elapsed,
                ok=True,
                translator_backend=backend_name,
            )
        failed_warnings = [r.warning for r in results if not r.ok and r.warning]
        rate_limited = bool(failed_warnings) and all(
            is_rate_limit_error(w) for w in failed_warnings
        )
        kind = "rate_limit" if rate_limited else "error"
        msg = f"Incomplete translation for {locale.code}"
        if rate_limited and failed_warnings:
            msg = failed_warnings[-1]
        return LanguageTranslationOutcome(
            language=locale.code,
            spoken_texts=spoken,
            scene_results=results,
            translation_seconds=elapsed,
            ok=False,
            message=msg,
            failure_kind=kind,
            translator_backend=backend_name,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return LanguageTranslationOutcome(
            language=locale.code,
            spoken_texts=spoken,
            scene_results=results,
            translation_seconds=elapsed,
            ok=False,
            message=str(exc),
            failure_kind=_failure_kind_from_exc(exc),
            translator_backend=backend_name,
        )


def _translate_sequential_for_backend(
    locale: LocaleSpec,
    english_texts: list[str],
    *,
    backend_name: str,
    cache: JobTranslationCache,
    term_log: TranslationTermLog,
) -> LanguageTranslationOutcome:
    t0 = time.perf_counter()
    translator = get_highlight_translator(backend_name, term_log=term_log, cache=cache)
    spoken: list[Optional[str]] = []
    results: list[SceneTranslationResult] = []
    try:
        for idx, en in enumerate(english_texts):
            res = translate_scene_with_retry(
                en,
                locale,
                scene_index=idx,
                translator=translator,
                backend_name=backend_name,
            )
            results.append(res)
            spoken.append(res.translated if res.ok else None)
        ok = all(spoken)
        elapsed = time.perf_counter() - t0
        return LanguageTranslationOutcome(
            language=locale.code,
            spoken_texts=spoken,
            scene_results=results,
            translation_seconds=elapsed,
            ok=ok,
            message="" if ok else f"Scene translation failed for {locale.code}",
            failure_kind=None if ok else "error",
            translator_backend=backend_name if ok else backend_name,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return LanguageTranslationOutcome(
            language=locale.code,
            spoken_texts=spoken,
            scene_results=results,
            translation_seconds=elapsed,
            ok=False,
            message=str(exc),
            failure_kind=_failure_kind_from_exc(exc),
            translator_backend=backend_name,
        )


def translate_language_scenes(
    lang: str,
    english_texts: list[str],
    *,
    cache: JobTranslationCache,
    term_log: TranslationTermLog,
    google_circuit: Optional[GoogleJobCircuitBreaker] = None,
    backend_chain: Optional[tuple[str, ...]] = None,
) -> LanguageTranslationOutcome:
    if lang == "en":
        return LanguageTranslationOutcome(
            language="en",
            spoken_texts=list(english_texts),
            translation_seconds=0.0,
            ok=True,
            translator_backend="source",
        )

    locale = get_locale(lang)
    chain = list(backend_chain) if backend_chain else available_chain()
    last: LanguageTranslationOutcome | None = None

    for backend in chain:
        if backend == "google" and google_circuit is not None and google_circuit.open:
            print(
                f"[INFO] Skipping Google for {lang} (circuit open)",
                file=sys.stderr,
            )
            continue

        print(f"[INFO] Translating {lang} via {backend}", file=sys.stderr)
        if backend == "google":
            outcome = _translate_google_batch_for_backend(
                locale,
                english_texts,
                cache=cache,
                term_log=term_log,
                backend_name="google",
            )
        else:
            outcome = _translate_sequential_for_backend(
                locale,
                english_texts,
                backend_name=backend,
                cache=cache,
                term_log=term_log,
            )

        last = outcome
        if outcome.ok:
            return outcome
        if not _should_try_next_backend(backend, outcome):
            break
        print(
            f"[WARN] {backend} failed for {lang}: {outcome.message}; trying next backend",
            file=sys.stderr,
        )

    if last is None:
        return LanguageTranslationOutcome(
            language=lang,
            spoken_texts=[None] * len(english_texts),
            ok=False,
            message="No translation backends available",
            failure_kind="error",
        )
    return last
