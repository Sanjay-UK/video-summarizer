"""ASS captions burned into highlight scenes (matches spoken narration)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Sequence

from config import (
    HIGHLIGHT_CAPTION_WORDS_PER_LINE,
    HIGHLIGHT_FONT_NAME,
    HIGHLIGHT_FONT_PATH,
    HIGHLIGHT_OUTPUT_HEIGHT,
    HIGHLIGHT_OUTPUT_WIDTH,
)
from src.highlight_tts import TtsResult


def _ticks_to_seconds(ticks: int) -> float:
    return ticks / 10_000_000.0


def _split_words_into_lines(words: Sequence[str], words_per_line: int) -> List[str]:
    lines: List[str] = []
    bucket: List[str] = []
    for word in words:
        bucket.append(word)
        if len(bucket) >= words_per_line:
            lines.append(" ".join(bucket))
            bucket = []
    if bucket:
        lines.append(" ".join(bucket))
    return lines


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_scene_ass(
    tts: TtsResult,
    display_duration: float,
    output_path: Path,
    *,
    font_path: Path = HIGHLIGHT_FONT_PATH,
) -> Path:
    """Build ASS timed to TTS; word boundaries if present else 6–8 word lines."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_name = HIGHLIGHT_FONT_NAME

    events: List[tuple[float, float, str]] = []
    spoken = tts.spoken_text.strip()

    word_bounds = [b for b in tts.boundaries if b.get("type") == "WordBoundary"]
    if word_bounds:
        for wb in word_bounds:
            start = _ticks_to_seconds(int(wb["offset"]))
            end = _ticks_to_seconds(int(wb["offset"]) + int(wb["duration"]))
            text = str(wb.get("text", "")).strip()
            if text:
                events.append((start, end, text))
    else:
        words = spoken.split()
        lines = _split_words_into_lines(words, HIGHLIGHT_CAPTION_WORDS_PER_LINE)
        if not lines:
            lines = [spoken]
        speech_end = min(tts.duration_seconds, display_duration)
        slot = speech_end / len(lines) if lines else speech_end
        for i, line in enumerate(lines):
            start = i * slot
            end = (i + 1) * slot if i < len(lines) - 1 else speech_end
            events.append((start, end, line))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {HIGHLIGHT_OUTPUT_WIDTH}
PlayResY: {HIGHLIGHT_OUTPUT_HEIGHT}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},42,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines_out = [header]
    for start, end, text in events:
        end = min(end, display_duration)
        if end <= start:
            end = min(display_duration, start + 0.05)
        lines_out.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{_escape_ass_text(text)}"
        )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))
    return output_path
