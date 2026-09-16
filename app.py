#!/usr/bin/env python3
"""Flask web app for video summarization."""

import json
import sys
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from datetime import datetime
import uuid

sys.path.insert(0, str(Path(__file__).parent))

from src.downloader import get_local_video
from src.audio_extractor import extract_audio
from src.transcriber import transcribe_audio
from src.summarizer import summarize_transcript
from config import OUTPUT_DIR

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

jobs = {}
UPLOAD_DIR = Path("/tmp/videosummarizer_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

class JobProgress:
    """Track job progress."""
    def __init__(self, job_id, source):
        self.job_id = job_id
        self.source = source
        self.current_step = None
        self.progress = 0.0
        self.status = "processing"
        self.results = None
        self.error = None
        self.created_at = datetime.now()
        self.completed_at = None
        self.job_output_dir = OUTPUT_DIR / job_id
        self.job_output_dir.mkdir(parents=True, exist_ok=True)

    def set_step(self, step_name, progress):
        """Update current step and progress."""
        self.current_step = step_name
        self.progress = progress
        print(f"[{self.job_id}] Step: {step_name} ({progress*100:.0f}%)")

    def to_dict(self):
        """Convert to API response format."""
        return {
            "current_step": self.current_step,
            "progress": self.progress,
            "status": self.status,
        }

def process_video(job_id, source, model="llama3.2:3b", compression="medium"):
    """Run core pipeline: load file → extract audio → transcribe → summarize."""
    job = jobs[job_id]

    try:
        job.set_step("loading", 0.05)
        print(f"[{job_id}] Loading local video file...")
        video_path = get_local_video(source)

        job.set_step("loading", 0.10)
        print(f"[{job_id}] Video ready: {video_path.name}")

        job.set_step("extracting_audio", 0.15)
        print(f"[{job_id}] Extracting audio from video...")
        audio_path = extract_audio(video_path, job.job_output_dir)
        job.set_step("extracting_audio", 0.25)
        print(f"[{job_id}] Audio extracted: {audio_path.name}")

        job.set_step("transcribing", 0.30)
        print(f"[{job_id}] Transcribing audio...")
        transcription = transcribe_audio(audio_path, model="base", output_dir=job.job_output_dir)
        job.set_step("transcribing", 0.65)
        print(f"[{job_id}] Transcription complete: {len(transcription.get('segments', []))} segments")

        job.set_step("summarizing", 0.70)
        print(f"[{job_id}] Summarizing with {model}...")
        summarize_transcript(
            transcript_path=job.job_output_dir / "transcript.txt",
            segments_path=job.job_output_dir / "segments.json",
            output_dir=job.job_output_dir,
            model=model,
            compression=compression,
        )
        job.set_step("summarizing", 1.0)
        print(f"[{job_id}] Summarization complete")

        job.status = "completed"
        job.progress = 1.0
        job.completed_at = datetime.now()

        summary_path = job.job_output_dir / "summary.txt"
        summary_text = open(summary_path).read() if summary_path.exists() else ""

        job.results = {
            "status": "completed",
            "summary": summary_text,
            "transcript_length": len(transcription.get("text", "")),
            "summary_length": len(summary_text),
        }

        metadata = {
            "job_id": job_id,
            "source": job.source,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat(),
            "status": "completed",
            "transcript_length": len(transcription.get("text", "")),
            "summary_length": len(summary_text),
        }
        with open(job.job_output_dir / "summary_metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        return True

    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.completed_at = datetime.now()
        import traceback
        traceback.print_exc()

        metadata = {
            "job_id": job_id,
            "source": job.source,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat(),
            "status": "error",
            "error": str(e),
        }
        with open(job.job_output_dir / "summary_metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        return False

@app.route("/")
def index():
    """Home page."""
    return render_template("index.html")

@app.route("/api/summarize", methods=["POST"])
def summarize():
    """Start summarization process from uploaded file."""
    try:
        model = request.form.get("model", "llama3.2:3b")
        compression = request.form.get("compression", "medium")
        uploaded_file = request.files.get("file")

        if not uploaded_file or not uploaded_file.filename:
            return jsonify({"error": "Please upload a video file"}), 400

        valid_extensions = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm"}
        file_ext = Path(uploaded_file.filename).suffix.lower()

        if file_ext not in valid_extensions:
            return jsonify({"error": f"Invalid file format: {file_ext}. Allowed: {valid_extensions}"}), 400

        upload_path = UPLOAD_DIR / uploaded_file.filename
        uploaded_file.save(str(upload_path))

        job_id = str(uuid.uuid4())[:8]
        job = JobProgress(job_id, str(upload_path))
        jobs[job_id] = job

        thread = threading.Thread(
            target=process_video,
            args=(job_id, str(upload_path), model, compression),
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

    return jsonify(jobs[job_id].to_dict()), 200

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

    return jsonify({"error": "Job not found"}), 404

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
                mimetype="text/plain",
            )
        return "File not found", 404
    except Exception as e:
        print(f"[ERROR] Download summary failed: {e}")
        return "File not found", 404

if __name__ == "__main__":
    print("Starting Video Summarization Web App...")
    print("Open: http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
