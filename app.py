#!/usr/bin/env python3
"""Flask web app for video summarization."""

import json
import re
import sys
import threading
from pathlib import Path
from typing import Union
from flask import Flask, render_template, request, jsonify, redirect, send_file
from datetime import datetime
import uuid
import os
import shutil

sys.path.insert(0, str(Path(__file__).parent))

from src.downloader import handle_video_source, download_youtube_video, get_local_video
from src.audio_extractor import extract_audio
from src.subtitle_generator import generate_subtitles
from src.tts_generator import generate_audio_explanation
from config import OUTPUT_DIR, DOWNLOADS_DIR, SUMMARIZATION_MODEL, choose_whisper_model
from src.job_video import copy_source_video_to_job
from src.highlight_ffmpeg import log_ffmpeg_subtitles_status_on_startup
from src.highlight_jobs import (
    highlight_file_for_lang,
    list_highlight_downloads,
    list_highlight_language_status,
    list_output_languages,
    read_highlight_status,
    write_highlight_status,
)
from src.highlight_pipeline import retry_highlight_languages
from src import s3_video_store as s3
from src.locales import LOCALES, parse_language_list
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
job_history = []
UPLOAD_DIR = Path("/tmp/videosummarizer_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

def enrich_history_entry(entry: dict) -> dict:
    """Attach on-disk artifacts (highlights, summary langs, audio) for history UI."""
    job_id = entry.get("job_id", "")
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        return entry

    highlights = list_highlight_downloads(job_dir)
    entry["highlights"] = highlights
    entry["has_highlight_video"] = len(highlights) > 0
    entry["s3_stored_highlights"] = s3.stored_highlight_langs(job_dir)

    i18n = summary_translations_for_api(job_dir)
    tr_keys = list((i18n.get("summary_translations") or {}).keys())
    entry["summary_languages"] = ["en"] + [c for c in tr_keys if c != "en"]
    entry["has_summary"] = (job_dir / "summary.txt").is_file()
    entry["has_audio"] = (job_dir / "audio_explanation.mp3").is_file()

    if not entry.get("output_languages"):
        entry["output_languages"] = list_output_languages(job_dir)

    metadata_path = job_dir / "summary_metadata.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            entry.setdefault("source", metadata.get("source", entry.get("source", job_id)))
            entry.setdefault("created_at", metadata.get("created_at", ""))
            entry.setdefault("completed_at", metadata.get("completed_at", ""))
            entry.setdefault("status", metadata.get("status", entry.get("status", "completed")))
        except (json.JSONDecodeError, OSError):
            pass
    return entry


def load_existing_jobs():
    """Scan output directory and load existing job history."""
    if not OUTPUT_DIR.exists():
        return

    for job_dir in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue

        job_id = job_dir.name

        if any(j["job_id"] == job_id for j in job_history):
            continue

        summary_path = job_dir / "summary.txt"
        metadata_path = job_dir / "summary_metadata.json"
        if not summary_path.is_file() and not metadata_path.is_file():
            continue

        entry: dict = {
            "job_id": job_id,
            "source": job_id,
            "created_at": datetime.fromtimestamp(job_dir.stat().st_mtime).isoformat(),
            "status": "completed" if summary_path.is_file() else "processing",
        }
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                entry["source"] = metadata.get("source", entry["source"])
                entry["created_at"] = metadata.get("created_at", entry["created_at"])
                entry["completed_at"] = metadata.get("completed_at", "")
                entry["status"] = metadata.get(
                    "status",
                    "completed" if summary_path.is_file() else "processing",
                )
            except (json.JSONDecodeError, OSError):
                pass

        job_history.append(enrich_history_entry(entry))

load_existing_jobs()

class JobProgress:
    """Track job progress."""
    def __init__(self, job_id, source):
        self.job_id = job_id
        self.source = source
        self.current_step = None
        self.progress = 0.0
        self.status = "processing"
        self.log_lines = []
        self.results = None
        self.error = None
        self.created_at = datetime.now()
        self.completed_at = None
        self.job_output_dir = OUTPUT_DIR / job_id
        self.job_output_dir.mkdir(parents=True, exist_ok=True)

    def add_log(self, message):
        """Add a log line."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{timestamp}] {message}")
        print(f"[{self.job_id}] {message}")

    def set_step(self, step_name, progress):
        """Update current step and progress."""
        self.current_step = step_name
        self.progress = progress
        self.add_log(f"Step: {step_name} ({progress*100:.0f}%)")

    def to_dict(self):
        """Convert to API response format."""
        return {
            "current_step": self.current_step,
            "progress": self.progress,
            "status": self.status,
            "log_lines": self.log_lines[-20:],
        }

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
    runtime_ctx = resolve_runtime_for_job(pipeline_runtime)
    env_prior = apply_runtime_side_effects(runtime_ctx)
    executor = build_executor(runtime_ctx)
    transcription: dict = {}

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
                video_path = download_youtube_video(source, DOWNLOADS_DIR, quality=360, max_retries=3)
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
                job.add_log("Checking RunPod connectivity before GPU work...")
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
        job.set_step("transcribing", 0.35)
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
        hl_status = read_highlight_status(job.job_output_dir)
        hl_status["requested_languages"] = output_lang_codes
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
            job.add_log(f"Audio/subtitle generation failed: {str(e)}")
            job.set_step("generating_subtitles", 0.98)

        wants_highlight = highlight_video or any(c != "en" for c in output_lang_codes)
        if wants_highlight:
            lang_codes = output_lang_codes
            job.set_step("highlight_topics", 0.92)
            job.add_log("Selecting topics for highlight video...")
            job.add_log(f"Highlight languages: {', '.join(lang_codes)} (translator: Google)")
            try:
                with open(job.job_output_dir / "segments.json", encoding="utf-8") as f:
                    segments = json.load(f)
                job.set_step("highlight_visuals", 0.94)
                job.add_log("Extracting scene visuals and narration...")
                job.set_step("highlight_assembly", 0.97)
                highlight_out = executor.run_highlight_phase(
                    job_id=job_id,
                    video_path=staged_video,
                    segments=segments,
                    job_output_dir=job.job_output_dir,
                    summarization_model=model,
                    compression=compression,
                    languages=lang_codes,
                    log=job.add_log,
                )
                if isinstance(highlight_out, dict):
                    highlight_paths = dict(highlight_out)
                    for lang, path in highlight_out.items():
                        job.add_log(f"Highlight {lang} ready: {path.name}")
                else:
                    highlight_paths = {"en": highlight_out}
                    job.add_log(f"Highlight en ready: {highlight_out.name}")
                for row in list_highlight_language_status(job.job_output_dir):
                    if row["status"] == "failed":
                        job.add_log(f"Highlight {row['code']} failed: {row.get('message', '')}")
            except Exception as e:
                job.add_log(f"Highlight video failed: {e}")
            job.set_step("highlight_complete", 1.0)
        else:
            job.set_step("generating_subtitles", 1.0)

        job.add_log("Pipeline completed successfully!")
        job.status = "completed"
        job.progress = 1.0
        job.completed_at = datetime.now()

        summary_path = job.job_output_dir / "summary.txt"
        summary_text = open(summary_path, encoding="utf-8").read() if summary_path.exists() else ""
        summary_text = clean_summary_text_for_display(summary_text, "en")

        highlight_downloads = list_highlight_downloads(job.job_output_dir)
        highlight_status = list_highlight_language_status(job.job_output_dir)
        summary_i18n = summary_translations_for_api(job.job_output_dir)
        results = {
            "status": "completed",
            "summary": summary_text,
            "transcript_length": len(transcription.get("text", "")),
            "summary_length": len(summary_text),
            "has_audio": (job.job_output_dir / "audio_explanation.mp3").is_file(),
            "highlight_video": len(highlight_downloads) > 0,
            "highlight_languages": highlight_downloads,
            "highlight_language_status": highlight_status,
            **summary_i18n,
            "output_languages": list_output_languages(job.job_output_dir),
        }

        job.results = results

        for item in job_history:
            if item["job_id"] == job_id:
                item["status"] = "completed"
                item["completed_at"] = job.completed_at.isoformat()
                enrich_history_entry(item)
                break

        metadata = {
            "job_id": job_id,
            "source": job.source,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat(),
            "status": "completed",
            "transcript_length": len(transcription.get("text", "")),
            "summary_length": len(summary_text)
        }
        with open(job.job_output_dir / "summary_metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        return True

    except Exception as e:
        error_msg = f"Error: {str(e)}"
        job.add_log(error_msg)
        job.status = "error"
        job.error = str(e)
        job.completed_at = datetime.now()
        import traceback
        traceback.print_exc()

        try:
            for item in job_history:
                if item["job_id"] == job_id:
                    item["status"] = "error"
                    item["completed_at"] = job.completed_at.isoformat()
                    item["error"] = str(e)
                    break
        except Exception as hist_exc:
            print(f"[WARN] job_history update failed for {job_id}: {hist_exc}")

        try:
            metadata = {
                "job_id": job_id,
                "source": job.source,
                "created_at": job.created_at.isoformat(),
                "completed_at": job.completed_at.isoformat(),
                "status": "error",
                "error": str(e),
            }
            job.job_output_dir.mkdir(parents=True, exist_ok=True)
            with open(job.job_output_dir / "summary_metadata.json", "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as meta_exc:
            print(f"[WARN] summary_metadata write failed for {job_id}: {meta_exc}")

        return False
    finally:
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
    return render_template("gpu_selection.html", report=report)


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
    result = stop_saved_runpod_pod()
    status = 200 if result.get("ok") else 400
    if result.get("ok"):
        result["deploy_status"] = get_compute_deploy_status(refresh_live=False)
    return jsonify(result), status


@app.route("/api/compute-network-volume-delete", methods=["POST"])
def compute_network_volume_delete_api():
    """Detach and delete the RunPod network volume (destroys cached model weights)."""
    data = request.get_json(silent=True) or {}
    confirm = (data.get("confirm") or request.form.get("confirm") or "").strip()
    result = delete_saved_network_volume(confirm=confirm)
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
    result = save_selection_and_deploy(
        selection_id,
        client_offer=client_offer,
        serverless_models=serverless_models,
        serverless_data_center_id=serverless_dc,
        docker_hub_user=docker_hub_user,
    )
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
        job = JobProgress(job_id, source)
        jobs[job_id] = job

        job_history.append({
            "job_id": job_id,
            "source": display_name,
            "created_at": job.created_at.isoformat(),
            "status": "processing"
        })

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

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/progress/<job_id>", methods=["GET"])
def progress(job_id):
    """Get progress updates."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    job = jobs[job_id]
    return jsonify(job.to_dict()), 200

@app.route("/api/results/<job_id>", methods=["GET"])
def results(job_id):
    """Get final results."""
    job = jobs.get(job_id)

    if job:
        if job.status == "completed":
            return jsonify(job.results), 200
        elif job.status == "error":
            return jsonify({"error": job.error}), 500
        else:
            return jsonify({"status": "processing"}), 202

    job_dir = OUTPUT_DIR / job_id
    summary_path = job_dir / "summary.txt"
    transcript_path = job_dir / "transcript.txt"

    if summary_path.exists():
        try:
            summary_text = open(summary_path, encoding="utf-8").read()
            summary_text = clean_summary_text_for_display(summary_text, "en")
            transcript_length = len(open(transcript_path, encoding="utf-8").read()) if transcript_path.exists() else 0

            highlight_downloads = list_highlight_downloads(job_dir)
            highlight_status = list_highlight_language_status(job_dir)
            summary_i18n = summary_translations_for_api(job_dir)
            return jsonify({
                "status": "completed",
                "summary": summary_text,
                "transcript_length": transcript_length,
                "summary_length": len(summary_text),
                "has_audio": (job_dir / "audio_explanation.mp3").is_file(),
                "highlight_video": len(highlight_downloads) > 0,
                "highlight_languages": highlight_downloads,
                "highlight_language_status": highlight_status,
                **summary_i18n,
                "output_languages": list_output_languages(job_dir),
            }), 200
        except Exception as e:
            print(f"[ERROR] Failed to load old job {job_id}: {e}")
            return jsonify({"error": "Failed to load job data"}), 500

    return jsonify({"error": "Job not found"}), 404

@app.route("/api/history", methods=["GET"])
def history():
    """Get job history (all processed videos)."""
    enriched = [enrich_history_entry(dict(entry)) for entry in job_history]
    return jsonify({
        "jobs": list(reversed(enriched)),
        "total": len(enriched),
    }), 200

@app.route("/download/summary/<job_id>")
@app.route("/download/summary/<job_id>/<lang_code>")
def download_summary(job_id, lang_code: str = "en"):
    """Download summary text file (English or translated)."""
    try:
        code = (lang_code or "en").strip().lower()
        if code not in LOCALES:
            return "Invalid language", 400
        job_dir = _job_output_dir(job_id)
        if code == "en":
            file_path = job_dir / "summary.txt"
            download_name = "summary.txt"
        else:
            file_path = job_dir / f"summary_{code}.txt"
            download_name = f"summary_{code}.txt"
            if not file_path.is_file():
                text = summary_text_for_language(job_dir, code)
                if text:
                    file_path.write_text(text, encoding="utf-8")

        if file_path.is_file():
            return send_file(
                file_path,
                as_attachment=True,
                download_name=download_name,
                mimetype="text/plain",
            )
        return "File not found", 404
    except Exception as e:
        print(f"[ERROR] Download summary failed: {e}")
        return "File not found", 404

@app.route("/download/subtitles/<job_id>")
def download_subtitles(job_id):
    """Download SRT subtitle file."""
    try:
        job = jobs.get(job_id)
        if job:
            file_path = job.job_output_dir / "summary.srt"
        else:
            file_path = OUTPUT_DIR / job_id / "summary.srt"

        if file_path.exists():
            return send_file(
                file_path,
                as_attachment=True,
                download_name="summary.srt",
                mimetype="text/plain"
            )
        return "File not found", 404
    except Exception as e:
        print(f"[ERROR] Download subtitles failed: {e}")
        return "File not found", 404

@app.route("/download/audio/<job_id>")
def download_audio(job_id):
    """Download audio explanation."""
    try:
        job = jobs.get(job_id)
        if job:
            file_path = job.job_output_dir / "audio_explanation.mp3"
        else:
            file_path = OUTPUT_DIR / job_id / "audio_explanation.mp3"

        if file_path.exists():
            return send_file(
                file_path,
                as_attachment=True,
                download_name="audio_explanation.mp3",
                mimetype="audio/mpeg"
            )
        return "File not found", 404
    except Exception as e:
        print(f"[ERROR] Download audio failed: {e}")
        return "File not found", 404

def _job_output_dir(job_id: str) -> Path:
    job = jobs.get(job_id)
    if job:
        return job.job_output_dir
    return OUTPUT_DIR / job_id


@app.route("/api/highlight/retry/<job_id>", methods=["POST"])
def retry_highlight(job_id):
    """Re-run failed highlight languages (reuses cache + shared visuals)."""
    try:
        if not _valid_job_id(job_id):
            return jsonify({"error": "Job not found"}), 404
        from src.pipeline_runtime import RUNTIME_RUNPOD, load_pipeline_runtime

        if load_pipeline_runtime() == RUNTIME_RUNPOD:
            # The retry path renders on this machine and never uploads to S3. Refuse rather than do local
            # processing in RunPod mode. A serverless retry needs its own implementation and tests.
            return jsonify({
                "error": "Retrying a highlight is not available in RunPod mode yet, so nothing was rendered "
                         "locally. Switch to Local mode to retry on this machine, or re-run the job on RunPod.",
                "code": "retry_unavailable_runpod",
            }), 409
        job_dir = _job_output_dir(job_id)
        if not job_dir.is_dir():
            return jsonify({"error": "Job not found"}), 404

        body = request.get_json(silent=True) or {}
        langs = body.get("languages")
        lang_list = None
        if isinstance(langs, list) and langs:
            lang_list = [str(x).strip().lower() for x in langs if str(x).strip()]

        model = body.get("model")
        compression = body.get("compression")

        retry_highlight_languages(
            job_dir,
            lang_list,
            model=model,
            compression=compression,
        )
        status = list_highlight_language_status(job_dir)
        downloads = list_highlight_downloads(job_dir)
        return jsonify(
            {
                "ok": True,
                "highlight_language_status": status,
                "highlight_languages": downloads,
                "highlight_video": len(downloads) > 0,
            }
        ), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


NO_STORE = {"Cache-Control": "no-store"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")  # job IDs are the first 8 hex characters of a UUID


def _valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id or ""))


def _upload_failed_response(job_id: str, code: str):
    """Refuse to serve a local copy whose S3 upload failed. The user sees why, not a stale file."""
    print(f"[S3] highlight {job_id}/{code} not served: S3 upload failed")
    message = "This highlight could not be saved to S3, so it is not available. Re-run the job to try again."
    return message, 503, NO_STORE


def _highlight_link(job_id: str, code: str, attachment: bool) -> dict:
    return s3.highlight_url(_job_output_dir(job_id), code, s3.highlight_store(), attachment=attachment)


@app.route("/api/highlight-url/<job_id>/<lang_code>", methods=["GET"])
def api_highlight_url(job_id, lang_code):
    """Fresh one-hour S3 link for a stored highlight. Built on every call, never saved or cached.

    404 with code 'not_in_s3' means the highlight exists only on local disk (local mode or a job from before S3).
    """
    code = (lang_code or "en").strip().lower()
    if not _valid_job_id(job_id):
        return jsonify({"error": "Job not found"}), 404, NO_STORE
    if code not in LOCALES:
        return jsonify({"error": "Invalid language"}), 400, NO_STORE
    if not _job_output_dir(job_id).is_dir():
        return jsonify({"error": "Job not found"}), 404, NO_STORE
    attachment = request.args.get("attachment", "") in ("1", "true")
    try:
        payload = _highlight_link(job_id, code, attachment)
    except s3.HighlightStoreError as exc:
        print(f"[S3] highlight link for {job_id}/{code} not issued: {exc.code}")
        return jsonify({"error": str(exc), "code": exc.code}), s3.ERROR_STATUS.get(exc.code, 500), NO_STORE
    except Exception as exc:  # noqa: BLE001 — log the type only; messages can carry sensitive detail
        print(f"[S3] highlight link for {job_id}/{code} failed: {type(exc).__name__}")
        return jsonify({"error": "S3 is unavailable for this highlight.", "code": "s3_unavailable"}), 503, NO_STORE
    print(f"[S3] highlight link for {job_id}/{code} issued")
    return jsonify(payload), 200, NO_STORE


def _redirect_to_stored_highlight(job_id: str, code: str, attachment: bool):
    """Send the browser to a fresh S3 link. Nothing falls back to the local file when S3 is the source."""
    try:
        payload = _highlight_link(job_id, code, attachment)
    except s3.HighlightStoreError as exc:
        print(f"[S3] highlight redirect for {job_id}/{code} not issued: {exc.code}")
        return str(exc), s3.ERROR_STATUS.get(exc.code, 500), NO_STORE
    except Exception as exc:  # noqa: BLE001
        print(f"[S3] highlight redirect for {job_id}/{code} failed: {type(exc).__name__}")
        return "S3 is unavailable for this highlight.", 503, NO_STORE
    response = redirect(payload["url"], code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/stream/highlight/<job_id>")
@app.route("/stream/highlight/<job_id>/<lang_code>")
def stream_highlight(job_id, lang_code: str = "en"):
    """Inline MP4 for browser <video> (supports Range seeking)."""
    try:
        if not _valid_job_id(job_id):
            return "File not found", 404
        code = (lang_code or "en").strip().lower()
        if code not in LOCALES:
            return "Invalid language", 400
        job_dir = _job_output_dir(job_id)
        if s3.upload_failed_for_lang(job_dir, code):
            return _upload_failed_response(job_id, code)
        if s3.has_stored_highlight(job_dir, code):
            return _redirect_to_stored_highlight(job_id, code, attachment=False)
        file_path = highlight_file_for_lang(job_dir, code)
        if file_path.is_file():
            return send_file(
                file_path,
                mimetype="video/mp4",
                as_attachment=False,
                download_name=file_path.name,
                conditional=True,
            )
        return "File not found", 404
    except ValueError:
        return "Invalid language", 400
    except Exception as e:
        print(f"[ERROR] Stream highlight failed: {e}")
        return "File not found", 404


@app.route("/download/highlight/<job_id>")
@app.route("/download/highlight/<job_id>/<lang_code>")
def download_highlight(job_id, lang_code: str = "en"):
    """Download highlight_<lang>.mp4 for a completed job."""
    try:
        if not _valid_job_id(job_id):
            return "File not found", 404
        code = (lang_code or "en").strip().lower()
        if code not in LOCALES:
            return "Invalid language", 400
        job_dir = _job_output_dir(job_id)
        if s3.upload_failed_for_lang(job_dir, code):
            return _upload_failed_response(job_id, code)
        if s3.has_stored_highlight(job_dir, code):
            return _redirect_to_stored_highlight(job_id, code, attachment=True)
        file_path = highlight_file_for_lang(job_dir, code)
        if file_path.is_file():
            return send_file(
                file_path,
                as_attachment=True,
                download_name=file_path.name,
                mimetype="video/mp4",
            )
        return "File not found", 404
    except ValueError:
        return "Invalid language", 400
    except Exception as e:
        print(f"[ERROR] Download highlight failed: {e}")
        return "File not found", 404

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
        print(f"[startup] RunPod SSH health check skipped: {exc}")


if __name__ == "__main__":
    print("Starting Video Summarization Web App...")
    log_ffmpeg_subtitles_status_on_startup()
    log_runpod_ssh_health_on_startup()
    from src.endpoint_capacity import start_background_recovery, startup_check

    startup_check(print)  # prunes expired job leases; sets endpoint max workers to 0 when none remain
    start_background_recovery(print)  # repeats that check every 5 min while the app runs
    # Localhost only, debugger off. The Werkzeug debugger must never be reachable from the network. Set
    # VIDEO_SUMMARIZER_DEBUG=1 for local debugging only.
    _flask_debug = os.environ.get("VIDEO_SUMMARIZER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    print("Open: http://localhost:5000")
    app.run(debug=_flask_debug, host="127.0.0.1", port=5000)
