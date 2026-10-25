# Setup Guide

Complete instructions to get this project running on a fresh computer.

## 1. Prerequisites

Install these before touching the project:

### Python 3.9+
Check with `python3 --version`. If missing, install from [python.org](https://www.python.org/downloads/) or via a package manager.

### FFmpeg
Required for audio extraction from video files.

- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add it to your `PATH`.

Verify: `ffmpeg -version`

### Ollama
Runs the local LLM used for summarization.

- **macOS:** `brew install ollama` (or download from [ollama.com](https://ollama.com))
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`
- **Windows:** Download the installer from [ollama.com](https://ollama.com)

Start the Ollama service (if it doesn't start automatically):
```bash
ollama serve
```
Leave this running in its own terminal, or let it run as a background service (default on macOS/Windows installs).

Pull the model(s) this app uses — at minimum the default one:
```bash
ollama pull llama3.2:3b
```
Optionally pull the higher-quality alternative available in the UI's model selector:
```bash
ollama pull qwen2.5:7b-instruct
```

Verify Ollama is reachable:
```bash
curl http://localhost:11434
```

## 2. Get the Code

```bash
git clone https://github.com/Sanjay-UK/video-summarizer.git
cd video-summarizer
```

## 3. Set Up Python Environment

Using a virtual environment is recommended to keep dependencies isolated:

```bash
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows
```

Install dependencies:
```bash
pip install -r requirements.txt
```

This installs:
- `flask` — web server
- `yt-dlp` — YouTube video downloading
- `faster-whisper` — audio transcription
- `pydub` — audio processing
- `ollama` — Python client for the local LLM
- `edge-tts` — primary text-to-speech engine
- `gtts` — fallback text-to-speech engine

## 4. Run the App

Make sure Ollama is running (Step 1), then:

```bash
python3 app.py
```

The Flask server starts on **http://localhost:5000**. Open that URL in a browser.

On first run, the app auto-creates two local folders (already excluded from git via `.gitignore`):
- `downloads/` — temporary cache for downloaded videos
- `output/` — per-job results (transcripts, summaries, generated audio, subtitles), one subfolder per job ID

## 5. Using the App

1. Paste a YouTube URL, or switch to the "Upload Video" tab to use a local file.
2. Choose a summarization model (Llama 3.2 is faster; Qwen 2.5 is higher quality but slower).
3. Choose an analysis depth (Quick / Balanced / Comprehensive) — this controls how many concepts from the video are kept in the summary, not a fixed word count, so it scales correctly for videos of any length.
4. Click **Summarize Video** and watch the live progress through each pipeline stage: Download → Extract Audio → Transcribe → Summarize → Generate Audio → Generate Subtitles.
5. Once complete, you can read the summary, listen to the narrated audio (with natural pacing that reflects the summary's point hierarchy), and download the synced SRT subtitle file.
6. Every past run is listed under **Processing History** on the dashboard and can be reopened directly by its job ID — each job also gets a shareable URL (`/job/<id>`).

## 6. Optional: CLI Usage (No Web UI)

For quick one-off summarization without the web interface:
```bash
python3 main.py "https://www.youtube.com/watch?v=VIDEO_ID" --quality 360
python3 main.py "https://www.youtube.com/watch?v=VIDEO_ID" --highlight-video --model llama3.2:3b
```

### Highlight video (optional)

- Burned-in captions need **FFmpeg with libass** (`subtitles` filter). The app checks for this at startup (warning only) and **fails with instructions** when you run a highlight job without it.
- **Resolution order:** `FFMPEG_PATH` environment variable → `ffmpeg` on `PATH` → common install locations (Homebrew on macOS).
- **macOS:** `brew install ffmpeg-full`, then either add `/opt/homebrew/opt/ffmpeg-full/bin` to `PATH` or:
  ```bash
  export FFMPEG_PATH=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
  ```
- **Windows (demo machine):** Install a **full** build that includes libass, for example:
  - [Gyan.dev FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/) — download **ffmpeg-release-full** (not the minimal *essentials* zip), unpack to e.g. `C:\ffmpeg`, then:
  ```cmd
  set FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
  ```
  Or add `C:\ffmpeg\bin` to your user **Path** and restart the terminal.
  Verify:
  ```cmd
  ffmpeg -filters 2>&1 | findstr subtitles
  ```
  You should see a line containing `subtitles`. Paths with drive letters (`C:\...`) are escaped automatically for the subtitle burn-in filter.
- **Linux:** `sudo apt install ffmpeg` (ensure libass is included) or use a distro package named `ffmpeg` with subtitles support.
- Edge TTS **7.3+** recommended for word-boundary captions: `pip install -U 'edge-tts>=7.3.1'`. Check: `python3 scripts/check_edge_tts_boundaries.py`
- Each job stores a **copy** of the source file as `output/<job_id>/source.mp4` (upload and YouTube paths).

## Troubleshooting

**"Ollama summarization failed" / connection refused**
Ollama isn't running, or the model isn't pulled. Run `ollama serve` and confirm `ollama list` shows `llama3.2:3b`.

**"FFmpeg not found"**
FFmpeg isn't installed or isn't on your `PATH`. Re-check Step 1 and confirm `ffmpeg -version` works in the same terminal you're running the app from.

**YouTube download fails with an anti-bot / SABR error**
YouTube occasionally blocks automated downloads for certain videos. The app will tell you in the progress log to use the "Upload Video" tab instead — download the video manually (e.g. via a browser extension or site) and upload the file directly.

**`[WARN] Could not import subtitle-gen tools`**
Harmless. This refers to an optional external tool path (`SUBTITLE_GEN_DIR` in `config.py`) used for domain-specific term correction that isn't part of this repo. The app works fully without it.

**Port 5000 already in use**
Another process is using it (on macOS this is sometimes AirPlay Receiver). Either stop that process, or edit the bottom of `app.py` to run on a different port, e.g. `app.run(port=5001)`.

**Slow transcription**
`faster-whisper` runs on CPU by default (`config.py` → `WHISPER_DEVICE = "cpu"`). This is normal on machines without a supported GPU; longer videos will simply take more time.

## Configuration Reference

Key settings live in `config.py` if you want to tune behavior:

| Setting | Default | Purpose |
|---|---|---|
| `WHISPER_MODEL` | `"base"` | Whisper model size (`tiny`, `base`, `small`, `medium`, `large`) — bigger is more accurate but slower |
| `SUMMARIZATION_MODEL` | `"llama3.2:3b"` | Default Ollama model used for summarization |
| `OLLAMA_HOST` | `"http://localhost:11434"` | Where the app looks for the Ollama service |
| `VIDEO_QUALITY` | `"360p"` | Download quality for YouTube videos |

## Network access and debugging

The app listens on **http://localhost:5000 only** (127.0.0.1). It has no user accounts, so anyone who can reach
the port can read its jobs, so it is not exposed to your network. The Flask debugger is off. To debug locally, set
`VIDEO_SUMMARIZER_DEBUG=1` before starting, and never on a shared network. Using the app from another device on
your network needs a deliberate change and its own access control. None is included here.

**Retry in RunPod mode:** the highlight Retry button renders on this machine, so it is refused in RunPod mode with a
message to switch to Local mode or re-run the job. Local mode retries as before.

## Optional: S3 source-video storage for RunPod highlights

Off by default. Local mode and the current worker (`v1.0.11`) are unaffected. When on, the source video is
uploaded to S3 and the worker downloads it through a presigned URL that expires (default 3600 s), instead of
receiving the video bytes in the request.

**Credentials:** never put AWS keys in the repo, `.env`, logs, or prompts. The app always uses a named
profile: `video-summarizer` by default, or the name in `VIDEO_S3_PROFILE`. It never uses the AWS `default`
profile, and it ignores `AWS_PROFILE`. Create the profile on your machine
yourself (you will be prompted for the key; nothing is stored in the project):

    aws configure --profile video-summarizer

**Read-only check (uploads and deletes nothing):**

    VIDEO_S3_PROFILE=video-summarizer venv-mac/bin/python scripts/check_video_s3.py

It confirms the caller identity, bucket region (`ap-south-1`), SSE-S3 encryption, public-access blocking,
and that presigned URLs sign. It exits non-zero on any failure.

**Settings** (set in your shell or local launch environment; the profile name is not a secret, so it may
also go in `.env`):

| Variable | Value | Notes |
|---|---|---|
| `VIDEO_S3_PROFILE` | `video-summarizer` | Named AWS profile to use |
| `VIDEO_S3_BUCKET` | `video-summarizer-757374023708-ap-south-1-an` | Default already set in code |
| `VIDEO_S3_REGION` | `ap-south-1` | Default already set in code |
| `VIDEO_S3_PRESIGN_EXPIRES` | `3600` | Seconds; clamped to 60 s – 7 days |
| `VIDEO_S3_ENABLED` | `true` | Turns storage on |
| `VIDEO_S3_WORKER_URL_SUPPORT` | `true` | Set **only** after a worker image that accepts `video_url` jobs is deployed |

Both `VIDEO_S3_ENABLED` and `VIDEO_S3_WORKER_URL_SUPPORT` must be `true`. Setting only the first one keeps
storage off, so a single setting cannot break highlights on the current worker.

**Behavior when on:** the source is uploaded with a unique key (UUID at the bucket root). The worker gets a
presigned URL. The object is deleted only after the RunPod job completes. If the job fails, the object is
kept and its key is recorded in the job folder (`s3_video.json`) for retry or manual cleanup. Presigned URLs
are never written to disk.

**Highlights in S3 (when both flags are on):** each finished highlight is uploaded to the private bucket under
`highlights/<uuid>/<lang>.mp4`, and its key, size, and status are saved in the job folder
(`s3_highlights.json`). No URL is saved anywhere. Every play or download asks the app for a fresh link that lasts
one hour (`/api/highlight-url/<job>/<lang>`). The page asks again once if playback fails. If S3 cannot give a
link, the page shows the error. It never plays the local copy in place of S3.

- **Retention:** highlight objects are kept indefinitely, so History keeps working. Nothing deletes them. A
  retention or cleanup rule would need its own decision and a test.
- **Credentials:** the profile's access key is long-term, so links stay valid until the key is rotated. If the
  profile ever uses temporary credentials that end within an hour, the app refuses to sign a link and says why.
- **Restarting the app without the two flags** stops new uploads. Existing S3 highlights still open in History.

## Endpoint worker capacity (lease lifecycle)

The app keeps the RunPod serverless endpoint at `max_workers=0` whenever it has no active job.

- **How it works:** each job holds a lease from submission until it finishes. Success, failure, and timeout
  all release it. Capacity goes to 0 only when no valid lease remains, and the change is read back from RunPod.
- **Leases** are JSON files in `data/endpoint_leases/`, guarded by a `filelock` lock. The lock works the same
  on macOS and Windows.
- **Expiry:** a lease expires 4 hours after it was acquired. Expiry is age-based only, with no process checks.
- **Startup:** when the app starts, it drops expired leases and, if none remain, sets capacity to 0 and
  verifies it. A failure is printed as a warning and does not stop the app.
- **While running:** the same check repeats every 5 minutes. It never lowers capacity while a valid lease exists.
  This runs only while the app process is running. If the app is stopped or crashes, nothing lowers capacity
  until it starts again.
- **Capacity calls time out after 20 seconds**, so a slow RunPod response cannot hold the lock past the 60-second
  wait that other jobs use.
- **New endpoints are created with max workers 0**, then read back to confirm.
- **A failed release never hides the job's own error.** The lease then expires on its own.
- **Creating or reusing an endpoint** sets capacity to 0 and verifies it. If that cannot be verified, the
  deploy is reported as failed.

**Limitations:**
- Coordination covers **one machine** that shares this project folder. Two machines using the same endpoint
  cannot see each other's leases, and this is not supported.
- Jobs started **outside this app** (RunPod console, other tools) hold no lease and are not tracked.
- If the app crashes mid-job, capacity stays up until the next app start or the lease expires.
