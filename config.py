"""Configuration for video summarizer project."""

import os
from pathlib import Path

# Directories
PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
OUTPUT_DIR = PROJECT_DIR / "output"

# External tool paths
YOUTUBE_TOOLS_DIR = Path(os.path.expanduser("~/Documents/Recordings/video_and_yt"))
SUBTITLE_GEN_DIR = Path(os.path.expanduser("~/Documents/Recordings/subtitle-gen"))

# Whisper settings
WHISPER_MODEL = "base"
WHISPER_DEVICE = "cpu"

# Video settings
VIDEO_QUALITY = "360p"
VIDEO_FORMAT = "mp4"

# Audio settings
AUDIO_FORMAT = "mp3"
AUDIO_SAMPLE_RATE = 16000

# Ensure directories exist
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Ollama settings (for Week 2+)
OLLAMA_HOST = "http://localhost:11434"
SUMMARIZATION_MODEL = "llama3.2:3b"  # Changed to match installed model
SUMMARY_TEMPERATURE = 0.3
SUMMARY_MAX_TOKENS = 2000
SUMMARY_RATIO = 0.4
