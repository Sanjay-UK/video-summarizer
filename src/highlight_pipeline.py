"""End-to-end highlight video pipeline (multi-language, opt-in)."""

from __future__ import annotations
from src.user_messages import GENERIC_HIGHLIGHT_FAILURE, public_error
from src.log_safety import redact, safe_exc

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Union

from config import HIGHLIGHT_RENDER_MAX_PARALLEL_LANGS, HIGHLIGHT_TRANSLATOR
from src.highlight_assembler import HIGHLIGHT_NARRATION_FILE, concat_scenes, ensure_narration_track
from src.highlight_shared_video import (
    copy_master_narration,
    ensure_shared_scene_visuals,
    load_master_narration,
    mux_language_scene,
    read_scene_timing_manifest,
)
from src.highlight_ffmpeg import require_ffmpeg_subtitles_filter
from src.highlight_captions import build_scene_ass
from src.highlight_jobs import (
    highlight_file_for_lang,
    merge_language_status,
    read_highlight_status,
    write_highlight_status,
)
from src.highlight_timing import scene_display_duration
from src.highlight_translate import LanguageTranslationOutcome, translate_language_scenes
from src.translators.google_circuit import (
    GoogleJobCircuitBreaker,
    reset_google_circuit_breaker,
    set_google_circuit_breaker,
)
from src.highlight_tts import generate_scene_narration
from src.job_video import copy_source_video_to_job, job_source_video_path
from src.locales import get_locale, parse_language_list
from src.narration_rewrite import max_narration_words, rewrite_for_speech
from src.topic_segmentation import TopicScene, run_topic_segmentation, write_highlight_scenes
from src.translation import SceneTranslationResult
from src.translation_cache import JobTranslationCache
from src.translation_log import TranslationTermLog
from src.visual_extractor import VisualSelection, extract_visuals_for_scenes, write_visual_manifest


def _translate_highlight_language(
    lang: str,
    english_texts: list[str],
    *,
    trans_cache: JobTranslationCache,
    term_log: TranslationTermLog,
    google_breaker: GoogleJobCircuitBreaker,
) -> tuple[str, LanguageTranslationOutcome]:
    """Worker: bind shared Google circuit breaker in this thread (contextvars)."""
    token = set_google_circuit_breaker(google_breaker)
    try:
        print(f"[INFO] Translating highlight language (parallel): {lang}", file=sys.stderr)
        outcome = translate_language_scenes(
            lang,
            english_texts,
            cache=trans_cache,
            term_log=term_log,
            google_circuit=google_breaker,
        )
        return lang, outcome
    finally:
        reset_google_circuit_breaker(token)


def resolve_job_video(video_path: Path, job_output_dir: Path) -> Path:
    staged = job_source_video_path(job_output_dir)
    if staged.is_file():
        return staged
    return copy_source_video_to_job(video_path, job_output_dir)


def _load_or_build_shared(
    segments: list[dict[str, Any]],
    staged_video: Path,
    job_output_dir: Path,
    *,
    model: Optional[str],
    max_scenes: int,
    compression: str,
    rebuild: bool = False,
) -> tuple[list[TopicScene], list[VisualSelection], list[str]]:
    scenes_path = job_output_dir / "highlight_scenes.json"
    visual_manifest_path = job_output_dir / "visual_manifest.json"
    en_narr_path = job_output_dir / "highlights" / "_shared" / "english_narrations.json"

    if (
        not rebuild
        and scenes_path.is_file()
        and visual_manifest_path.is_file()
        and en_narr_path.is_file()
    ):
        scenes_data = json.loads(scenes_path.read_text(encoding="utf-8"))
        scene_list = scenes_data.get("scenes", [])
        scenes = [
            TopicScene(
                segment_id=int(s["segment_id"]),
                source_time=float(s["source_time"]),
                topic_title=str(s.get("topic_title", "")),
                summary_text=str(s["summary_text"]),
            )
            for s in scene_list
        ]
        if max_scenes > 0:
            scenes = scenes[:max_scenes]
        vis_data = json.loads(visual_manifest_path.read_text(encoding="utf-8"))
        vis_list = vis_data.get("visuals", [])
        visuals = [
            VisualSelection(
                scene_index=int(v["scene_index"]),
                mode=v["mode"],
                source_time=float(v.get("source_time", 0)),
                visual_path=Path(v["visual_path"]),
                sharpness_score=v.get("sharpness_score"),
                motion_score=v.get("motion_score"),
            )
            for v in vis_list
        ]
        if max_scenes > 0:
            visuals = visuals[:max_scenes]
        en_payload = json.loads(en_narr_path.read_text(encoding="utf-8"))
        english_texts = [str(x) for x in en_payload.get("texts", [])]
        if len(english_texts) == len(scenes):
            return scenes, visuals, english_texts

    scenes = run_topic_segmentation(segments, model=model)
    if max_scenes > 0:
        scenes = scenes[:max_scenes]

    write_highlight_scenes(scenes_path, scenes)

    visuals = extract_visuals_for_scenes(staged_video, scenes, job_output_dir)
    write_visual_manifest(visual_manifest_path, visuals)

    english_texts: list[str] = []
    for scene in scenes:
        spoken = rewrite_for_speech(
            scene.summary_text,
            scene.topic_title,
            model=model,
            compression=compression,
        )
        english_texts.append(spoken)

    shared_dir = job_output_dir / "highlights" / "_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    with open(en_narr_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "compression": compression,
                "max_words_per_scene": max_narration_words(compression),
                "texts": english_texts,
                "topic_titles": [s.topic_title for s in scenes],
            },
            f,
            indent=2,
        )

    return scenes, visuals, english_texts


def summarize_narration_voices(narration_manifest: list) -> list:
    """Distinct narration voices used across scenes, e.g. ["en-US-GuyNeural"] or ["gtts:en"].

    Recorded in the language status so a failed or fallback TTS run shows which provider produced the audio.
    """
    return sorted({str(s.get("tts_voice") or "unknown") for s in narration_manifest})


def _write_lang_failure(
    job_output_dir: Path,
    lang: str,
    message: str,
    *,
    failure_kind: Optional[str],
    translation_seconds: float = 0.0,
    translator_backend: str = "",
) -> None:
    raw = redact(message)  # never keep secrets; the raw text stays on the console for debugging
    print(f"[highlight] {lang} failed: {raw}", file=sys.stderr)
    # The status file is shown in the UI, so it gets only user-safe text.
    message = public_error(raw, default=GENERIC_HIGHLIGHT_FAILURE)
    highlight_root = job_output_dir / "highlights" / lang
    highlight_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "language": lang,
        "status": "failed",
        "translator": translator_backend or HIGHLIGHT_TRANSLATOR,
        "message": message,
        "failure_kind": failure_kind,
        "translation_seconds": round(translation_seconds, 3),
    }
    (highlight_root / "language_status.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    merge_language_status(job_output_dir, lang, payload)


def record_unrecorded_highlight_failures(
    job_output_dir: Path,
    lang_codes: list[str],
    message: str,
    *,
    failure_kind: str,
) -> None:
    """Mark each requested language that has neither a file nor a status record as failed.

    Used when the highlight step ends without reporting on a language, for example when it raises before any language
    starts. Without a record the language shows as "not generated", with no reason and no retry.
    """
    recorded = read_highlight_status(job_output_dir).get("languages") or {}
    for code in lang_codes:
        if not highlight_file_for_lang(job_output_dir, code).is_file() and code not in recorded:
            _write_lang_failure(job_output_dir, code, message, failure_kind=failure_kind)


def _render_language_media(
    lang: str,
    scenes: list[TopicScene],
    visuals: list[VisualSelection],
    spoken_texts: list[str],
    english_texts: list[str],
    job_output_dir: Path,
    *,
    compression: str,
    translation_seconds: float,
    translator_backend: str,
) -> Path:
    locale = get_locale(lang)
    highlight_root = job_output_dir / "highlights" / lang
    highlight_root.mkdir(parents=True, exist_ok=True)

    timing_rows = read_scene_timing_manifest(job_output_dir)
    timing_by_index = {int(r["scene_index"]): float(r["display_seconds"]) for r in timing_rows}

    tts_start = time.perf_counter()
    scene_videos: list[Path] = []
    narration_manifest: list[dict[str, Any]] = []
    reused_narration = 0
    if lang == "en":
        # A previous English track may be out of date. Remove it so a failed render cannot leave it behind.
        (highlight_root / HIGHLIGHT_NARRATION_FILE).unlink(missing_ok=True)

    for scene, visual, spoken in zip(scenes, visuals, spoken_texts):
        if not spoken:
            continue

        scene_dir = highlight_root / f"scene_{visual.scene_index:03d}"
        scene_dir.mkdir(parents=True, exist_ok=True)
        (scene_dir / "narration.txt").write_text(spoken, encoding="utf-8")

        display_sec = timing_by_index.get(visual.scene_index)
        if display_sec is None:
            raise RuntimeError(f"Missing shared timing for scene {visual.scene_index}")

        tts = None
        if lang == "en":
            # The timing pass already synthesized this English text. Reuse that audio instead of a second TTS call.
            master = load_master_narration(job_output_dir, visual.scene_index, spoken)
            if master is not None:
                tts = copy_master_narration(master, scene_dir / "narration.mp3")
                reused_narration += 1
        if tts is None:
            tts = generate_scene_narration(spoken, scene_dir / "narration.mp3", locale=locale)
        ass_path = build_scene_ass(
            tts,
            display_sec,
            scene_dir / "captions.ass",
            locale=locale,
        )

        final_scene = mux_language_scene(
            job_output_dir,
            visual.scene_index,
            display_sec,
            tts.audio_path,
            ass_path,
            scene_dir / "build" / f"scene_{visual.scene_index:03d}_final.mp4",
        )
        scene_videos.append(final_scene)

        narration_manifest.append(
            {
                "scene_index": visual.scene_index,
                "topic_title": scene.topic_title,
                "spoken_text": spoken,
                "source_en": english_texts[visual.scene_index] if lang != "en" else spoken,
                "tts_boundary_kind": tts.boundary_kind,
                "tts_voice": tts.voice_used,
                "narration_seconds": tts.duration_seconds,
                "display_seconds": display_sec,
            }
        )

    tts_seconds = time.perf_counter() - tts_start

    if not scene_videos:
        raise RuntimeError(f"No scenes rendered for language {lang}")

    render_start = time.perf_counter()
    with open(highlight_root / "narration_manifest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "language": lang,
                "compression": compression,
                "max_words_per_scene_en": max_narration_words(compression),
                "scenes": narration_manifest,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    output_mp4 = highlight_root / f"highlight_{lang}.mp4"
    concat_scenes(scene_videos, output_mp4)
    if lang == "en":
        # The English download is the narration from this video, so it has the same length and timing.
        ensure_narration_track(highlight_root)
        print(f"[highlight] English narration reused for {reused_narration} of {len(scene_videos)} scenes", file=sys.stderr)
    render_seconds = time.perf_counter() - render_start

    total = translation_seconds + tts_seconds + render_seconds
    success = {
        "language": lang,
        "status": "success",
        "translator": translator_backend or ("source" if lang == "en" else HIGHLIGHT_TRANSLATOR),
        "translation_seconds": round(translation_seconds, 3),
        "tts_seconds": round(tts_seconds, 3),
        "render_seconds": round(render_seconds, 3),
        "total_seconds": round(total, 3),
        "narration_voices": summarize_narration_voices(narration_manifest),
    }
    (highlight_root / "language_status.json").write_text(
        json.dumps(success, indent=2),
        encoding="utf-8",
    )
    merge_language_status(job_output_dir, lang, success)
    return output_mp4


def _save_translations_json(
    lang: str,
    job_output_dir: Path,
    results: list[SceneTranslationResult],
) -> None:
    trans_path = job_output_dir / "highlights" / lang / "translations.json"
    trans_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trans_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "language": lang,
                "translator": HIGHLIGHT_TRANSLATOR,
                "scenes": [asdict(r) for r in results],
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def run_highlight_pipeline(
    video_path: Path,
    segments: list[dict[str, Any]],
    job_output_dir: Path,
    *,
    model: Optional[str] = None,
    max_scenes: int = 0,
    compression: str = "medium",
    languages: Optional[list[str] | str] = None,
    rebuild_shared: bool = False,
) -> Union[Path, dict[str, Path]]:
    require_ffmpeg_subtitles_filter()

    job_output_dir = Path(job_output_dir)
    lang_codes = parse_language_list(languages)
    staged_video = resolve_job_video(Path(video_path), job_output_dir)

    scenes, visuals, english_texts = _load_or_build_shared(
        segments,
        staged_video,
        job_output_dir,
        model=model,
        max_scenes=max_scenes,
        compression=compression,
        rebuild=rebuild_shared,
    )

    shared_dir = job_output_dir / "highlights" / "_shared"
    term_log = TranslationTermLog(job_output_dir / "highlights" / "translation_log.json")
    trans_cache = JobTranslationCache(shared_dir / "translation_cache.json")

    status = read_highlight_status(job_output_dir)
    status["requested_languages"] = lang_codes
    write_highlight_status(job_output_dir, status)

    google_breaker = GoogleJobCircuitBreaker()
    if os.environ.get("HIGHLIGHT_DISABLE_GOOGLE", "").strip().lower() in ("1", "true", "yes"):
        google_breaker.trip("HIGHLIGHT_DISABLE_GOOGLE")
    breaker_token = set_google_circuit_breaker(google_breaker)

    # Phase 1: translate languages in parallel (shared cache + Google circuit breaker)
    translations: dict[str, LanguageTranslationOutcome] = {}
    translate_workers = min(len(lang_codes), 4)
    try:
        with ThreadPoolExecutor(max_workers=max(translate_workers, 1)) as pool:
            futures = {
                pool.submit(
                    _translate_highlight_language,
                    lang,
                    english_texts,
                    trans_cache=trans_cache,
                    term_log=term_log,
                    google_breaker=google_breaker,
                ): lang
                for lang in lang_codes
            }
            for fut in as_completed(futures):
                lang, outcome = fut.result()
                translations[lang] = outcome
                if lang != "en" and outcome.scene_results:
                    _save_translations_json(lang, job_output_dir, outcome.scene_results)
                if not outcome.ok:
                    msg = outcome.message or f"Translation failed for {lang}"
                    if outcome.failure_kind == "rate_limit":
                        print(f"[highlight] {lang} translation rate limited: {msg}", file=sys.stderr)
                        msg = "The translation service is busy. Please try again later."
                    _write_lang_failure(
                        job_output_dir,
                        lang,
                        msg,
                        failure_kind=outcome.failure_kind,
                        translation_seconds=outcome.translation_seconds,
                        translator_backend=outcome.translator_backend,
                    )
                    print(f"[WARN] Translation failed for {lang}: {msg}", file=sys.stderr)
    finally:
        reset_google_circuit_breaker(breaker_token)

    en_spoken = english_texts
    if translations.get("en") and translations["en"].ok and translations["en"].spoken_texts:
        en_spoken = [str(s) for s in translations["en"].spoken_texts if s]
    if any(o.ok and o.spoken_texts for o in translations.values()):
        print("[INFO] Building shared highlight scene visuals (one video track for all languages)", file=sys.stderr)
        ensure_shared_scene_visuals(
            job_output_dir,
            visuals,
            en_spoken,
            rebuild=rebuild_shared,
        )

    # Phase 2: TTS + mux per language (shared visuals; audio + captions only)
    render_queue: dict[str, list[str]] = {}
    for lang, outcome in translations.items():
        if outcome.ok and all(outcome.spoken_texts):
            render_queue[lang] = [str(s) for s in outcome.spoken_texts]

    results: dict[str, Path] = {}
    errors: dict[str, str] = {}

    workers = min(HIGHLIGHT_RENDER_MAX_PARALLEL_LANGS, max(len(render_queue), 1))
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futures = {
            pool.submit(
                _render_language_media,
                lang,
                scenes,
                visuals,
                spoken,
                english_texts,
                job_output_dir,
                compression=compression,
                translation_seconds=translations[lang].translation_seconds,
                translator_backend=translations[lang].translator_backend,
            ): lang
            for lang, spoken in render_queue.items()
        }
        for fut in as_completed(futures):
            lang = futures[fut]
            try:
                results[lang] = fut.result()
                print(f"[INFO] Highlight {lang}: {results[lang]}", file=sys.stderr)
            except Exception as exc:
                errors[lang] = str(exc)
                _write_lang_failure(
                    job_output_dir,
                    lang,
                    str(exc),
                    failure_kind="error",
                    translation_seconds=translations[lang].translation_seconds,
                    translator_backend=translations[lang].translator_backend,
                )
                print(f"[WARN] Highlight render {lang} failed: {safe_exc(exc)}", file=sys.stderr)

    if "en" in lang_codes and "en" not in results and len(lang_codes) == 1:
        raise RuntimeError(errors.get("en", translations.get("en", LanguageTranslationOutcome("en", [])).message or "English highlight failed"))

    if len(lang_codes) == 1 and lang_codes[0] == "en":
        if "en" not in results:
            raise RuntimeError(errors.get("en", "English highlight failed"))
        return results["en"]

    return results


def retry_highlight_languages(
    job_output_dir: Path,
    languages: Optional[list[str]] = None,
    *,
    model: Optional[str] = None,
    compression: Optional[str] = None,
) -> dict[str, Path]:
    """Re-translate and re-render failed languages; reuses shared visuals + cache."""
    job_output_dir = Path(job_output_dir)
    segments_path = job_output_dir / "segments.json"
    video_path = job_output_dir / "source.mp4"
    if not segments_path.is_file() or not video_path.is_file():
        raise FileNotFoundError("Job missing segments.json or source.mp4 for retry")

    if languages:
        lang_codes = parse_language_list(languages)
    else:
        from src.highlight_jobs import failed_highlight_languages

        lang_codes = failed_highlight_languages(job_output_dir)
        if not lang_codes:
            return {}

    en_path = job_output_dir / "highlights" / "_shared" / "english_narrations.json"
    comp = compression or "medium"
    if en_path.is_file():
        payload = json.loads(en_path.read_text(encoding="utf-8"))
        comp = str(payload.get("compression", comp))

    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    out = run_highlight_pipeline(
        video_path,
        segments,
        job_output_dir,
        model=model,
        max_scenes=0,
        compression=comp,
        languages=lang_codes,
        rebuild_shared=False,
    )
    return out if isinstance(out, dict) else {"en": out}
