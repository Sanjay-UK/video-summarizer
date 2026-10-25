"""Hybrid local/RunPod execution for heavy vs light pipeline steps."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from config import choose_whisper_model
from src.gpu_selection import load_env_file
from src.runpod_ssh import (
    copy_from_remote,
    copy_to_remote,
    ensure_remote_dir,
    get_runpod_ssh_target,
    remote_app_root,
    remote_job_dir,
    ssh_run,
    sync_project_bundle,
)
from src.summary_translate import translate_summary_for_languages
from src.summarizer import summarize_transcript
from src.transcriber import transcribe_audio

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def _delete_s3_source(store: Any, job_output_dir: Path, key: str, log: Callable[[str], None]) -> None:
    """Delete the uploaded source after a completed job. A failed delete is reported and the key is kept."""
    from src import s3_video_store as s3

    record = s3.read_record(job_output_dir)
    try:
        store.delete(key)
        record.update({"status": "deleted", "deleted_at": s3.utc_now()})
        log("Source video deleted from S3 after the worker finished.")
    except Exception as exc:  # noqa: BLE001 — report, keep the key for manual cleanup
        record.update({"status": "delete_failed", "delete_error": type(exc).__name__})
        log(f"WARNING: could not delete S3 source ({type(exc).__name__}); key kept in {s3.S3_RECORD_NAME}.")
    s3.write_record(job_output_dir, record)


def _mark_s3_retained(job_output_dir: Path, key: str, log: Callable[[str], None]) -> None:
    from src import s3_video_store as s3

    record = s3.read_record(job_output_dir)
    record.update({"status": "retained_after_failure", "retained_at": s3.utc_now()})
    s3.write_record(job_output_dir, record)
    log(f"S3 source kept for retry (key recorded in {s3.S3_RECORD_NAME}).")


def _store_highlights_in_s3(
    job_output_dir: Path, paths: dict[str, Path], store: Any, log: Callable[[str], None]
) -> dict[str, Path]:
    """Upload each finished highlight and record its key. A language whose upload fails is reported as failed.

    Highlights are never deleted here. Their keys stay in s3_highlights.json so the job remains usable in History.
    """
    from src import s3_video_store as s3
    from src.highlight_pipeline import _write_lang_failure

    kept: dict[str, Path] = {}
    for lang, mp4 in paths.items():
        record = s3.upload_highlight(job_output_dir, lang, mp4, store)
        if record.get("status") == "uploaded":
            log(f"Highlight {lang} stored in S3 (key recorded in {s3.HIGHLIGHT_RECORD_NAME}).")
            kept[lang] = mp4
        else:
            message = f"Highlight upload to S3 failed ({record.get('error_code', 'error')}); not served from local disk."
            log(message)
            _write_lang_failure(job_output_dir, lang, message, failure_kind="s3_upload")
    return kept


class PipelineExecutor(ABC):
    mode: str

    def run_highlight_phase(
        self,
        *,
        job_id: str,
        video_path: Path,
        segments: list[dict[str, Any]],
        job_output_dir: Path,
        summarization_model: str,
        compression: str,
        languages: list[str],
        log: Callable[[str], None],
    ) -> dict[str, Path] | Path:
        from src.highlight_pipeline import run_highlight_pipeline

        log("Highlight video: local machine (FFmpeg, TTS, assembly)")
        out = run_highlight_pipeline(
            video_path,
            segments,
            job_output_dir,
            model=summarization_model,
            compression=compression,
            languages=languages,
        )
        return out if isinstance(out, dict) else {"en": out}

    @abstractmethod
    def run_heavy_phase(
        self,
        *,
        job_id: str,
        audio_path: Path,
        job_output_dir: Path,
        whisper_model: str,
        summarization_model: str,
        compression: str,
        highlight_languages: str,
        log: Callable[[str], None],
    ) -> None:
        """Transcribe, summarize, and summary-translate (same artifacts as local)."""


class LocalPipelineExecutor(PipelineExecutor):
    mode = "local"

    def run_heavy_phase(
        self,
        *,
        job_id: str,
        audio_path: Path,
        job_output_dir: Path,
        whisper_model: str,
        summarization_model: str,
        compression: str,
        highlight_languages: str,
        log: Callable[[str], None],
    ) -> None:
        _ = job_id
        log("Heavy steps: local machine (Whisper CPU, Ollama/NLLB local)")
        transcribe_audio(
            audio_path,
            model=whisper_model,
            output_dir=job_output_dir,
            device="cpu",
        )
        summarize_transcript(
            transcript_path=job_output_dir / "transcript.txt",
            segments_path=job_output_dir / "segments.json",
            output_dir=job_output_dir,
            model=summarization_model,
            compression=compression,
        )
        summary_text = (job_output_dir / "summary.txt").read_text(encoding="utf-8")
        from src.locales import parse_language_list

        langs = parse_language_list(highlight_languages)
        translate_summary_for_languages(job_output_dir, summary_text, langs)


class ServerlessRunpodPipelineExecutor(PipelineExecutor):
    mode = "hybrid_runpod"

    def run_heavy_phase(
        self,
        *,
        job_id: str,
        audio_path: Path,
        job_output_dir: Path,
        whisper_model: str,
        summarization_model: str,
        compression: str,
        highlight_languages: str,
        log: Callable[[str], None],
    ) -> None:
        from datetime import datetime, timezone

        from src.runpod_serverless import (
            load_endpoint_id_from_selection,
            run_heavy_phase_via_serverless,
            save_job_metrics_to_selection,
        )

        endpoint_id = load_endpoint_id_from_selection()
        result = run_heavy_phase_via_serverless(
            endpoint_id=endpoint_id,
            job_id=job_id,
            audio_path=audio_path,
            job_output_dir=job_output_dir,
            whisper_model=whisper_model,
            summarization_model=summarization_model,
            compression=compression,
            highlight_languages=highlight_languages,
            log=log,
        )
        metrics = {
            "delayTime": result.get("delayTime"),
            "executionTime": result.get("executionTime"),
            "runpod_job_id": result.get("id"),
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        save_job_metrics_to_selection(metrics)

    def run_highlight_phase(
        self,
        *,
        job_id: str,
        video_path: Path,
        segments: list[dict[str, Any]],
        job_output_dir: Path,
        summarization_model: str,
        compression: str,
        languages: list[str],
        log: Callable[[str], None],
    ) -> dict[str, Path] | Path:
        from src.serverless_model_catalog import serverless_highlight_on_worker

        if not serverless_highlight_on_worker():
            return super().run_highlight_phase(
                job_id=job_id,
                video_path=video_path,
                segments=segments,
                job_output_dir=job_output_dir,
                summarization_model=summarization_model,
                compression=compression,
                languages=languages,
                log=log,
            )
        from src.highlight_pipeline import _write_lang_failure
        from src.runpod_serverless import (
            load_endpoint_id_from_selection,
            run_highlight_phase_via_serverless,
        )

        endpoint_id = load_endpoint_id_from_selection()
        segments_json = json.dumps(segments)
        lang_csv = ",".join(languages)

        s3_key: str | None = None
        s3_store = None
        video_url: str | None = None
        from src import s3_video_store as s3

        if s3.s3_enabled():
            # Source goes to S3 and the worker gets a presigned URL. The presigned URL is never logged or saved.
            s3_store = s3.VideoStore(s3.settings_from_env())
            s3_key = s3.make_object_key(video_path.name)
            try:
                s3_store.upload_source(video_path, s3_key)
                s3.write_record(job_output_dir, {
                    "bucket": s3_store.bucket, "key": s3_key, "status": "uploaded",
                    "uploaded_at": s3.utc_now(), "deleted_at": None,
                })
                video_url = s3_store.presign_get(s3_key)
                log(f"Source uploaded to S3 (key recorded in {s3.S3_RECORD_NAME}).")
            except Exception as exc:
                message = f"Source upload to S3 failed ({type(exc).__name__}); remote highlight was not started."
                log(message)
                for lang in languages:
                    _write_lang_failure(job_output_dir, lang, message, failure_kind="s3_upload")
                return {}

        try:
            run_highlight_phase_via_serverless(
                endpoint_id=endpoint_id,
                job_id=job_id,
                video_path=video_path,
                segments_json=segments_json,
                job_output_dir=job_output_dir,
                summarization_model=summarization_model,
                compression=compression,
                languages=lang_csv,
                log=log,
                video_url=video_url,
            )
        except Exception as exc:
            # No silent local fallback: a locally rendered video must never be reported as a worker result.
            # Each requested language is recorded as failed, so the UI and status show the real outcome.
            message = f"RunPod worker highlight failed: {exc}"
            log(message)
            if s3_key and s3_store is not None:
                # Keep the source so a retry can reuse it; the key is recorded for manual cleanup.
                _mark_s3_retained(job_output_dir, s3_key, log)
            for lang in languages:
                _write_lang_failure(
                    job_output_dir,
                    lang,
                    message,
                    failure_kind="runpod_worker",
                )
            return {}
        if s3_key and s3_store is not None:
            # The RunPod job completed, so the worker has finished downloading the source.
            _delete_s3_source(s3_store, job_output_dir, s3_key, log)
        from src.highlight_jobs import highlight_file_for_lang

        paths: dict[str, Path] = {}
        for lang in languages:
            mp4 = highlight_file_for_lang(job_output_dir, lang)
            if mp4.is_file():
                paths[lang] = mp4
        missing = [lang for lang in languages if lang not in paths]
        for lang in missing:
            message = "RunPod worker returned no highlight file for this language."
            log(message)
            _write_lang_failure(job_output_dir, lang, message, failure_kind="runpod_worker")
        if s3_store is not None and paths:
            paths = _store_highlights_in_s3(job_output_dir, paths, s3_store, log)
        return paths


class HybridRunpodPipelineExecutor(PipelineExecutor):
    mode = "hybrid_runpod"

    def run_heavy_phase(
        self,
        *,
        job_id: str,
        audio_path: Path,
        job_output_dir: Path,
        whisper_model: str,
        summarization_model: str,
        compression: str,
        highlight_languages: str,
        log: Callable[[str], None],
    ) -> None:
        log("Heavy steps: RunPod GPU (Whisper CUDA, Ollama summarize, Google/NLLB summary translation)")
        log("Light steps stay local (TTS, highlights, FFmpeg, UI)")
        transport = os.environ.get("RUNPOD_REMOTE_TRANSPORT", "auto").strip().lower()
        use_jupyter = transport in ("http", "jupyter")
        if transport == "auto":
            from src.runpod_ssh import get_runpod_ssh_target, probe_runpod_ssh_reachable

            try:
                probe_runpod_ssh_reachable()
                target = get_runpod_ssh_target()
                if target.host == "ssh.runpod.io":
                    log(
                        "RunPod proxy SSH cannot run remote commands reliably; "
                        "using Jupyter HTTP for file transfer and GPU work."
                    )
                    use_jupyter = True
            except RuntimeError:
                use_jupyter = True

        if use_jupyter:
            from src.runpod_jupyter import run_heavy_phase_via_jupyter

            run_heavy_phase_via_jupyter(
                job_id=job_id,
                audio_path=audio_path,
                job_output_dir=job_output_dir,
                whisper_model=whisper_model,
                summarization_model=summarization_model,
                compression=compression,
                highlight_languages=highlight_languages,
                log=log,
            )
            return

        try:
            self._run_heavy_via_ssh(
                job_id=job_id,
                audio_path=audio_path,
                job_output_dir=job_output_dir,
                whisper_model=whisper_model,
                summarization_model=summarization_model,
                compression=compression,
                highlight_languages=highlight_languages,
                log=log,
            )
        except RuntimeError as exc:
            if transport != "auto":
                raise
            log(f"RunPod SSH path failed ({str(exc)[:200]}…); falling back to Jupyter HTTP.")
            from src.runpod_jupyter import run_heavy_phase_via_jupyter

            run_heavy_phase_via_jupyter(
                job_id=job_id,
                audio_path=audio_path,
                job_output_dir=job_output_dir,
                whisper_model=whisper_model,
                summarization_model=summarization_model,
                compression=compression,
                highlight_languages=highlight_languages,
                log=log,
            )

    def _run_heavy_via_ssh(
        self,
        *,
        job_id: str,
        audio_path: Path,
        job_output_dir: Path,
        whisper_model: str,
        summarization_model: str,
        compression: str,
        highlight_languages: str,
        log: Callable[[str], None],
    ) -> None:
        target = get_runpod_ssh_target()
        if target.host == "ssh.runpod.io":
            log("RunPod file transfer: SSH stdin stream (proxy SSH has no SCP subsystem)")
        sync_project_bundle(target, _PROJECT_DIR)
        remote_dir = remote_job_dir(job_id)
        ensure_remote_dir(target, remote_dir)

        remote_audio = f"{remote_dir}/{audio_path.name}"
        copy_to_remote(target, audio_path, remote_audio)

        manifest = {
            "job_id": job_id,
            "audio_file": audio_path.name,
            "whisper_model": whisper_model,
            "summarization_model": summarization_model,
            "compression": compression,
            "highlight_languages": highlight_languages,
        }
        manifest_local = job_output_dir / "heavy_manifest.json"
        manifest_local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        copy_to_remote(target, manifest_local, f"{remote_dir}/heavy_manifest.json")

        app_root = remote_app_root()
        remote_cmd = (
            f"cd {app_root} && python3 scripts/runpod_heavy_worker.py --workdir {remote_dir}"
        )
        log(f"RunPod SSH: starting heavy worker in {remote_dir}")
        ssh_run(target, remote_cmd, timeout=7200)

        for name in (
            "transcript.txt",
            "segments.json",
            "summary.txt",
            "summary_translations.json",
            "heavy_done.json",
        ):
            copy_from_remote(target, f"{remote_dir}/{name}", job_output_dir / name)

        if not (job_output_dir / "transcript.txt").is_file():
            raise RuntimeError("RunPod heavy worker did not produce transcript.txt")


def build_executor(runtime_ctx: dict[str, Any]) -> PipelineExecutor:
    if runtime_ctx.get("mode") == "hybrid_runpod":
        from src.compute_deploy import COMPUTE_MODE_SERVERLESS, load_compute_mode

        if load_compute_mode() == COMPUTE_MODE_SERVERLESS:
            return ServerlessRunpodPipelineExecutor()
        return HybridRunpodPipelineExecutor()
    return LocalPipelineExecutor()


def apply_runtime_side_effects(runtime_ctx: dict[str, Any]) -> dict[str, str | None]:
    """Set env toggles for hybrid split; returns prior values to restore."""
    prior = {"NLLB_VIA_RUNPOD": os.environ.get("NLLB_VIA_RUNPOD")}
    if runtime_ctx.get("mode") == "hybrid_runpod":
        os.environ["NLLB_VIA_RUNPOD"] = "1"
    else:
        os.environ.pop("NLLB_VIA_RUNPOD", None)
    return prior


def restore_runtime_side_effects(prior: dict[str, str | None]) -> None:
    for key, value in prior.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
