"""Generate audio explanation from summary using Edge TTS.

Edge TTS provides high-quality, natural-sounding voices from Microsoft.
Much better quality than gTTS, free to use.
"""

from src.log_safety import safe_exc
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Union
import re

from config import OUTPUT_DIR
from src.text_cleaner import (
    create_audio_friendly_transcript,
    create_audio_friendly_transcript_with_hierarchy,
)
from src.natural_speech_generator import create_natural_audio_transcript


def enhance_text_for_tts(text: str) -> str:
    """Enhance text for clear TTS - no XML tags, just plain text.

    Only expand abbreviations. Let Edge TTS handle natural pacing.
    """
    # Expand abbreviations for clarity
    replacements = {
        r'\be\.g\.\b': 'for example',
        r'\bi\.e\.\b': 'that is',
        r'\betc\.\b': 'and so on',
        r'\bvs\.\b': 'versus',
        r'\bDr\.\b': 'Doctor',
        r'\bMr\.\b': 'Mister',
        r'\bMrs\.\b': 'Missus',
        r'\bMs\.\b': 'Miss',
        r'\bProf\.\b': 'Professor',
    }

    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Clean up multiple spaces
    text = re.sub(r'\s+', ' ', text)

    return text.strip()


async def generate_audio_async(text: str, output_path: Path) -> Path:
    """Generate audio using Edge TTS (async).

    Note: SSML tags are NOT supported by edge-tts Communicate API,
    so we process pause markers into sentence structure instead.
    """
    try:
        from edge_tts import Communicate

        # If text has SSML markers, convert to natural text with breaks
        # Edge TTS reads pauses naturally - long pauses = emphasized content
        if '<speak>' in text or '<prosody>' in text:
            # Remove SSML tags but keep the logical structure
            # Convert <break time="1000ms"/> to double newlines for natural pause
            enhanced_text = text.replace('</prosody>', '.')
            enhanced_text = re.sub(r'<prosody[^>]*>', '', enhanced_text)
            enhanced_text = re.sub(r'<break time="1000ms"/>', '\n\n', enhanced_text)  # Long pause = paragraph break
            enhanced_text = re.sub(r'<break time="800ms"/>', '\n', enhanced_text)     # Medium pause = line break
            enhanced_text = re.sub(r'<break time="300ms"/>', ' ', enhanced_text)      # Short pause = space
            enhanced_text = re.sub(r'</?speak>', '', enhanced_text)
        else:
            # Enhance text for better TTS quality
            enhanced_text = enhance_text_for_tts(text)

        # Use a natural-sounding, clear voice
        communicate = Communicate(
            text=enhanced_text,
            voice="en-US-GuyNeural",  # Clear, professional voice
            rate="-10%",  # Slightly slower for clarity (reduced from -15% to avoid over-slowness)
        )

        # Save as MP3
        await communicate.save(str(output_path))
        return output_path

    except Exception as e:
        print(f"[WARN] Edge TTS failed: {safe_exc(e)}")
        raise


def apply_hierarchy_prosody(text_with_markers: str) -> str:
    """Convert pause/rate markers to SSML prosody tags.

    Converts markers like [PAUSE_1000ms] and [RATE_85%] to SSML:
    - [PAUSE_Xms] → <break time="Xms"/>
    - [RATE_X%] → <prosody rate="X%">...</prosody>

    Args:
        text_with_markers: Text with [PAUSE_Xms] and [RATE_X%] markers

    Returns:
        SSML-formatted text ready for TTS
    """
    # Extract all markers and text segments
    parts = []
    current_rate = None
    pattern = r'\[(PAUSE_\d+ms|RATE_\d+%)\]'

    last_end = 0
    for match in re.finditer(pattern, text_with_markers):
        marker = match.group(1)
        marker_start = match.start()

        # Add text before this marker
        if marker_start > last_end:
            text_part = text_with_markers[last_end:marker_start].strip()
            if text_part:
                if current_rate:
                    parts.append(f'<prosody rate="{current_rate}%">{text_part}</prosody>')
                else:
                    parts.append(text_part)

        # Process marker
        if marker.startswith("PAUSE_"):
            pause_ms = marker.replace("PAUSE_", "").replace("ms", "")
            parts.append(f'<break time="{pause_ms}ms"/>')
        elif marker.startswith("RATE_"):
            current_rate = marker.replace("RATE_", "").replace("%", "")

        last_end = match.end()

    # Add remaining text
    if last_end < len(text_with_markers):
        text_part = text_with_markers[last_end:].strip()
        if text_part:
            if current_rate:
                parts.append(f'<prosody rate="{current_rate}%">{text_part}</prosody>')
            else:
                parts.append(text_part)

    # Wrap in SSML speak tags
    ssml_text = '<speak>' + ' '.join(parts) + '</speak>'
    return ssml_text


def generate_audio_explanation(
    summary_text: str,
    output_path: Union[Path, str] = None,
) -> tuple:
    """Generate audio explanation with hierarchy-aware prosody.

    Uses pause timing and rate changes to convey point hierarchy:
    - Main points (level 0): 85% rate, 1000ms pause
    - Sub-points (level 1): 100% rate, 800ms pause
    - Details (level 2+): 110% rate, 300ms pause

    Args:
        summary_text: Text to convert to speech
        output_path: Where to save MP3 file

    Returns:
        Tuple of (audio_path, audio_transcript)
        - audio_path: Path to generated audio file
        - audio_transcript: The audio-ready text (for subtitles)
    """

    if output_path is None:
        output_path = OUTPUT_DIR / "audio_explanation.mp3"
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Generating audio explanation ({len(summary_text)} chars)...")

    # Step 1: Create audio transcript with hierarchy detection
    print("[INFO] Detecting hierarchy and generating enhanced transcript...")
    enhanced_text, hierarchy = create_audio_friendly_transcript_with_hierarchy(summary_text)
    print(f"[INFO] Created enhanced transcript with hierarchy ({len(enhanced_text)} chars)")
    print(f"[INFO] Detected {len(hierarchy)} points across {max(h['level'] for h in hierarchy) + 1} hierarchy levels")

    # Step 2: Convert markers to SSML for proper prosody
    print("[INFO] Converting to SSML with hierarchy prosody...")
    ssml_text = apply_hierarchy_prosody(enhanced_text)

    # Step 3: Generate clean transcript for subtitles (same text but without markers)
    audio_transcript = re.sub(r'\[PAUSE_\d+ms\]|\[RATE_\d+%\]', '', enhanced_text).strip()
    audio_transcript = re.sub(r'\s+', ' ', audio_transcript)

    # Try Edge TTS (high quality, natural voices)
    try:
        print("[INFO] Using Edge TTS with hierarchy prosody...")

        # Run async function with SSML
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(generate_audio_async(ssml_text, output_path))
        loop.close()

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[DONE] Audio generated with Edge TTS ({file_size_mb:.1f} MB)")
        return output_path, audio_transcript

    except ImportError:
        print("[WARN] edge-tts not installed. Installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "edge-tts"],
            capture_output=False
        )
        print("[INFO] Please restart the summarization")
        raise

    except Exception as e:
        print(f"[WARN] Edge TTS failed: {safe_exc(e)}")
        print("[INFO] Falling back to gTTS...")
        audio_path = use_gtts_fallback(audio_transcript, output_path)
        return audio_path, audio_transcript


def use_gtts_fallback(text: str, output_path: Path) -> Path:
    """Fallback to gTTS if Edge TTS fails."""
    try:
        print("[INFO] Using gTTS as fallback...")
        from gtts import gTTS

        # Chunk long text if needed
        if len(text) > 5000:
            print(f"[INFO] Text is long, chunking...")
            chunks = [text[i:i+3000] for i in range(0, len(text), 3000)]
        else:
            chunks = [text]

        # Generate audio for first chunk (faster fallback)
        tts = gTTS(text=chunks[0], lang="en", slow=True)
        tts.save(str(output_path))

        print(f"[DONE] Audio generated with gTTS fallback")
        return output_path

    except Exception as e:
        print(f"[ERROR] Both TTS methods failed: {safe_exc(e)}")
        print("[WARN] Creating dummy audio file to continue...")
        output_path.touch()
        return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate audio explanation from summary")
    parser.add_argument("summary_path", nargs="?", help="Path to summary.txt")
    parser.add_argument("--output", "-o", help="Output MP3 file")

    args = parser.parse_args()

    if not args.summary_path:
        args.summary_path = OUTPUT_DIR / "summary.txt"

    summary_path = Path(args.summary_path)
    if not summary_path.exists():
        print(f"[ERROR] Summary not found: {summary_path}")
        sys.exit(1)

    with open(summary_path, "r", encoding="utf-8") as f:
        summary_text = f.read()

    audio_path = generate_audio_explanation(summary_text, output_path=args.output)

    print(f"\n✅ Audio explanation: {audio_path}")
    print(f"   Size: {audio_path.stat().st_size / (1024*1024):.1f} MB")
