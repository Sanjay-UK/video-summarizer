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


class PipelineExecutor(ABC):
    mode: str

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
