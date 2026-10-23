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
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_MODEL_SHORT = os.environ.get("WHISPER_MODEL_SHORT", "tiny")
WHISPER_SHORT_UPLOAD_MAX_SEC = float(os.environ.get("WHISPER_SHORT_UPLOAD_MAX_SEC", "180"))
WHISPER_DEVICE = "cpu"


def choose_whisper_model(duration_seconds: float | None) -> str:
    """Use a smaller/faster Whisper model for short uploads (default: under 3 minutes)."""
    if duration_seconds is not None and duration_seconds <= WHISPER_SHORT_UPLOAD_MAX_SEC:
        return WHISPER_MODEL_SHORT
    return WHISPER_MODEL

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
# Highlight translation: google (default) | nllb | ollama
# Default highlight translation backend (recommended: google via deep-translator)
HIGHLIGHT_TRANSLATOR = os.environ.get("HIGHLIGHT_TRANSLATOR", "google").strip().lower()
HIGHLIGHT_TRANSLATION_MODEL = os.environ.get("HIGHLIGHT_TRANSLATION_MODEL", SUMMARIZATION_MODEL)
# Extra terms to transliterate (comma-separated in env HIGHLIGHT_EXTRA_PROTECTED_TERMS)
_extra_terms_raw = os.environ.get("HIGHLIGHT_EXTRA_PROTECTED_TERMS", "")
HIGHLIGHT_EXTRA_PROTECTED_TERMS = frozenset(
    t.strip().lower() for t in _extra_terms_raw.split(",") if t.strip()
)
HIGHLIGHT_OLLAMA_TEMPERATURE = 0.0
HIGHLIGHT_OLLAMA_SEED = 42
HIGHLIGHT_CAPTION_WORDS_PER_LINE = 7
HIGHLIGHT_INDIC_CAPTION_CHARS_PER_LINE = 18
HIGHLIGHT_TTS_MAX_SCENE_SECONDS = 20.0
def _default_highlight_render_parallel() -> int:
    raw = os.environ.get("HIGHLIGHT_RENDER_MAX_PARALLEL_LANGS", "").strip()
    if raw:
        try:
            return max(1, min(4, int(raw)))
        except ValueError:
            pass
    cpu = os.cpu_count() or 4
    if cpu >= 8:
        return 4
    if cpu >= 4:
        return 3
    return 2


HIGHLIGHT_RENDER_MAX_PARALLEL_LANGS = _default_highlight_render_parallel()
HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC = float(os.environ.get("HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC", "1.0"))
HIGHLIGHT_CLIP_DURATION_SEC = 4.0  # when motion triggers clip mode (configurable)
HIGHLIGHT_PLACEHOLDER_WIDTH = 1280
HIGHLIGHT_PLACEHOLDER_HEIGHT = 720
HIGHLIGHT_FONT_PATH = PROJECT_DIR / "static" / "fonts" / "DejaVuSans.ttf"
HIGHLIGHT_FONT_NAME = "Arial"
# Max spoken words per highlight scene (ties to Analysis Depth / compression)
HIGHLIGHT_NARRATION_MAX_WORDS = {
    "high": 30,
    "medium": 45,
    "low": 70,
}
