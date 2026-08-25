#!/usr/bin/env python3
"""Flask web app for video summarization."""

import json
import sys
import threading
from pathlib import Path
from typing import Union
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
import uuid
import os
import shutil

sys.path.insert(0, str(Path(__file__).parent))

from src.downloader import handle_video_source, download_youtube_video, get_local_video
from src.audio_extractor import extract_audio
from src.transcriber import transcribe_audio
from src.summarizer import summarize_transcript
from src.subtitle_generator import generate_subtitles
from src.tts_generator import generate_audio_explanation
from config import OUTPUT_DIR, DOWNLOADS_DIR

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

jobs = {}
job_history = []
UPLOAD_DIR = Path("/tmp/videosummarizer_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

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

        metadata_path = job_dir / "summary_metadata.json"
        if metadata_path.exists():
            try:
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)

                job_history.append({
                    "job_id": job_id,
                    "source": metadata.get("source", "Unknown"),
                    "created_at": metadata.get("created_at", ""),
                    "completed_at": metadata.get("completed_at", ""),
                    "status": "completed" if (job_dir / "summary.txt").exists() else "processing"
                })
            except:
                pass

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

def process_video(job_id, source, model="llama3.2:3b", compression="medium"):
    """Run complete pipeline in background.

    Args:
        compression: "high" (50%), "medium" (70%), or "low" (85%) compression
    """
    job = jobs[job_id]

    try:
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

        job.set_step("extracting_audio", 0.15)
        job.add_log("Extracting audio from video...")
        audio_path = extract_audio(video_path, job.job_output_dir)
        job.set_step("extracting_audio", 0.25)
        job.add_log(f"Audio extracted: {audio_path.name}")

        job.set_step("transcribing", 0.30)
        job.add_log("Transcribing audio (this may take a few minutes)...")
        transcription = transcribe_audio(audio_path, model="base", output_dir=job.job_output_dir)
        job.set_step("transcribing", 0.65)
        segments_count = len(transcription.get("segments", []))
        job.add_log(f"Transcription complete: {segments_count} segments")

        job.set_step("summarizing", 0.70)
        job.add_log(f"Summarizing with {model} (compression: {compression})...")
        summary = summarize_transcript(
            transcript_path=job.job_output_dir / "transcript.txt",
            segments_path=job.job_output_dir / "segments.json",
            output_dir=job.job_output_dir,
            model=model,
            compression=compression,
        )
        job.set_step("summarizing", 0.85)
        job.add_log("Summarization complete")

        job.set_step("generating_audio", 0.90)
        job.add_log("Generating audio explanation with hierarchy...")
        try:
            summary_text = open(job.job_output_dir / "summary.txt").read()

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

            job.set_step("generating_subtitles", 1.0)
        except Exception as e:
            job.add_log(f"Audio/subtitle generation failed: {str(e)}")
            # Ensure the UI still reflects that the pipeline reached its final step,
            # even if audio/subtitle generation failed partway through.
            job.set_step("generating_subtitles", 1.0)

        job.add_log("Pipeline completed successfully!")
        job.status = "completed"
        job.progress = 1.0
        job.completed_at = datetime.now()

        summary_path = job.job_output_dir / "summary.txt"
        summary_text = open(summary_path).read() if summary_path.exists() else ""

        results = {
            "status": "completed",
            "summary": summary_text,
            "transcript_length": len(transcription.get("text", "")),
            "summary_length": len(summary_text),
        }

        job.results = results

        for item in job_history:
            if item["job_id"] == job_id:
                item["status"] = "completed"
                item["completed_at"] = job.completed_at.isoformat()
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

        for item in job_history:
            if item["job_id"] == job_id:
                item["status"] = "error"
                item["completed_at"] = job.completed_at.isoformat()
                item["error"] = str(e)
                break

        metadata = {
            "job_id": job_id,
            "source": job.source,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat(),
            "status": "error",
            "error": str(e)
        }
        with open(job.job_output_dir / "summary_metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        return False

@app.route("/")
def index():
    """Home page."""
    return render_template("index.html")

@app.route("/job/<job_id>")
def job_page(job_id):
    """Serve the same page for a direct/shared/refreshed job URL.

    The frontend reads the job_id from the URL on load and fetches its results.
    """
    return render_template("index.html")

@app.route("/api/summarize", methods=["POST"])
def summarize():
    """Start summarization process from URL or uploaded file."""
    try:
        url = request.form.get("url", "").strip()
        model = request.form.get("model", "llama3.2:3b")
        compression = request.form.get("compression", "medium")
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

        thread = threading.Thread(
            target=process_video,
            args=(job_id, source, model, compression),
            daemon=True
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
            summary_text = open(summary_path, "r").read()
            transcript_length = len(open(transcript_path, "r").read()) if transcript_path.exists() else 0

            return jsonify({
                "status": "completed",
                "summary": summary_text,
                "transcript_length": transcript_length,
                "summary_length": len(summary_text)
            }), 200
        except Exception as e:
            print(f"[ERROR] Failed to load old job {job_id}: {e}")
            return jsonify({"error": "Failed to load job data"}), 500

    return jsonify({"error": "Job not found"}), 404

@app.route("/api/history", methods=["GET"])
def history():
    """Get job history (all processed videos)."""
    return jsonify({
        "jobs": list(reversed(job_history)),
        "total": len(job_history)
    }), 200

@app.route("/download/summary/<job_id>")
def download_summary(job_id):
    """Download summary text file."""
    try:
        job = jobs.get(job_id)
        if job:
            file_path = job.job_output_dir / "summary.txt"
        else:
            file_path = OUTPUT_DIR / job_id / "summary.txt"

        if file_path.exists():
            return send_file(
                file_path,
                as_attachment=True,
                download_name="summary.txt",
                mimetype="text/plain"
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

if __name__ == "__main__":
    print("Starting Video Summarization Web App...")
    print("Open: http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
