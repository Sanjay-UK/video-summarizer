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

# Highlight video (Phase 1 — English only)
HIGHLIGHT_CHUNK_MAX_CHARS = 10_000  # ~2500 tokens; matches summarizer chunk budget
HIGHLIGHT_TOPIC_MERGE_SEC = 40.0
HIGHLIGHT_FRAME_WINDOW_START_OFFSET_SEC = 2.0  # sample from segment_start + 2s
HIGHLIGHT_FRAME_WINDOW_END_OFFSET_SEC = 5.0  # through segment_start + 5s
HIGHLIGHT_FRAME_CANDIDATE_COUNT = 5
HIGHLIGHT_SHARPNESS_THRESHOLD = 35.0  # Laplacian variance; tune per content
HIGHLIGHT_MAX_HEIGHT = 720
HIGHLIGHT_MIN_DISPLAY_SEC = 5.5
HIGHLIGHT_MOTION_THRESHOLD = 35.0  # mean abs pixel diff; clip only if clearly above (talking-head → still)
HIGHLIGHT_MIN_VALID_SCENES = 2  # below this after validation → evenly spaced fallback
HIGHLIGHT_FPS = 30
HIGHLIGHT_AUDIO_RATE = 44100
HIGHLIGHT_OUTPUT_WIDTH = 1280
HIGHLIGHT_OUTPUT_HEIGHT = 720
HIGHLIGHT_TTS_VOICE = "en-US-GuyNeural"
HIGHLIGHT_OLLAMA_TEMPERATURE = 0.0
HIGHLIGHT_OLLAMA_SEED = 42
HIGHLIGHT_CAPTION_WORDS_PER_LINE = 7
HIGHLIGHT_CLIP_DURATION_SEC = 4.0  # when motion triggers clip mode (configurable)
HIGHLIGHT_PLACEHOLDER_WIDTH = 1280
HIGHLIGHT_PLACEHOLDER_HEIGHT = 720
HIGHLIGHT_FONT_PATH = PROJECT_DIR / "static" / "fonts" / "DejaVuSans.ttf"
HIGHLIGHT_FONT_NAME = "Arial"
