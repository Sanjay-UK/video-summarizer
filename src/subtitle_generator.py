"""Generate SRT subtitles from summary text - uses audio-friendly cleaned text."""

import json
from pathlib import Path
from typing import Union

from config import OUTPUT_DIR
from src.text_cleaner import strip_markdown, convert_lists_to_prose
from src.domain_handler import DomainHandler
import re


def format_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clean_subtitle_text(text: str) -> str:
    """Clean text for subtitles using same method as audio.

    Ensures subtitles match the audio by using identical text processing.
    """
    # Strip markdown formatting
    text = strip_markdown(text)

    # Convert lists to natural prose
    text = convert_lists_to_prose(text)

    # Apply domain-aware symbol handling
    text = DomainHandler.process_for_audio(text)

    # Expand abbreviations
    abbreviations = {
        r'\be\.g\.\b': 'for example',
        r'\bi\.e\.\b': 'that is',
        r'\betc\.\b': 'and so on',
        r'\bvs\.\b': 'versus',
        r'\be\.g\b': 'for example',
        r'\bi\.e\b': 'that is',
        r'\bdr\.': 'doctor',
        r'\bmr\.': 'mister',
        r'\bmrs\.': 'missus',
        r'\bms\.': 'miss',
        r'\bprof\.': 'professor',
        r'\bincl\.': 'including',
        r'\besq\.': 'esquire',
    }

    for pattern, replacement in abbreviations.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # Clean whitespace
    text = re.sub(r' +', ' ', text)
    text = text.strip()

    return text


def generate_subtitles(
    summary_segments_path: Union[Path, str] = None,
    output_path: Union[Path, str] = None,
    audio_transcript: str = None,
) -> Path:
    """Generate SRT subtitle file from summary segments.

    When audio_transcript is provided, uses it for subtitle text to ensure
    perfect synchronization between audio and subtitles.

    Args:
        summary_segments_path: Path to summary_segments.json
        output_path: Where to save SRT file
        audio_transcript: Optional audio-friendly transcript text (must match audio)

    Returns:
        Path to generated SRT file
    """
    
    if summary_segments_path is None:
        summary_segments_path = OUTPUT_DIR / "summary_segments.json"
    else:
        summary_segments_path = Path(summary_segments_path)
    
    if not summary_segments_path.exists():
        raise FileNotFoundError(f"Summary segments not found: {summary_segments_path}")
    
    if output_path is None:
        output_path = OUTPUT_DIR / "summary.srt"
    else:
        output_path = Path(output_path)
    
    # Load segments for timing information
    with open(summary_segments_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    # Build SRT content
    srt_lines = []

    if audio_transcript:
        # When audio_transcript is provided, split it into sentences
        # to match the segment count in segments.json
        # Split by periods followed by space, then match to segment count
        sentences = [s.strip() for s in audio_transcript.split('.') if s.strip()]

        # Ensure we have same number of sentences as segments
        # (sometimes splitting may produce more/fewer)
        for i, seg in enumerate(segments, start=1):
            start = format_timestamp(seg.get("start", 0))
            end = format_timestamp(seg.get("end", 0))

            # Use corresponding sentence from audio_transcript if available
            if i - 1 < len(sentences):
                text = sentences[i - 1]
                # Re-add period if this is not the last sentence
                if i < len(segments):
                    text = text + "."
            else:
                # Fallback: use cleaned segment text
                text = clean_subtitle_text(seg.get("text", ""))

            # Build SRT entry
            srt_lines.append(str(i))
            srt_lines.append(f"{start} --> {end}")
            srt_lines.append(text)
            srt_lines.append("")  # Blank line between entries
    else:
        # Original behavior: clean text from segments
        for i, seg in enumerate(segments, start=1):
            start = format_timestamp(seg.get("start", 0))
            end = format_timestamp(seg.get("end", 0))
            text = seg.get("text", "")

            # Clean text using same method as audio to ensure subtitles match audio
            text = clean_subtitle_text(text)

            # Build SRT entry
            srt_lines.append(str(i))
            srt_lines.append(f"{start} --> {end}")
            srt_lines.append(text)
            srt_lines.append("")  # Blank line between entries
    
    # Write SRT file
    srt_content = "\n".join(srt_lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt_content)
    
    print(f"[DONE] Generated {len(segments)} subtitle entries: {output_path}")
    return output_path


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate SRT subtitles from summary")
    parser.add_argument("--segments", "-s", help="Path to summary_segments.json")
    parser.add_argument("--output", "-o", help="Output SRT file")
    
    args = parser.parse_args()
    
    srt_path = generate_subtitles(args.segments, args.output)
    print(f"\n✅ Subtitles generated: {srt_path}")
