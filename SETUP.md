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
Another process is using it (on macOS this is sometimes AirPlay Receiver). Either stop that process, or edit the bottom of `app.py` to run on a different port, e.g. `app.run(debug=True, port=5001)`.

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
