# Automatic Video Summarization using AI

Convert long educational videos into concise summaries with audio explanations and subtitles.

## Features
- Download YouTube educational videos
- Extract transcript with domain-specific term correction
- Generate concise summaries using local LLM
- Create audio explanations via TTS
- Generate synchronized subtitles (SRT)

## Quick Start (Week 1: Foundation)

### Prerequisites
- **FFmpeg**: Required for audio extraction
  - macOS: `brew install ffmpeg`
  - Ubuntu: `sudo apt-get install ffmpeg`
  - Windows: Download from https://ffmpeg.org/download.html

- **Ollama** (for Week 2): Already installed? Check: `ollama --version`

### Setup
```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Test each component
# Day 1-2: Test YouTube download
python -c "from src.downloader import download_youtube_video; help(download_youtube_video)"

# Day 3: Test audio extraction
python -c "from src.audio_extractor import extract_audio; help(extract_audio)"

# Day 4: Test transcription
python -c "from src.transcriber import transcribe_audio; help(transcribe_audio)"
```

### Week 1 Pipeline (Days 1-4)
```bash
# 1. Download a YouTube video (example)
python src/downloader.py "https://www.youtube.com/watch?v=XXXX"
# Output: downloads/[video-name].mp4

# 2. Extract audio
python src/audio_extractor.py downloads/[video-name].mp4
# Output: output/audio.mp3

# 3. Transcribe with term correction
python src/transcriber.py output/audio.mp3
# Outputs:
#   - output/transcript.txt (full transcription)
#   - output/segments.json (segments with timestamps)
#   - output/uncertain_terms.json (terms to review)
```

## Project Structure
```
video-summarizer/
├── src/
│   ├── main.py              # CLI entry point
│   ├── youtube_downloader.py # yt-dlp wrapper
│   ├── transcriber.py       # faster-whisper + term correction
│   ├── summarizer.py        # Llama 3.1 via Ollama
│   ├── tts_generator.py     # Piper TTS for audio
│   └── segment_linker.py    # Match summary to video timestamps
├── outputs/                 # Generated summaries, audio, subtitles
├── downloads/               # Downloaded videos
├── tests/                   # Test files
└── requirements.txt
```

## Dependencies
- Python 3.9+
- faster-whisper
- Ollama (for local LLM)
- FFmpeg
- Piper TTS

## Dependencies (Week 1)

| Module | Purpose | Source |
|--------|---------|--------|
| faster-whisper | Speech-to-text (2-4× faster than Whisper) | pip |
| pydub | Audio processing | pip |
| yt-dlp | YouTube download | pip |
| subtitle-gen | Term correction & dictionaries | local import |

## Configuration

Edit `config.py` to customize:
- `WHISPER_MODEL`: Model size (tiny, base, small, medium, large)
  - For college project: use "base" (fast, good accuracy, ~140MB)
  - First run downloads model to `~/.cache/whisper/`
- `VIDEO_QUALITY`: Download quality (720p recommended for balance)
- Directory paths

## Term Correction

Subtitle-gen's domain-specific term dictionary is automatically loaded and applied during transcription.

**To review uncertain terms after transcription:**
```bash
python /Users/uks/Documents/Recordings/subtitle-gen/generate.py --review
```

**To add new terms:**
```bash
python /Users/uks/Documents/Recordings/subtitle-gen/generate.py --learn "misheard" "correct"
```

## Troubleshooting

### FFmpeg not found
```bash
which ffmpeg
# If not found, install using package manager (see Prerequisites)
```

### Whisper model download hangs
- First run downloads ~140MB model
- Subsequent runs use cached model (~/.cache/whisper/)
- Models are stored locally - no internet needed after first download

### Memory issues on 8GB RAM laptop
- Use "base" model (not medium/large)
- Close other applications during transcription
- Processing takes ~2-3× real-time for base model

### Poor transcription quality
- Ensure audio is clear (no heavy background noise)
- Consider using "small" model for better accuracy (slightly slower)
- Add domain-specific terms to terms.json

## Status
✅ **Week 1 (Foundation)**: Downloader, audio extractor, transcriber  
🚧 **Week 2 (Summarization)**: Ollama integration, text summarization  
⏳ **Week 3 (Output)**: Subtitles, TTS, CLI integration
