#!/usr/bin/env python3
"""Flask web app for video summarization."""

from src.log_safety import redact, safe_exc, safe_traceback
from src.user_messages import GENERIC_FAILURE, GENERIC_HIGHLIGHT_FAILURE, public_error, public_log_line
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Union
from flask import Flask, render_template, request, jsonify, redirect
from datetime import datetime
import uuid
import hashlib
import os
import shutil

sys.path.insert(0, str(Path(__file__).parent))

from src.downloader import handle_video_source, download_youtube_video, get_local_video
from src.audio_extractor import extract_audio
from src.subtitle_generator import generate_subtitles
from src.tts_generator import generate_audio_explanation
from config import OUTPUT_DIR, DOWNLOADS_DIR, SUMMARIZATION_MODEL, choose_whisper_model
from src.job_video import JOB_SOURCE_BASENAME, copy_source_video_to_job
from src.highlight_ffmpeg import log_ffmpeg_subtitles_status_on_startup
from src.highlight_jobs import (
    list_highlight_downloads,
    list_highlight_language_status,
    list_output_languages,
    read_highlight_status,
    write_highlight_status,
)
from src.highlight_pipeline import (
    _write_lang_failure,
    record_unrecorded_highlight_failures,
    retry_highlight_languages,
)
from src.highlight_assembler import HIGHLIGHT_NARRATION_FILE, ensure_narration_track
from src import s3_video_store as s3
from src.locales import LOCALES, parse_language_list
from src.youtube_errors import (
    USER_ACTIONABLE as USER_ACTIONABLE_CODES,
    YouTubeDownloadError,
    is_anti_bot_detail,
    sanitize_technical,
)
from src.summary_jobs import clean_summary_text_for_display, summary_translations_for_api
from src.summary_translate import summary_text_for_language, translate_summary_for_languages
from src.gpu_selection import build_gpu_selection_report
from src.compute_deploy import (
    delete_saved_network_volume,
    save_selection_and_deploy,
    stop_saved_runpod_pod,
)
from src.compute_status import get_compute_deploy_status
from src.pipeline_executor import (
    apply_runtime_side_effects,
    build_executor,
    restore_runtime_side_effects,
)
from src.pipeline_runtime import (
    get_pipeline_runtime_status,
    load_pipeline_runtime,
    resolve_runtime_for_job,
    save_pipeline_runtime,
)

app = Flask(__name__)


@app.context_processor
def inject_compute_deploy_status():
    """RunPod deploy banner data — only loaded for GPU selection page."""
    from flask import request

    if request.endpoint != "gpu_selection_page":
        return {"deploy_status": {"has_deploy": False, "show_deploy_banner": False}}
    try:
        return {"deploy_status": get_compute_deploy_status(refresh_live=True)}
    except Exception:
        return {"deploy_status": {"has_deploy": False, "show_deploy_banner": False}}
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

jobs = {}
UPLOAD_DIR = Path(tempfile.gettempdir()) / "videosummarizer_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

class JobProgress:
    """Track job progress."""
    def __init__(self, job_id, source, display_name=None, video_uid=None):
        self.job_id = job_id
        self.source = source
        self.display_name = display_name or source
        self.video_uid = video_uid or s3.make_video_uid()
        self.uploaded_files = {}  # files already in S3 for this job, so none is sent twice
        self.current_step = None
        self.progress = 0.0
        self.status = "processing"
        self.log_lines = []
        self.results = None
        self.error = None
        self.error_code = None
        self.indeterminate = False
        self.stage_started_at = datetime.now()
        self.progress_source = "local"
        self._lock = threading.RLock()
        self.created_at = datetime.now()
        self.completed_at = None
        self.job_output_dir = OUTPUT_DIR / job_id
        self.job_output_dir.mkdir(parents=True, exist_ok=True)

    def add_log(self, message):
        """Add a log line."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        # The console keeps the full detail for debugging. The progress screen gets only user-safe lines.
        print(f"[{self.job_id}] {message}")
        shown = public_log_line(message)
        if shown is not None:
            self.log_lines.append(f"[{timestamp}] {shown}")

    def set_step(self, step_name, progress, indeterminate=False):
        """Start a stage. indeterminate=True means no progress inside this stage can be measured."""
        with self._lock:
            self.current_step = step_name
            self.progress = min(float(progress), 0.99)  # 100% is only set by mark_completed()
            self.indeterminate = indeterminate
            self.stage_started_at = datetime.now()
        if indeterminate:
            self.add_log(f"Step: {step_name} (running, no measurable progress)")
        else:
            self.add_log(f"Step: {step_name} ({progress*100:.0f}%)")

    def mark_completed(self):
        """The only place that sets 100%: status and progress change together, after outputs are saved."""
        with self._lock:
            self.indeterminate = False
            self.progress = 1.0
            self.status = "completed"
            self.completed_at = datetime.now()

    def mark_failed(self, message, code=None):
        """Record a failure. Progress stays at its last confirmed value.

        The stored message is sanitized (URLs, tokens, and cookie values removed), so the progress API, the results
        API, and the metadata file all show the same safe text.
        """
        with self._lock:
            self.indeterminate = False
            self.status = "error"
            self.error = public_error(sanitize_technical(str(message), limit=400), default=GENERIC_FAILURE)
            self.error_code = code
            self.completed_at = datetime.now()

    def set_progress(self, fraction):
        """Measured progress inside the current stage. Never moves backward, and never reaches 1.0.

        Reaching 100% is reserved for a confirmed successful completion (job.progress = 1.0 after the outputs
        are saved), so a measured value here is capped just below it.
        """
        try:
            value = min(max(float(fraction), 0.0), 0.99)
        except (TypeError, ValueError):
            return
        with self._lock:
            self.indeterminate = False
            if value > self.progress:
                self.progress = value

    def to_dict(self):
        """Convert to API response format. Read under the lock, so status and progress are consistent."""
        with self._lock:
            return {
                "job_id": self.job_id,
                "current_step": self.current_step,
                "progress": self.progress,
                "status": self.status,
                "log_lines": self.log_lines[-20:],
                "error": self.error if self.status == "error" else None,
                "error_code": self.error_code,
                "indeterminate": self.indeterminate,
                "stage_elapsed_sec": int((datetime.now() - self.stage_started_at).total_seconds()),
                "progress_source": self.progress_source,
            }

def _job_reporter(job, transcribe_range):
    """Connects the pipeline's measured-progress calls to this job. Transcription progress maps into its range."""
    low, high = transcribe_range

    def reporter(kind, value, step, indeterminate):
        if kind == "stage":
            job.set_step(step, value, indeterminate=indeterminate)
        else:
            job.set_progress(low + (high - low) * value)

    return reporter


def _download_progress_hook(job):
    """yt-dlp progress hook: downloaded bytes over total bytes, mapped into the download range (5%-10%)."""

    def hook(status):
        try:
            if status.get("status") != "downloading":
                return
            done = status.get("downloaded_bytes")
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if done and total:
                job.set_progress(0.05 + 0.05 * min(done / total, 1.0))
        except Exception:  # noqa: BLE001 — progress must never fail the download
            return

    return hook


def process_video(
    job_id,
    source,
    model="llama3.2:3b",
    compression="medium",
    highlight_video: bool = False,
    highlight_languages: str = "en",
    pipeline_runtime: str | None = None,
):
    """Run complete pipeline in background.

    Args:
        compression: "high" (50%), "medium" (70%), or "low" (85%) compression
        highlight_video: when True, also produce highlights/<lang>/highlight_<lang>.mp4
        highlight_languages: comma-separated language codes (en, hi, kn, te, ta)
    """
    job = jobs[job_id]
    from src.job_progress import reset_reporter, set_reporter

    runtime_ctx = resolve_runtime_for_job(pipeline_runtime)
    env_prior = apply_runtime_side_effects(runtime_ctx)
    executor = build_executor(runtime_ctx)
    transcription: dict = {}
    reporter_token = set_reporter(_job_reporter(job, transcribe_range=(0.35, 0.60)))

    try:
        if runtime_ctx.get("error"):
            raise RuntimeError(runtime_ctx["error"])
        job.add_log(f"Runtime: {runtime_ctx.get('location_label', 'this machine')}")
        job.add_log(
            f"Split: heavy={runtime_ctx.get('heavy_location', 'local')}, "
            f"light={runtime_ctx.get('light_location', 'local')}"
        )
        job.add_log("Initializing pipeline...")

        job.set_step("downloading", 0.05)

        if "youtube.com" in source or "youtu.be" in source:
            job.add_log("Downloading YouTube video...")
            try:
                video_path = download_youtube_video(
                    source, DOWNLOADS_DIR, quality=360, max_retries=3,
                    progress_hook=_download_progress_hook(job),
                )
            except YouTubeDownloadError as e:
                # Classified failure. The user message is shown; technical detail goes to the server log only.
                job.add_log(f"YouTube download stopped ({e.code}).")
                print(f"[{job_id}] YouTube detail ({e.code}): {e.detail}")
                if is_anti_bot_detail(e.detail):
                    job.add_log("YouTube has anti-bot protection on this video.")
                    job.add_log("Please download the video manually and upload it instead.")
                raise
            except Exception as e:
                error_str = str(e).lower()
                if "page needs to be reloaded" in error_str or "sabr" in error_str:
                    job.add_log("YouTube has anti-bot protection on this video.")
                    job.add_log("Please download the video manually and upload it instead:")
                    job.add_log("   1. Go to https://www.y2mate.com/")
                    job.add_log("   2. Paste your YouTube URL")
                    job.add_log("   3. Download as MP4")
                    job.add_log("   4. Use 'Upload Video' tab on this page")
                    raise RuntimeError("YouTube download blocked by anti-bot protection. Please use the Upload tab instead.")
                else:
                    raise
        else:
            job.add_log("Loading local video file...")
            video_path = get_local_video(source)

        job.set_step("downloading", 0.10)
        job.add_log(f"Video ready: {video_path.name}")
        staged_video = copy_source_video_to_job(video_path, job.job_output_dir)
        job.add_log(f"Staged copy in job dir: {staged_video.name}")
        store = _s3_store()
        _upload_source_to_s3(job, store, staged_video)
        source_url = _source_url_for_worker(job, store)

        job.set_step("extracting_audio", 0.15)
        job.add_log("Extracting audio from video...")
        audio_path = extract_audio(video_path, job.job_output_dir)
        job.set_step("extracting_audio", 0.25)
        job.add_log(f"Audio extracted: {audio_path.name}")

        job.set_step("transcribing", 0.30)
        from src.highlight_ffmpeg import probe_duration_seconds

        audio_duration = probe_duration_seconds(audio_path)
        whisper_model = choose_whisper_model(audio_duration)
        from src.compute_deploy import COMPUTE_MODE_SERVERLESS, load_compute_mode

        if (
            runtime_ctx.get("mode") == "hybrid_runpod"
            and load_compute_mode() == COMPUTE_MODE_SERVERLESS
        ):
            from src.serverless_model_catalog import load_saved_serverless_models

            sm = load_saved_serverless_models()
            whisper_model = sm.get("whisper") or whisper_model
            model = sm.get("ollama") or model
        job.add_log(
            f"Heavy phase (~{audio_duration:.0f}s audio, Whisper {whisper_model}, "
            f"summarize {model})..."
        )
        if runtime_ctx.get("mode") == "hybrid_runpod":
            if load_compute_mode() != COMPUTE_MODE_SERVERLESS:
                import os

                from src.runpod_ssh import probe_runpod_ssh_reachable

                transport = os.environ.get("RUNPOD_REMOTE_TRANSPORT", "auto").strip().lower()
                job.add_log("Checking connection before processing...")
                if transport in ("http", "jupyter"):
                    from src.runpod_jupyter import probe_jupyter_reachable

                    probe_jupyter_reachable()
                elif transport == "ssh":
                    probe_runpod_ssh_reachable()
                else:
                    try:
                        probe_runpod_ssh_reachable()
                    except RuntimeError:
                        from src.runpod_jupyter import probe_jupyter_reachable

                        job.add_log(
                            "SSH unreachable; using RunPod Jupyter HTTP proxy (port 8888)."
                        )
                        probe_jupyter_reachable()
            else:
                job.add_log(
                    "RunPod serverless: submitting GPU job (workers scale from zero on RunPod)."
                )
        remote_heavy = runtime_ctx.get("mode") == "hybrid_runpod"
        job.progress_source = "runpod" if remote_heavy else "local"
        job.set_step("transcribing", 0.35, indeterminate=remote_heavy)
        if remote_heavy:
            job.add_log("Transcribing and summarizing. Progress is not reported during this step.")
        executor.run_heavy_phase(
            job_id=job_id,
            audio_path=audio_path,
            job_output_dir=job.job_output_dir,
            whisper_model=whisper_model,
            summarization_model=model,
            compression=compression,
            highlight_languages=highlight_languages,
            log=job.add_log,
        )
        job.set_step("summarizing", 0.85)
        segments_path = job.job_output_dir / "segments.json"
        segments_list: list = []
        if segments_path.is_file():
            with open(segments_path, encoding="utf-8") as f:
                loaded = json.load(f)
            segments_list = loaded if isinstance(loaded, list) else loaded.get("segments", [])
        transcript_path = job.job_output_dir / "transcript.txt"
        transcript_text = (
            transcript_path.read_text(encoding="utf-8") if transcript_path.is_file() else ""
        )
        transcription = {"text": transcript_text, "segments": segments_list}
        segments_count = len(segments_list)
        job.add_log(f"Heavy phase complete ({segments_count} segments, summary + translations)")

        output_lang_codes = parse_language_list(highlight_languages)
        # Only languages the user asked to render are "requested". Without a highlight request the list is empty,
        # so no highlight row (and no false "Failed") is shown.
        wants_highlight_now = highlight_video or any(c != "en" for c in output_lang_codes)
        hl_status = read_highlight_status(job.job_output_dir)
        hl_status["requested_languages"] = output_lang_codes if wants_highlight_now else []
        write_highlight_status(job.job_output_dir, hl_status)

        summary_text = open(job.job_output_dir / "summary.txt", encoding="utf-8").read()

        highlight_paths: dict[str, Path] = {}

        job.set_step("generating_audio", 0.90)
        job.add_log("Generating audio explanation with hierarchy...")
        try:

            # Generate audio with hierarchy-aware prosody
            # Returns: (audio_path, audio_transcript)
            audio_exp_path, audio_transcript = generate_audio_explanation(
                summary_text=summary_text,
                output_path=job.job_output_dir / "audio_explanation.mp3",
            )

            # Save the audio-ready transcript (includes hierarchy transitions)
            with open(job.job_output_dir / "audio_transcript.txt", "w") as f:
                f.write(audio_transcript)
            job.add_log(f"Audio generated with hierarchy prosody ({len(audio_transcript)} chars)")

            # Generate subtitles using the same text as audio
            # This ensures perfect audio-subtitle synchronization
            job.set_step("generating_subtitles", 0.95)
            job.add_log("Generating subtitles from audio transcript...")
            srt_path = generate_subtitles(
                summary_segments_path=job.job_output_dir / "summary_segments.json",
                output_path=job.job_output_dir / "summary.srt",
                audio_transcript=audio_transcript,
            )
            job.add_log("Subtitles generated from audio text")

            job.set_step("generating_subtitles", 0.98)
        except Exception as e:
            job.add_log(f"Audio/subtitle generation failed: {sanitize_technical(redact(str(e)))}")
            job.set_step("generating_subtitles", 0.98)

        wants_highlight = highlight_video or any(c != "en" for c in output_lang_codes)
        if wants_highlight:
            lang_codes = output_lang_codes
            job.add_log(f"Highlight languages: {', '.join(lang_codes)} (translator: Google)")
            try:
                with open(job.job_output_dir / "segments.json", encoding="utf-8") as f:
                    segments = json.load(f)
                # One blocking call (topics, visuals, narration, subtitles, FFmpeg). Its progress is not measured,
                # so the stage is indeterminate until the call returns.
                job.set_step("highlight_assembly", 0.92, indeterminate=True)
                job.add_log("Rendering highlight video (topics, visuals, narration, subtitles)...")
                highlight_out = executor.run_highlight_phase(
                    job_id=job_id,
                    video_path=staged_video,
                    segments=segments,
                    job_output_dir=job.job_output_dir,
                    summarization_model=model,
                    compression=compression,
                    languages=lang_codes,
                    log=job.add_log,
                    source_url=source_url,
                )
                if isinstance(highlight_out, dict):
                    highlight_paths = dict(highlight_out)
                    for lang, path in highlight_out.items():
                        job.add_log(f"Highlight {lang} ready: {path.name}")
                else:
                    highlight_paths = {"en": highlight_out}
                    job.add_log(f"Highlight en ready: {highlight_out.name}")
                # A requested language that was attempted but produced neither a file nor a status record is a failure.
                record_unrecorded_highlight_failures(
                    job.job_output_dir, lang_codes, "No highlight file was produced for this language.",
                    failure_kind="missing_output",
                )
                for row in list_highlight_language_status(job.job_output_dir):
                    if row["status"] == "failed":
                        job.add_log(f"Highlight {row['code']} failed: {row.get('message', '')}")
            except Exception as e:
                job.add_log(f"Highlight video failed: {sanitize_technical(redact(str(e)))}")
                # The step stopped before reporting on the languages. Record them as failed so the results page shows
                # the failure and offers a retry, instead of "Not generated" with no reason.
                record_unrecorded_highlight_failures(
                    job.job_output_dir, lang_codes, GENERIC_HIGHLIGHT_FAILURE, failure_kind="error",
                )
            job.set_step("highlight_complete", 0.98)
        else:
            job.set_step("generating_subtitles", 0.98)

        results = _results_payload(job.job_output_dir, len(transcription.get("text", "")))
        _prepare_downloadable_files(job.job_output_dir, results)
        job.add_log("Saving results to storage...")
        completed_at = datetime.now()
        s3.publish_job(
            store,
            job.job_output_dir,
            job_id=job_id,
            video_uid=job.video_uid,
            source_name=job.display_name,
            created_at=job.created_at.isoformat(),
            completed_at=completed_at.isoformat(),
            results=results,
            known_files=job.uploaded_files,
        )
        job.add_log("Pipeline completed successfully!")
        job.mark_completed()
        job.results = results
        return True

    except Exception as e:
        error_msg = f"Error: {sanitize_technical(str(e))}"
        job.add_log(error_msg)
        job.mark_failed(
            str(e),
            e.code if isinstance(e, (YouTubeDownloadError, s3.HighlightStoreError)) else None,
        )
        import traceback
        print(safe_traceback())

        try:
            s3.publish_failure(
                _s3_store(),
                job_id=job_id,
                video_uid=job.video_uid,
                source_name=job.display_name,
                created_at=job.created_at.isoformat(),
                completed_at=job.completed_at.isoformat(),
                error=job.error or GENERIC_FAILURE,
                error_code=job.error_code,
                known_files=job.uploaded_files,
            )
        except Exception as meta_exc:  # noqa: BLE001 — the job already shows its error on screen
            print(f"[WARN] failed-job record not saved to storage for {job_id}: {type(meta_exc).__name__}")
        return False
    finally:
        reset_reporter(reporter_token)
        try:
            restore_runtime_side_effects(env_prior)
        except Exception as restore_exc:
            print(f"[WARN] runtime env restore failed for {job_id}: {restore_exc}")

def _index_page_context() -> dict:
    return {
        "pipeline_runtime": load_pipeline_runtime(),
        "pipeline_runtime_status": get_pipeline_runtime_status(),
    }


@app.route("/")
def index():
    """Home page."""
    return render_template("index.html", **_index_page_context())


@app.route("/gpu-selection")
def gpu_selection_page():
    """Live GPU availability and ranking for cloud runs (demo)."""
    report = build_gpu_selection_report()
    return render_template("gpu_selection.html", report=report, pipeline_runtime=load_pipeline_runtime())


@app.route("/api/compute-deploy-status", methods=["GET"])
def compute_deploy_status_api():
    """JSON: saved deploy + live RunPod pod status."""
    return jsonify(get_compute_deploy_status(refresh_live=True)), 200


@app.route("/api/pipeline-runtime", methods=["GET", "POST"])
def pipeline_runtime_api():
    """GET/POST saved summarization runtime: local machine vs RunPod GPU pod."""
    if request.method == "GET":
        return jsonify(get_pipeline_runtime_status()), 200
    data = request.get_json(silent=True) or {}
    runtime = (data.get("pipeline_runtime") or request.form.get("pipeline_runtime") or "").strip()
    result = save_pipeline_runtime(runtime)
    status = 200 if result.get("ok") else 400
    if result.get("ok"):
        result.update(get_pipeline_runtime_status())
    return jsonify(result), status


@app.route("/api/compute-deploy-stop", methods=["POST"])
def compute_deploy_stop_api():
    """Stop RunPod compute: scale serverless workers to 0 or terminate legacy pod."""
    from src.serverless_status_cache import invalidate_all

    result = stop_saved_runpod_pod()
    if result.get("ok"):
        invalidate_all()  # only a confirmed change drops the cache; a failed call leaves it to expire by TTL
    status = 200 if result.get("ok") else 400
    if result.get("ok"):
        result["deploy_status"] = get_compute_deploy_status(refresh_live=False)
    return jsonify(result), status


@app.route("/api/compute-network-volume-delete", methods=["POST"])
def compute_network_volume_delete_api():
    """Detach and delete the RunPod network volume (destroys cached model weights)."""
    data = request.get_json(silent=True) or {}
    confirm = (data.get("confirm") or request.form.get("confirm") or "").strip()
    from src.serverless_status_cache import invalidate_all

    result = delete_saved_network_volume(confirm=confirm)
    if result.get("ok"):
        invalidate_all()
    status = 200 if result.get("ok") else 400
    if result.get("ok"):
        result["deploy_status"] = get_compute_deploy_status(refresh_live=False)
    return jsonify(result), status


@app.route("/api/gpu-selection/serverless-region", methods=["GET"])
def gpu_selection_serverless_region_api():
    """Live RunPod serverless catalog for one data center (refreshed on every request)."""
    from src.compute_deploy import load_saved_selection
    from src.gpu_selection import load_env_file
    from src.serverless_gpu_region import EMPTY_SERVERLESS_RECOMMENDATIONS, fetch_live_serverless_region

    load_env_file()
    dc = (request.args.get("data_center_id") or "").strip()
    if not dc:
        return jsonify({"ok": False, "error": "data_center_id query parameter is required"}), 400
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "RUNPOD_API_KEY missing in .env"}), 400

    saved = load_saved_selection()
    deploy_status = (saved or {}).get("deploy")
    env_pools = os.environ.get("RUNPOD_SERVERLESS_GPU_POOLS", "ADA_24,AMPERE_16").strip()
    payload = fetch_live_serverless_region(
        api_key,
        dc,
        saved=saved,
        deploy_status=deploy_status if isinstance(deploy_status, dict) else None,
        env_gpu_pools=env_pools,
    )
    payload["html"] = {
        "gpu_catalog": render_template(
            "partials/serverless_gpu_catalog.html",
            serverless_sections=payload.get("serverless_sections") or [],
            default_selection_id=payload.get("default_selection_id"),
        ),
        "recommendations": render_template(
            "partials/serverless_recommendations.html",
            recommendations=payload.get("recommendations") or EMPTY_SERVERLESS_RECOMMENDATIONS,
        ),
        "saved_display": render_template(
            "partials/serverless_saved_display.html",
            saved_display=payload.get("saved_display") or {"kind": "none"},
        ),
    }
    status = 200 if payload.get("ok") else 502
    return jsonify(payload), status


@app.route("/api/gpu-selection", methods=["GET", "POST"])
def gpu_selection_api():
    """GET: catalog report. POST: save one GPU/CPU selection and trigger deploy."""
    if request.method == "GET":
        return jsonify(build_gpu_selection_report()), 200
    body = request.get_json(silent=True) or {}
    selection_id = body.get("selection_id") or request.form.get("selection_id", "")
    client_offer = body.get("offer") if isinstance(body.get("offer"), dict) else None
    serverless_models = body.get("serverless_models")
    if not isinstance(serverless_models, dict):
        serverless_models = None
    serverless_dc = body.get("serverless_data_center_id")
    if not isinstance(serverless_dc, str):
        serverless_dc = None
    docker_hub_user = body.get("docker_hub_user")
    if docker_hub_user is not None and not isinstance(docker_hub_user, str):
        docker_hub_user = None
    from src.serverless_status_cache import invalidate_all

    result = save_selection_and_deploy(
        selection_id,
        client_offer=client_offer,
        serverless_models=serverless_models,
        serverless_data_center_id=serverless_dc,
        docker_hub_user=docker_hub_user,
    )
    if result.get("ok"):
        invalidate_all()  # the endpoint may have changed; the next status read must ask RunPod again
    if result.get("ok"):
        from src.compute_deploy import COMPUTE_MODE_SERVERLESS, load_compute_mode
        from src.pipeline_runtime import RUNTIME_RUNPOD, save_pipeline_runtime

        if load_compute_mode() == COMPUTE_MODE_SERVERLESS:
            save_pipeline_runtime(RUNTIME_RUNPOD)
    if result.get("ok"):
        result["deploy_status"] = get_compute_deploy_status(refresh_live=True)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status

@app.route("/job/<job_id>")
def job_page(job_id):
    """Serve the same page for a direct/shared/refreshed job URL.

    The frontend reads the job_id from the URL on load and fetches its results.
    """
    return render_template("index.html", **_index_page_context())

@app.route("/api/summarize", methods=["POST"])
def summarize():
    """Start summarization process from URL or uploaded file."""
    try:
        # Storage is required. Check it before the upload is saved, so nothing is started that cannot be kept.
        s3.ensure_ready(_s3_store())
        url = request.form.get("url", "").strip()
        # Homepage no longer exposes model picker; serverless GPU page saves worker models separately.
        model = SUMMARIZATION_MODEL
        compression = request.form.get("compression", "medium")
        highlight_video = request.form.get("highlight_video", "").lower() in ("1", "true", "on", "yes")
        lang_parts = request.form.getlist("highlight_languages")
        if not lang_parts:
            raw_langs = request.form.get("highlight_languages", "en")
            lang_parts = [p.strip() for p in raw_langs.split(",") if p.strip()]
        highlight_languages = ",".join(lang_parts) if lang_parts else "en"
        lang_codes_for_highlight = parse_language_list(highlight_languages)
        if not highlight_video and any(c != "en" for c in lang_codes_for_highlight):
            highlight_video = True
        uploaded_file = request.files.get("file")

        source = None
        display_name = None

        if uploaded_file and uploaded_file.filename:
            valid_extensions = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm"}
            file_ext = Path(uploaded_file.filename).suffix.lower()

            if file_ext not in valid_extensions:
                return jsonify({"error": f"Invalid file format: {file_ext}. Allowed: {valid_extensions}"}), 400

            upload_path = UPLOAD_DIR / uploaded_file.filename
            uploaded_file.save(str(upload_path))
            source = str(upload_path)
            display_name = uploaded_file.filename

        elif url:
            source = url
            display_name = url[:50] + "..." if len(url) > 50 else url
        else:
            return jsonify({"error": "Please provide a YouTube URL or upload a video file"}), 400

        job_id = str(uuid.uuid4())[:8]
        job = JobProgress(job_id, source, display_name=display_name)
        jobs[job_id] = job

        pipeline_runtime = request.form.get("pipeline_runtime", "").strip() or None
        if pipeline_runtime:
            save_pipeline_runtime(pipeline_runtime)

        thread = threading.Thread(
            target=process_video,
            args=(
                job_id,
                source,
                model,
                compression,
                highlight_video,
                highlight_languages,
                pipeline_runtime,
            ),
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id}), 200

    except s3.HighlightStoreError as exc:
        return jsonify({"error": redact(str(exc)), "code": exc.code}), s3.ERROR_STATUS.get(exc.code, 503)
    except Exception as e:
        return jsonify({"error": redact(str(e))}), 500

@app.route("/api/progress/<job_id>", methods=["GET"])
def progress(job_id):
    """Get progress updates."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    job = jobs[job_id]
    return jsonify(job.to_dict()), 200

def _s3_store():
    """The configured S3 bucket. The AWS client is created on first use, not here."""
    return s3.highlight_store()


def _upload_source_to_s3(job, store, staged_video: Path) -> None:
    """The source video is saved to S3 before any processing. Its key is recorded with the job."""
    job.add_log("Saving the source video to storage...")
    job.uploaded_files[JOB_SOURCE_BASENAME] = s3.upload_job_file(
        store, job.job_id, job.video_uid, staged_video, JOB_SOURCE_BASENAME
    )
    job.add_log("Source video saved.")


def _source_url_for_worker(job, store) -> str | None:
    """A one-hour link for the RunPod worker to download the source. Only when the worker supports URLs.

    The link is handed to the worker and never logged or saved.
    """
    if not s3.worker_url_support():
        return None
    entry = job.uploaded_files[JOB_SOURCE_BASENAME]
    return store.presign_get(entry["key"], expires=s3.HIGHLIGHT_URL_EXPIRES, content_type=entry["content_type"])


def _results_payload(job_dir: Path, transcript_length: int) -> dict:
    """The results shown on the results page, built from a job folder. Saved to S3 as part of the manifest."""
    summary_path = job_dir / "summary.txt"
    summary_text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    summary_text = clean_summary_text_for_display(summary_text, "en")
    highlight_downloads = list_highlight_downloads(job_dir)
    summary_i18n = summary_translations_for_api(job_dir)
    return {
        "status": "completed",
        "summary": summary_text,
        "transcript_length": transcript_length,
        "summary_length": len(summary_text),
        "has_audio": (job_dir / "audio_explanation.mp3").is_file(),
        "highlight_video": len(highlight_downloads) > 0,
        "highlight_languages": highlight_downloads,
        "highlight_language_status": list_highlight_language_status(job_dir),
        "requested_languages": read_highlight_status(job_dir).get("requested_languages") or [],
        **summary_i18n,
        "output_languages": list_output_languages(job_dir),
    }


def _prepare_downloadable_files(job_dir: Path, results: dict) -> None:
    """Write the per-language files that downloads serve, so each one is uploaded with the job.

    Downloads never generate files on demand: once the job is in S3, only what was uploaded can be served.
    """
    for code in results.get("summary_translations") or {}:
        if code == "en":
            continue
        target = job_dir / f"summary_{code}.txt"
        if not target.is_file():
            text = summary_text_for_language(job_dir, code)
            if text:
                target.write_text(text, encoding="utf-8")
    if "en" in (results.get("requested_languages") or []):
        ensure_narration_track(job_dir / "highlights" / "en")


def _load_manifest(job_id: str):
    """(manifest, None) for a stored job, or (None, response) when it cannot be shown."""
    if not _valid_job_id(job_id):
        return None, (jsonify({"error": "Job not found"}), 404)
    try:
        manifest = s3.load_job_manifest(_s3_store(), job_id)
    except Exception as exc:  # noqa: BLE001 — log the type only; messages can carry sensitive detail
        error = s3.as_store_error(exc)
        print(f"[S3] job {job_id} could not be read: {error.code} ({type(exc).__name__})")
        return None, (jsonify({"error": redact(str(error)), "code": error.code}), s3.ERROR_STATUS.get(error.code, 503))
    if manifest is None:
        return None, (jsonify({"error": "Job not found"}), 404)
    return manifest, None


def _history_entry(manifest: dict) -> dict:
    results = manifest.get("results") or {}
    files = manifest.get("files") or {}
    highlights = results.get("highlight_languages") or []
    summary_langs = ["en"] + [c for c in (results.get("summary_translations") or {}) if c != "en"]
    return {
        "job_id": manifest["job_id"],
        "video_uid": manifest["video_uid"],
        "source": manifest.get("source") or manifest["job_id"],
        "status": manifest.get("status", "completed"),
        "error": manifest.get("error"),
        "created_at": manifest.get("created_at", ""),
        "completed_at": manifest.get("completed_at", ""),
        "has_summary": "summary.txt" in files,
        "summary_languages": summary_langs,
        "has_audio": "audio_explanation.mp3" in files,
        "highlights": highlights,
        "has_highlight_video": len(highlights) > 0,
        "output_languages": results.get("output_languages") or [],
    }


def _warn_if_storage_unavailable() -> None:
    try:
        s3.ensure_ready(_s3_store())
        print("[startup] S3 storage OK")
    except s3.HighlightStoreError as exc:
        print(f"[startup] S3 storage is not ready ({exc.code}). New jobs cannot start until this is fixed.")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] S3 storage check skipped: {type(exc).__name__}")


@app.route("/api/results/<job_id>", methods=["GET"])
def results(job_id):
    """Results for a job. A job still running here is answered from memory; any other job is read from S3."""
    job = jobs.get(job_id)

    if job:
        if job.status == "completed":
            return jsonify(job.results), 200
        elif job.status == "error":
            status = 422 if job.error_code in USER_ACTIONABLE_CODES else 500
            return jsonify({"error": job.error, "code": job.error_code}), status
        else:
            return jsonify({"status": "processing"}), 202

    manifest, error = _load_manifest(job_id)
    if error is not None:
        return error
    if manifest.get("status") == "error":
        code = manifest.get("error_code")
        return jsonify({"error": manifest.get("error") or GENERIC_FAILURE, "code": code}), (
            422 if code in USER_ACTIONABLE_CODES else 500
        )
    payload = manifest.get("results")
    if not isinstance(payload, dict):
        return jsonify({"error": "Failed to load job data"}), 500
    return jsonify(payload), 200


@app.route("/api/history", methods=["GET"])
def history():
    """Every job stored in S3, newest first. A job still running on this machine is listed as 'processing'."""
    try:
        manifests = s3.list_job_manifests(_s3_store())
    except Exception as exc:  # noqa: BLE001
        error = s3.as_store_error(exc)
        print(f"[S3] history could not be loaded: {error.code} ({type(exc).__name__})")
        return jsonify({"error": redact(str(error)), "code": error.code, "jobs": [], "total": 0}), (
            s3.ERROR_STATUS.get(error.code, 503)
        ), NO_STORE
    entries = [_history_entry(m) for m in manifests]
    stored_ids = {entry["job_id"] for entry in entries}
    running = [
        {
            "job_id": j.job_id,
            "video_uid": j.video_uid,
            "source": j.display_name,
            "status": "processing",
            "error": None,
            "created_at": j.created_at.isoformat(),
            "completed_at": "",
            "has_summary": False,
            "summary_languages": ["en"],
            "has_audio": False,
            "highlights": [],
            "has_highlight_video": False,
            "output_languages": [],
        }
        for j in list(jobs.values())
        if j.status == "processing" and j.job_id not in stored_ids
    ]
    listed = running + entries
    return jsonify({"jobs": listed, "total": len(listed)}), 200, NO_STORE


@app.route("/api/job/<job_id>", methods=["DELETE"])
def delete_job_route(job_id):
    """Delete one job and every file of it from S3. Irreversible. A job that is still running cannot be deleted."""
    if not _valid_job_id(job_id):
        return jsonify({"error": "Job not found"}), 404
    job = jobs.get(job_id)
    if job and job.status == "processing":
        return jsonify({"error": "This job is still running. Delete it when it has finished."}), 409
    try:
        removed = s3.delete_job(_s3_store(), job_id)
    except Exception as exc:  # noqa: BLE001
        error = s3.as_store_error(exc)
        print(f"[S3] job {job_id} not deleted: {error.code} ({type(exc).__name__})")
        return jsonify({"error": redact(str(error)), "code": error.code}), s3.ERROR_STATUS.get(error.code, 503)
    if removed == 0:
        return jsonify({"error": "Job not found"}), 404
    jobs.pop(job_id, None)
    return jsonify({"ok": True, "deleted_files": removed}), 200


@app.route("/download/summary/<job_id>")
@app.route("/download/summary/<job_id>/<lang_code>")
def download_summary(job_id, lang_code: str = "en"):
    """Summary text (English or translated). Redirects to a fresh one-hour S3 link."""
    code = (lang_code or "en").strip().lower()
    if code not in LOCALES:
        return "Invalid language", 400
    rel = "summary.txt" if code == "en" else f"summary_{code}.txt"
    return _file_redirect(job_id, rel, attachment=True)


@app.route("/download/subtitles/<job_id>")
def download_subtitles(job_id):
    """SRT subtitle file. Redirects to a fresh one-hour S3 link."""
    return _file_redirect(job_id, "summary.srt", attachment=True)


@app.route("/download/audio/<job_id>")
def download_audio(job_id):
    """English audio download.

    When English highlights were requested, the download is the narration from the highlight video, so the audio and
    the video have the same length and timing. If that audio is missing, the response says so; the summary audio is
    never returned in its place. `?source=summary` returns the full summary narration instead.
    """
    manifest, error = _load_manifest(job_id)
    if error is not None:
        return error
    if request.args.get("source") != "summary":
        requested = (manifest.get("results") or {}).get("requested_languages") or []
        if "en" in requested:
            rel = f"highlights/en/{HIGHLIGHT_NARRATION_FILE}"
            if rel in (manifest.get("files") or {}):
                return _file_redirect(job_id, rel, attachment=True)
            return jsonify({"error": "The highlight audio is not available for this job."}), 404
    return _file_redirect(job_id, "audio_explanation.mp3", attachment=True)


def _file_digests(folder: Path) -> dict[str, str]:
    """SHA-256 of every file under folder, keyed by relative path. Used to upload only what a retry changed."""
    digests: dict[str, str] = {}
    for path in folder.rglob("*"):
        if path.is_file():
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests[path.relative_to(folder).as_posix()] = digest.hexdigest()
    return digests


@app.route("/api/highlight/retry/<job_id>", methods=["POST"])
def retry_highlight(job_id):
    """Re-run failed highlight languages on this machine. The job's files come from S3, and the result is saved back."""
    try:
        if not _valid_job_id(job_id):
            return jsonify({"error": "Job not found"}), 404
        from src.pipeline_runtime import RUNTIME_RUNPOD, load_pipeline_runtime

        if load_pipeline_runtime() == RUNTIME_RUNPOD:
            # Retries render on this machine. A serverless retry needs its own implementation and tests.
            return jsonify({
                "error": "Retrying a highlight is not available in the current processing mode yet. "
                         "Switch to Local mode on the GPU selection page to retry on this machine.",
                "code": "retry_unavailable_runpod",
            }), 409

        store = _s3_store()
        manifest = s3.load_job_manifest(store, job_id)
        if manifest is None:
            return jsonify({"error": "Job not found"}), 404
        if manifest.get("status") != "completed":
            return jsonify({"error": "Only a completed job can be retried."}), 409

        body = request.get_json(silent=True) or {}
        langs = body.get("languages")
        lang_list = None
        if isinstance(langs, list) and langs:
            lang_list = [str(x).strip().lower() for x in langs if str(x).strip()]

        model = body.get("model")
        compression = body.get("compression")

        work_dir = Path(tempfile.mkdtemp(prefix=f"retry_{job_id}_"))
        try:
            s3.download_job_files(store, manifest, work_dir)
            before = _file_digests(work_dir)
            retry_highlight_languages(
                work_dir,
                lang_list,
                model=model,
                compression=compression,
            )
            results = _results_payload(work_dir, (manifest.get("results") or {}).get("transcript_length", 0))
            _prepare_downloadable_files(work_dir, results)
            after = _file_digests(work_dir)
            # Files the retry did not change are already in S3 with the same content, so they are not sent again.
            unchanged = {
                rel: manifest["files"][rel]
                for rel, digest in before.items()
                if after.get(rel) == digest and rel in manifest["files"]
            }
            s3.publish_job(
                store,
                work_dir,
                job_id=job_id,
                video_uid=manifest["video_uid"],
                source_name=manifest.get("source") or job_id,
                created_at=manifest.get("created_at", ""),
                completed_at=manifest.get("completed_at", ""),
                results=results,
                known_files=unchanged,
            )
            status = list_highlight_language_status(work_dir)
            downloads = list_highlight_downloads(work_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if job_id in jobs:
            jobs[job_id].results = results
        return jsonify(
            {
                "ok": True,
                "highlight_language_status": status,
                "highlight_languages": downloads,
                "highlight_video": len(downloads) > 0,
            }
        ), 200
    except s3.HighlightStoreError as exc:
        return jsonify({"error": redact(str(exc)), "code": exc.code}), s3.ERROR_STATUS.get(exc.code, 503)
    except Exception as e:
        return jsonify({"error": redact(str(e))}), 500


NO_STORE = {"Cache-Control": "no-store"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")  # job IDs are the first 8 hex characters of a UUID


def _valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id or ""))


def _highlight_rel(code: str) -> str:
    """Path of a highlight inside the job folder, the same path the pipeline writes it to."""
    return f"highlights/{code}/highlight_{code}.mp4"


def _file_redirect(job_id: str, rel: str, *, attachment: bool):
    """Send the browser to a fresh one-hour S3 link for one file of a job. Nothing is served from this machine."""
    manifest, error = _load_manifest(job_id)
    if error is not None:
        return error
    try:
        payload = s3.file_link(_s3_store(), manifest, rel, attachment=attachment)
    except Exception as exc:  # noqa: BLE001
        error = s3.as_store_error(exc)
        print(f"[S3] {job_id}/{rel} not issued: {error.code} ({type(exc).__name__})")
        return redact(str(error)), s3.ERROR_STATUS.get(error.code, 503), NO_STORE
    response = redirect(payload["url"], code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/highlight-url/<job_id>/<lang_code>", methods=["GET"])
def api_highlight_url(job_id, lang_code):
    """Fresh one-hour S3 link for a highlight. Built on every call, never saved or cached.

    404 with code 'not_in_s3' means this job has no highlight in that language.
    """
    code = (lang_code or "en").strip().lower()
    if code not in LOCALES:
        return jsonify({"error": "Invalid language"}), 400, NO_STORE
    manifest, error = _load_manifest(job_id)
    if error is not None:
        return error[0], error[1], NO_STORE
    attachment = request.args.get("attachment", "") in ("1", "true")
    try:
        payload = s3.file_link(_s3_store(), manifest, _highlight_rel(code), attachment=attachment)
    except Exception as exc:  # noqa: BLE001 — log the type only; messages can carry sensitive detail
        error = s3.as_store_error(exc)
        print(f"[S3] highlight link for {job_id}/{code} not issued: {error.code} ({type(exc).__name__})")
        return jsonify({"error": redact(str(error)), "code": error.code}), s3.ERROR_STATUS.get(error.code, 503), NO_STORE
    print(f"[S3] highlight link for {job_id}/{code} issued")
    return jsonify(payload), 200, NO_STORE


@app.route("/stream/highlight/<job_id>")
@app.route("/stream/highlight/<job_id>/<lang_code>")
def stream_highlight(job_id, lang_code: str = "en"):
    """Inline MP4 for the browser <video>. Redirects to a fresh one-hour S3 link."""
    code = (lang_code or "en").strip().lower()
    if code not in LOCALES:
        return "Invalid language", 400
    return _file_redirect(job_id, _highlight_rel(code), attachment=False)


@app.route("/download/highlight/<job_id>")
@app.route("/download/highlight/<job_id>/<lang_code>")
def download_highlight(job_id, lang_code: str = "en"):
    """Download highlight_<lang>.mp4. Redirects to a fresh one-hour S3 link."""
    code = (lang_code or "en").strip().lower()
    if code not in LOCALES:
        return "Invalid language", 400
    return _file_redirect(job_id, _highlight_rel(code), attachment=True)

def log_runpod_ssh_health_on_startup() -> None:
    """Warn when RunPod GPU mode is on but SSH is not reachable."""
    try:
        import os

        from src.pipeline_runtime import RUNTIME_RUNPOD, load_pipeline_runtime
        from src.runpod_ssh import get_runpod_ssh_probe_status

        if load_pipeline_runtime() != RUNTIME_RUNPOD:
            return
        from src.compute_deploy import COMPUTE_MODE_SERVERLESS, load_compute_mode

        if load_compute_mode() == COMPUTE_MODE_SERVERLESS:
            return
        prior_probe = os.environ.get("RUNPOD_SSH_PROBE_TIMEOUT")
        os.environ["RUNPOD_SSH_PROBE_TIMEOUT"] = "12"
        try:
            status = get_runpod_ssh_probe_status()
        finally:
            if prior_probe is None:
                os.environ.pop("RUNPOD_SSH_PROBE_TIMEOUT", None)
            else:
                os.environ["RUNPOD_SSH_PROBE_TIMEOUT"] = prior_probe
        if status.get("ok"):
            print(f"[startup] RunPod SSH OK ({status.get('target')})")
            return
        print(
            "[startup] RunPod GPU mode is enabled but SSH is not reachable. "
            "Jobs will fail quickly at the heavy phase unless you fix SSH or "
            "switch to local mode on the home page."
        )
        if status.get("error"):
            print(f"[startup] SSH probe: {status['error'][:500]}")
    except Exception as exc:
        print(f"[startup] RunPod SSH health check skipped: {safe_exc(exc)}")


if __name__ == "__main__":
    print("Starting Video Summarization Web App...")
    log_ffmpeg_subtitles_status_on_startup()
    log_runpod_ssh_health_on_startup()
    _warn_if_storage_unavailable()
    from src.endpoint_capacity import start_background_recovery, startup_check

    startup_check(print)  # prunes expired job leases; sets endpoint max workers to 0 when none remain
    start_background_recovery(print)  # repeats that check every 5 min while the app runs
    # Localhost only, debugger off. The Werkzeug debugger must never be reachable from the network. Set
    # VIDEO_SUMMARIZER_DEBUG=1 for local debugging only.
    _flask_debug = os.environ.get("VIDEO_SUMMARIZER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    print("Open: http://localhost:5000")
    app.run(debug=_flask_debug, host="127.0.0.1", port=5000)
