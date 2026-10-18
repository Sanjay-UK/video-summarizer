"""Chunk Whisper segments for highlight LLM passes (never mid-segment)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Sequence

from config import HIGHLIGHT_CHUNK_MAX_CHARS


@dataclass(frozen=True)
class SegmentChunk:
    """Contiguous run of Whisper segments sent to one LLM call."""

    chunk_index: int
    global_segment_start: int
    global_segment_end: int  # inclusive
    time_start: float
    time_end: float
    llm_text: str


def format_segment_for_llm(segment_id: int, text: str) -> str:
    return f"[{segment_id}] {text.strip()}"


def chunk_whisper_segments(
    segments: Sequence[dict[str, Any]],
    max_chars: int = HIGHLIGHT_CHUNK_MAX_CHARS,
) -> List[SegmentChunk]:
    """Group segments into chunks without splitting any segment."""
    if not segments:
        return []

    chunks: List[SegmentChunk] = []
    chunk_index = 0
    start_idx = 0
    current_lines: List[str] = []
    current_chars = 0

    def flush(end_idx: int) -> None:
        nonlocal chunk_index, start_idx, current_lines, current_chars
        if not current_lines:
            return
        chunks.append(
            SegmentChunk(
                chunk_index=chunk_index,
                global_segment_start=start_idx,
                global_segment_end=end_idx,
                time_start=float(segments[start_idx].get("start", 0)),
                time_end=float(segments[end_idx].get("end", 0)),
                llm_text="\n".join(current_lines),
            )
        )
        chunk_index += 1
        start_idx = end_idx + 1
        current_lines = []
        current_chars = 0

    for i, seg in enumerate(segments):
        line = format_segment_for_llm(i, seg.get("text", ""))
        line_len = len(line) + 1

        if current_lines and current_chars + line_len > max_chars:
            flush(i - 1)

        if not current_lines:
            start_idx = i

        current_lines.append(line)
        current_chars += line_len

    if current_lines:
        flush(len(segments) - 1)

    return chunks
