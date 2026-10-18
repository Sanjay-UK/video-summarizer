"""Topic shifts for highlight video via Ollama JSON mode."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, List, Optional, Sequence

from config import (
    HIGHLIGHT_MIN_VALID_SCENES,
    HIGHLIGHT_TOPIC_MERGE_SEC,
    OLLAMA_HOST,
    SUMMARIZATION_MODEL,
)
from src.highlight_llm import highlight_ollama_options
from src.highlight_chunking import SegmentChunk, chunk_whisper_segments

try:
    import ollama
except ImportError:
    ollama = None  # type: ignore


@dataclass
class TopicScene:
    segment_id: int
    source_time: float
    topic_title: str
    summary_text: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["source_time"] = round(float(d["source_time"]), 3)
        return d


def segment_start_time(segments: Sequence[dict[str, Any]], segment_id: int) -> float:
    if segment_id < 0 or segment_id >= len(segments):
        raise IndexError(f"segment_id {segment_id} out of range 0..{len(segments) - 1}")
    return float(segments[segment_id].get("start", 0))


def _build_topic_prompt(chunk: SegmentChunk, rolling_context: str) -> str:
    context_block = ""
    if rolling_context.strip():
        context_block = (
            "TOPICS ALREADY COVERED IN PRIOR PARTS (do not repeat; continue the narrative):\n"
            f"{rolling_context.strip()}\n\n"
        )

    return f"""You identify natural topic shifts in an educational video transcript.

Rules:
- Output ONLY valid JSON (no markdown).
- List topics where the subject meaningfully changes — NOT every sentence.
- Each topic must reference the segment id [N] where that topic STARTS in this part.
- Use only segment ids that appear in TRANSCRIPT PART below.
- 1 to 8 topics for this part (fewer if the part is short).

JSON shape:
{{
  "topics": [
    {{
      "segment_id": 0,
      "topic_title": "short label",
      "summary_text": "1-3 sentences summarizing this topic for a student"
    }}
  ]
}}

{context_block}TRANSCRIPT PART (chunk {chunk.chunk_index + 1}, segments {chunk.global_segment_start}-{chunk.global_segment_end}):
{chunk.llm_text}
"""


def _call_ollama_json(prompt: str, model: str) -> dict[str, Any]:
    if ollama is None:
        raise RuntimeError("ollama package not installed")

    client = ollama.Client(host=OLLAMA_HOST)
    response = client.generate(
        model=model,
        prompt=prompt,
        stream=False,
        format="json",
        options=highlight_ollama_options(num_ctx=8192),
    )
    raw = response.get("response", "").strip()
    if not raw:
        raise ValueError("Empty JSON response from Ollama")
    return json.loads(raw)


def _validate_topic_segment_ids(
    payload: dict[str, Any],
    chunk: SegmentChunk,
) -> List[int]:
    """Return strictly increasing segment ids inside the chunk range."""
    topics_raw = payload.get("topics")
    if not isinstance(topics_raw, list):
        raise ValueError("JSON must contain a 'topics' array")

    lo, hi = chunk.global_segment_start, chunk.global_segment_end
    candidates: List[int] = []

    for item in topics_raw:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("segment_id")
        if raw_id is None:
            continue
        if isinstance(raw_id, float) and raw_id.is_integer():
            raw_id = int(raw_id)
        if isinstance(raw_id, float) and raw_id.is_integer():
            raw_id = int(raw_id)
        if not isinstance(raw_id, int):
            try:
                raw_id = int(raw_id)
            except (TypeError, ValueError):
                continue
        if raw_id < lo or raw_id > hi:
            continue
        candidates.append(raw_id)

    candidates.sort()
    validated: List[int] = []
    last = -1
    for seg_id in candidates:
        if seg_id <= last:
            continue
        validated.append(seg_id)
        last = seg_id
    return validated


def _parse_topics_payload(
    payload: dict[str, Any],
    chunk: SegmentChunk,
    segments: Sequence[dict[str, Any]],
) -> List[TopicScene]:
    topics_raw = payload.get("topics")
    if not isinstance(topics_raw, list):
        raise ValueError("JSON must contain a 'topics' array")

    valid_ids = set(_validate_topic_segment_ids(payload, chunk))
    if not valid_ids:
        raise ValueError("No valid strictly-increasing segment_ids in chunk range")

    by_id: dict[int, dict[str, Any]] = {}
    for item in topics_raw:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("segment_id")
        if raw_id is None:
            continue
        try:
            seg_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if seg_id not in valid_ids:
            continue
        if seg_id in by_id:
            continue
        by_id[seg_id] = item

    scenes: List[TopicScene] = []
    for seg_id in sorted(valid_ids):
        item = by_id.get(seg_id)
        if not item:
            continue
        title = str(item.get("topic_title", "")).strip() or f"Topic at segment {seg_id}"
        summary = str(item.get("summary_text", "")).strip()
        if not summary:
            continue
        scenes.append(
            TopicScene(
                segment_id=seg_id,
                source_time=segment_start_time(segments, seg_id),
                topic_title=title,
                summary_text=summary,
            )
        )

    if len(scenes) < 1:
        raise ValueError("Too few valid topics after segment_id validation")
    return scenes


def segment_topics_in_chunk(
    chunk: SegmentChunk,
    segments: Sequence[dict[str, Any]],
    *,
    rolling_context: str = "",
    model: Optional[str] = None,
) -> List[TopicScene]:
    """LLM topic shifts for one chunk; retries once on JSON/parse failure."""
    if model is None:
        model = SUMMARIZATION_MODEL

    prompt = _build_topic_prompt(chunk, rolling_context)
    last_error: Optional[Exception] = None

    for attempt in range(2):
        try:
            payload = _call_ollama_json(prompt, model)
            scenes = _parse_topics_payload(payload, chunk, segments)
            if scenes:
                return scenes
            raise ValueError("No valid topics in JSON")
        except Exception as exc:
            last_error = exc
            print(f"[WARN] Topic parse attempt {attempt + 1} failed: {exc}", file=sys.stderr)

    raise RuntimeError(f"Topic segmentation failed after retry: {last_error}")


def _rolling_context_from_scenes(scenes: Sequence[TopicScene], max_chars: int = 1500) -> str:
    lines = [f"- {s.topic_title}: {s.summary_text}" for s in scenes]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def merge_topics_within_gap(
    scenes: Sequence[TopicScene],
    segments: Sequence[dict[str, Any]],
    merge_within_sec: float = HIGHLIGHT_TOPIC_MERGE_SEC,
) -> List[TopicScene]:
    """Merge consecutive topics whose source times are closer than merge_within_sec."""
    if not scenes:
        return []

    ordered = sorted(scenes, key=lambda s: (s.source_time, s.segment_id))
    merged: List[TopicScene] = [ordered[0]]

    for scene in ordered[1:]:
        prev = merged[-1]
        if scene.source_time - prev.source_time < merge_within_sec:
            merged[-1] = TopicScene(
                segment_id=prev.segment_id,
                source_time=prev.source_time,
                topic_title=prev.topic_title,
                summary_text=f"{prev.summary_text} {scene.summary_text}".strip(),
            )
        else:
            merged.append(scene)

    return merged


def fallback_evenly_spaced_scenes(
    segments: Sequence[dict[str, Any]],
    *,
    target_count: Optional[int] = None,
) -> List[TopicScene]:
    """Evenly spaced scenes when LLM JSON fails."""
    if not segments:
        return []

    duration = float(segments[-1].get("end", 0))
    if duration <= 0:
        duration = float(segments[-1].get("start", 0))

    if target_count is None:
        target_count = max(4, min(12, int(duration / 60) + 1))

    scenes: List[TopicScene] = []
    for i in range(target_count):
        t = (i + 0.5) * duration / target_count
        seg_id = _nearest_segment_index(segments, t)
        scenes.append(
            TopicScene(
                segment_id=seg_id,
                source_time=segment_start_time(segments, seg_id),
                topic_title=f"Segment {i + 1}",
                summary_text="Summary unavailable; evenly spaced fallback scene.",
            )
        )
    return scenes


def _nearest_segment_index(segments: Sequence[dict[str, Any]], time_sec: float) -> int:
    best_idx = 0
    best_dist = float("inf")
    for i, seg in enumerate(segments):
        start = float(seg.get("start", 0))
        dist = abs(start - time_sec)
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    return best_idx


def run_topic_segmentation(
    segments: Sequence[dict[str, Any]],
    *,
    model: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> List[TopicScene]:
    """Full pass: chunk → sequential LLM topics → merge within 40s."""
    from config import HIGHLIGHT_CHUNK_MAX_CHARS

    chunk_size = max_chars if max_chars is not None else HIGHLIGHT_CHUNK_MAX_CHARS
    chunks = chunk_whisper_segments(segments, max_chars=chunk_size)
    if not chunks:
        return []

    all_scenes: List[TopicScene] = []
    rolling = ""

    try:
        for chunk in chunks:
            print(
                f"[INFO] Topic segmentation chunk {chunk.chunk_index + 1}/{len(chunks)} "
                f"(segments {chunk.global_segment_start}-{chunk.global_segment_end})"
            )
            chunk_scenes = segment_topics_in_chunk(
                chunk,
                segments,
                rolling_context=rolling,
                model=model,
            )
            all_scenes.extend(chunk_scenes)
            rolling = _rolling_context_from_scenes(all_scenes)
    except Exception as exc:
        print(f"[WARN] Topic segmentation fallback: {exc}", file=sys.stderr)
        return merge_topics_within_gap(
            fallback_evenly_spaced_scenes(segments),
            segments,
        )

    deduped: List[TopicScene] = []
    last_seg = -1
    for scene in sorted(all_scenes, key=lambda s: s.segment_id):
        if scene.segment_id <= last_seg:
            continue
        deduped.append(scene)
        last_seg = scene.segment_id

    if len(deduped) < HIGHLIGHT_MIN_VALID_SCENES:
        print(
            f"[WARN] Only {len(deduped)} valid topic scene(s); using evenly spaced fallback",
            file=sys.stderr,
        )
        return merge_topics_within_gap(
            fallback_evenly_spaced_scenes(segments),
            segments,
        )

    return merge_topics_within_gap(deduped, segments)


def write_highlight_scenes(path: Path, scenes: Sequence[TopicScene]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scenes": [s.to_dict() for s in scenes]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path
