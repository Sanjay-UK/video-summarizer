"""Summarize transcripts using Ollama/Llama with intelligent prompt engineering."""

import json
import re
import sys
from pathlib import Path
from typing import Union

from config import (
    OUTPUT_DIR,
    OLLAMA_HOST,
    SUMMARIZATION_MODEL,
    SUMMARY_TEMPERATURE,
    SUMMARY_MAX_TOKENS,
    SUMMARY_RATIO,
)

try:
    import ollama
except ImportError:
    print("[ERROR] ollama Python client not installed. Run: pip install ollama")
    sys.exit(1)


def test_ollama():
    """Test Ollama connection and model availability."""
    try:
        print("[INFO] Testing Ollama connection...")
        print(f"[INFO] Host: {OLLAMA_HOST}")
        
        client = ollama.Client(host=OLLAMA_HOST)
        models = client.list()
        
        if not models:
            print("[ERROR] No models available. Run: ollama pull llama2")
            return False
        
        print(f"[INFO] Available models:")
        for model in models.get("models", []):
            print(f"       - {model.get('name')}")
        
        return True
    except Exception as e:
        print(f"[ERROR] Ollama connection failed: {e}")
        print("[ERROR] Make sure Ollama is running: ollama serve")
        return False


def load_transcript(transcript_path: Union[Path, str]) -> str:
    """Load transcript from file."""
    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        raise FileNotFoundError(f"Transcript not found: {transcript_path}")
    
    with open(transcript_path, "r", encoding="utf-8") as f:
        return f.read()


def load_segments(segments_path: Union[Path, str]):
    """Load segments with timestamps from JSON."""
    segments_path = Path(segments_path)
    if not segments_path.exists():
        raise FileNotFoundError(f"Segments file not found: {segments_path}")
    
    with open(segments_path, "r", encoding="utf-8") as f:
        return json.load(f)


def chunk_text(text: str, max_tokens: int = 2500):
    """Split text into chunks suitable for LLM processing.

    Splits on sentence boundaries so it works correctly on real transcripts,
    which are typically one continuous block of text with no blank lines
    (paragraph-based splitting silently fails to chunk such text at all).
    """
    import re

    max_chars = max_tokens * 4
    chunks = []

    # Split on sentence boundaries: '.', '!', '?' followed by whitespace
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())

    current_chunk = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current_chunk) + len(sentence) + 1 <= max_chars:
            current_chunk += (" " if current_chunk else "") + sentence
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            # A single sentence longer than max_chars is kept whole rather than dropped
            current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text]


_PRIORITY_TAG_RE = re.compile(r"\[(CRITICAL|IMPORTANT|SUPPORTING)\]", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+")


def parse_ranked_concepts(raw: str) -> list:
    """Parse the model's ranked-concept output into (priority, text) pairs.

    Accepts the formats the model actually produces:
      [CRITICAL] Concept: explanation             (tag inline, the prompt's format)
      * [IMPORTANT] Concept: explanation          (bullet before the tag)
      **[SUPPORTING]** Concept: explanation       (bold tag)
      [CRITICAL]                                  (tag on its own line)
      * Concept: explanation                      (bullets under that tag)

    Untagged prose (preambles, "Extracted Concepts:" headers) is ignored. Only bullet lines
    inherit a priority from a tag-only header, so ordinary sentences are never treated as concepts.
    """
    concepts = []
    section_priority = None
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        is_bullet = _BULLET_PREFIX_RE.match(stripped) is not None
        line = _BULLET_PREFIX_RE.sub("", stripped, count=1).replace("**", "").replace("__", "").strip()
        if not line:
            continue

        match = _PRIORITY_TAG_RE.search(line)
        if match:
            priority = match.group(1).upper()
            before, after = line[:match.start()].strip(), line[match.end():].strip()
            text = (before + " " + after).strip() if before else after
            text = text.strip(" :-–—").strip()
            section_priority = priority
            if text:
                concepts.append((priority, text))
            continue

        if is_bullet and section_priority:
            text = line.strip(" :-–—").strip()
            if text:
                concepts.append((section_priority, text))
    return concepts


def extract_and_rank_concepts(transcript: str, model: str = None, chunk_index: int = 0) -> str:
    """Extract and rank all concepts by importance using two-pass approach.

    Pass 1: Identify all unique concepts/topics and rank them by importance.

    Args:
        chunk_index: Index of the source chunk this transcript came from. Tagged onto
            each returned concept line so concepts from multiple chunks can be safely
            aggregated, globally selected, then routed back to their originating chunk.

    Returns:
        Structured list of concepts with importance ranking and chunk tag
        Format: [CRITICAL][chunk:0] Concept 1: explanation
                [IMPORTANT][chunk:0] Concept 2: explanation
                [SUPPORTING][chunk:0] Concept 3: explanation
    """
    if model is None:
        model = SUMMARIZATION_MODEL

    try:
        print(f"[INFO] Extracting and ranking concepts with {model}...")

        client = ollama.Client(host=OLLAMA_HOST)

        prompt = f"""You are an expert at identifying educational concepts.

TASK 1 - EXTRACT ALL CONCEPTS:
1. Identify every distinct concept, topic, or learning objective in this transcript
2. List each as a bullet point
3. Include even minor/supporting concepts
4. Only use information present in the transcript. Do not invent names, tools, or topics
   that are not explicitly mentioned.

TASK 2 - RANK BY IMPORTANCE:
For each concept, assign a priority level based on educational value:
- [CRITICAL]: Essential for understanding the main topic
- [IMPORTANT]: Supports learning but not absolutely essential
- [SUPPORTING]: Adds depth, examples, or context

Output format (CRITICAL concepts first, then IMPORTANT, then SUPPORTING):
[CRITICAL] Concept 1: Brief explanation
[CRITICAL] Concept 2: Brief explanation
[IMPORTANT] Concept 3: Brief explanation
[SUPPORTING] Concept 4: Brief explanation

TRANSCRIPT:
{transcript}

RANKED CONCEPTS:
"""

        # The model sometimes ignores the format (or claims the transcript is missing). Retry once with
        # an explicit format reminder; if that also yields nothing, fail instead of returning empty.
        format_reminder = (
            "\n\nYour previous answer had no usable ranked concepts. Reply ONLY with lines that start with "
            "[CRITICAL], [IMPORTANT], or [SUPPORTING], followed by the concept. The transcript is above."
        )
        ranked = []
        concepts = ""
        for attempt_prompt in (prompt, prompt + format_reminder):
            response = client.generate(
                model=model,
                prompt=attempt_prompt,
                stream=False,
                options={
                    "temperature": SUMMARY_TEMPERATURE,
                    "top_p": 0.9,
                    "num_ctx": 8192,
                },
            )

            concepts = response.get("response", "").strip()

            if not concepts:
                raise RuntimeError("Empty response from Ollama")

            ranked = parse_ranked_concepts(concepts)
            if ranked:
                break
            print(f"[WARN] No ranked concepts in attempt; retrying once with format reminder")

        if not ranked:
            # Fail loudly: an unparsed response must not turn into an empty summary.
            raise RuntimeError(f"No ranked concepts found in model output: {concepts[:200]!r}")

        # Tag each concept line with its source chunk index so concepts from multiple
        # chunks can be aggregated, globally selected, then split back per chunk.
        # Output format: "[CRITICAL][chunk:0] Concept: explanation"
        return '\n'.join(f"[{priority}][chunk:{chunk_index}] {text}" for priority, text in ranked)

    except Exception as e:
        print(f"[ERROR] Concept extraction failed: {e}")
        raise


def select_concepts_by_compression(concepts_text: str, compression: str = "medium") -> str:
    """Select concepts based on compression level.

    Compression thresholds:
    - high: Keep top 20% of concepts (critical focus)
    - medium: Keep top 60% of concepts (balanced)
    - low: Keep 100% of concepts (comprehensive)

    Returns:
        Filtered list of selected concepts
    """
    lines = concepts_text.split('\n')
    lines = [l.strip() for l in lines if l.strip() and l.strip().startswith('[')]

    # Count concepts by priority
    critical = [l for l in lines if '[CRITICAL]' in l]
    important = [l for l in lines if '[IMPORTANT]' in l]
    supporting = [l for l in lines if '[SUPPORTING]' in l]

    total_concepts = len(critical) + len(important) + len(supporting)

    # Compression thresholds
    thresholds = {
        "high": 0.20,      # Keep top 20%
        "medium": 0.60,    # Keep top 60%
        "low": 1.0         # Keep 100%
    }

    keep_percentage = thresholds.get(compression, 0.60)
    num_to_keep = max(1, int(total_concepts * keep_percentage))

    # Always keep all CRITICAL concepts
    selected = critical.copy()
    remaining_slots = num_to_keep - len(selected)

    # Add IMPORTANT concepts if there's room
    if remaining_slots > 0:
        selected.extend(important[:remaining_slots])
        remaining_slots = num_to_keep - len(selected)

    # Add SUPPORTING concepts if there's room
    if remaining_slots > 0:
        selected.extend(supporting[:remaining_slots])

    selected_text = '\n'.join(selected)

    print(f"[INFO] Compression: {compression} | Selected {len(selected)}/{total_concepts} concepts")

    return selected_text


def split_concepts_by_chunk(selected_concepts: str, num_chunks: int) -> list:
    """Split a globally-selected, chunk-tagged concept list back per source chunk.

    Args:
        selected_concepts: Output of select_concepts_by_compression(), where each line
            is tagged with its source chunk, e.g. "[CRITICAL][chunk:0] Concept: ...".
        num_chunks: Total number of chunks the transcript was split into.

    Returns:
        List of length num_chunks, where entry i is the (untagged) concept text
        belonging to chunk i, or an empty string if no concepts from that chunk survived
        the global selection.
    """
    import re

    per_chunk = ["" for _ in range(num_chunks)]
    buckets = [[] for _ in range(num_chunks)]

    for line in selected_concepts.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        match = re.search(r'\[chunk:(\d+)\]', stripped)
        chunk_idx = int(match.group(1)) if match else 0
        chunk_idx = min(chunk_idx, num_chunks - 1)
        # Remove the chunk tag before handing back to the write pass
        clean_line = re.sub(r'\[chunk:\d+\]', '', stripped)
        buckets[chunk_idx].append(clean_line)

    for i in range(num_chunks):
        per_chunk[i] = '\n'.join(buckets[i])

    return per_chunk


def create_summary_prompt(transcript: str, selected_concepts: str = None, chunk_num: int = 1, total_chunks: int = 1, compression: str = "medium") -> str:
    """Create educational summarization prompt with compression level.

    When selected_concepts are provided, focuses summary on those concepts only.

    Args:
        selected_concepts: Pre-selected ranked concepts (from extract_and_rank_concepts)
        compression: "high", "medium", or "low" compression level
    """

    # If selected concepts provided, use two-pass approach
    if selected_concepts:
        if total_chunks > 1:
            chunk_note = f" (Part {chunk_num}/{total_chunks})"
        else:
            chunk_note = ""

        return f"""You are an expert educational summarizer using selected concepts.

COMPRESSION MODE: {compression.upper()}

SELECTED CONCEPTS TO COVER (listed by priority):
{selected_concepts}

YOUR TASK:
Write a comprehensive summary covering ONLY the concepts listed above.

CRITICAL REQUIREMENTS:
1. Include FULL explanations for each selected concept
2. Include relevant examples for each concept (do NOT skip examples)
3. DO NOT add extra concepts beyond the selected list
4. Maintain depth and clarity for each concept
5. Organize hierarchically: CRITICAL concepts first, then IMPORTANT, then SUPPORTING
6. Use clear bullet points and structured format

TRANSCRIPT{chunk_note}:
{transcript}

---

SUMMARY (Cover only selected concepts with full depth):
"""

    # Fall back to original word-count based approach if no selected concepts
    # Estimate transcript length to set word targets
    transcript_words = len(transcript.split())

    # Set compression target and guidelines based on level
    if compression == "high":
        # 50% compression = keep 50% of content, remove 50%
        target_words = max(100, transcript_words // 2)
        depth_guidance = f"""ABSOLUTE PRIORITY: Create a SHORT summary (target: ~{target_words} words maximum).

1. List ONLY the top 3-5 most critical concepts
2. Use 1-2 sentence bullet points ONLY
3. REMOVE all examples, case studies, and elaborations
4. REMOVE detailed explanations and context
5. REMOVE any non-essential details or supporting information
6. DO NOT include methods, processes, or "how-to" details unless absolutely critical
7. Focus on WHAT was discussed, not HOW it was discussed

Example format:
- Concept 1: One sentence definition
- Concept 2: One sentence definition
- Concept 3: One sentence definition"""
    elif compression == "low":
        # 85% compression = keep 85% of content, remove 15%
        target_words = max(300, int(transcript_words * 0.85))
        depth_guidance = f"""ABSOLUTE PRIORITY: Create a COMPREHENSIVE summary (target: ~{target_words} words).

1. Include ALL key concepts and learning objectives
2. Provide detailed explanations for each concept
3. Include relevant examples that illustrate points
4. Explain the reasoning and context behind concepts
5. Preserve technical terminology and accuracy
6. Include supporting details that enhance understanding
7. Keep any methods, processes, or step-by-step explanations that are important
8. Organize hierarchically: main concepts first, then supporting details under each"""
    else:  # medium (default)
        # 70% compression = keep 70% of content, remove 30%
        target_words = max(200, int(transcript_words * 0.70))
        depth_guidance = f"""BALANCED approach (target: ~{target_words} words).

1. Include main concepts and key arguments (ESSENTIAL)
2. Include 1-2 examples per concept (IMPORTANT)
3. Skip lengthy elaborations and repetition (REMOVE)
4. Skip tangential stories or asides (REMOVE)
5. Preserve technical accuracy
6. Use structured format: concept followed by brief explanation and one example
7. Balance between depth and brevity"""

    if total_chunks > 1:
        chunk_note = f" (Part {chunk_num}/{total_chunks})"
    else:
        chunk_note = ""

    return f"""You are an expert educational content summarizer with strict compression targets.

COMPRESSION LEVEL: {compression.upper()}
TARGET LENGTH: Approximately {target_words} words (measured by word count, NOT concept count)
CURRENT TRANSCRIPT LENGTH: Approximately {transcript_words} words

{depth_guidance}

---

TRANSCRIPT{chunk_note}:
{transcript}

---

SUMMARY (Remember: target approximately {target_words} words):
"""


def summarize_with_ollama(text: str, model: str = None, compression: str = "medium", selected_concepts: str = None) -> str:
    """Summarize text using Ollama/Llama.

    Args:
        compression: "high", "medium", or "low" compression level
        selected_concepts: Pre-selected ranked concepts (from extract_and_rank_concepts)
    """

    if model is None:
        model = SUMMARIZATION_MODEL

    try:
        print(f"[INFO] Calling Ollama ({model})...")

        client = ollama.Client(host=OLLAMA_HOST)

        prompt = create_summary_prompt(text, selected_concepts=selected_concepts, compression=compression)

        response = client.generate(
            model=model,
            prompt=prompt,
            stream=False,
            options={
                "temperature": SUMMARY_TEMPERATURE,
                "top_p": 0.9,
                "num_ctx": 8192,
            },
        )

        summary = response.get("response", "").strip()

        if not summary:
            raise RuntimeError("Empty response from Ollama")

        return summary

    except Exception as e:
        print(f"[ERROR] Ollama summarization failed: {e}")
        raise


def link_summary_to_segments(summary: str, segments: list):
    """Link summary sentences back to video timestamps."""
    import re
    
    sentences = re.split(r'(?<=[.!?])\s+', summary)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    linked = []
    
    for sentence in sentences:
        best_match = None
        best_score = 0
        best_start_idx = 0
        best_end_idx = 0
        
        sentence_lower = sentence.lower()
        
        for i, seg in enumerate(segments):
            seg_text = seg.get("text", "").lower()
            
            if len(sentence_lower) > 10:
                words = sentence_lower.split()[:5]
                phrase = " ".join(words)
                if phrase in seg_text:
                    if i < len(segments) - 1:
                        best_match = sentence
                        best_start_idx = i
                        best_end_idx = i
                        best_score = 0.9
                    break
            
            if not best_match:
                for j in range(i, min(i + 3, len(segments))):
                    if j == i:
                        best_match = sentence
                        best_start_idx = i
                        best_end_idx = min(i + 2, len(segments) - 1)
                        best_score = 0.5
        
        if best_match and best_start_idx < len(segments):
            start_time = segments[best_start_idx].get("start", 0)
            end_time = segments[best_end_idx].get("end", segments[-1].get("end", 0))
            
            linked.append({
                "text": best_match,
                "start": start_time,
                "end": end_time,
                "confidence": best_score,
            })
    
    return linked


def summarize_transcript(
    transcript_path: Union[Path, str] = None,
    segments_path: Union[Path, str] = None,
    output_dir: Union[Path, str] = None,
    model: str = None,
    compression: str = "medium",
):
    """Summarize a transcript using Ollama/Llama.

    Args:
        compression: "high" (quick summary), "medium" (balanced), "low" (detailed)
    """
    
    if transcript_path is None:
        transcript_path = OUTPUT_DIR / "transcript.txt"
    else:
        transcript_path = Path(transcript_path)
    
    if segments_path is None:
        segments_path = OUTPUT_DIR / "segments.json"
    else:
        segments_path = Path(segments_path)
    
    if output_dir is None:
        output_dir = OUTPUT_DIR
    else:
        output_dir = Path(output_dir)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if model is None:
        model = SUMMARIZATION_MODEL
    
    print(f"[INFO] Loading transcript: {transcript_path}")
    transcript = load_transcript(transcript_path)
    
    print(f"[INFO] Loading segments: {segments_path}")
    segments = load_segments(segments_path)
    
    print(f"[INFO] Transcript length: {len(transcript)} characters")
    
    chunks = chunk_text(transcript, max_tokens=2500)
    print(f"[INFO] Split into {len(chunks)} chunks")

    # PASS 1: Extract and rank concepts from EVERY chunk first, tagging each concept
    # with its source chunk so compression can be applied globally (not per-chunk),
    # avoiding uneven starvation of chunks with denser topic coverage.
    print(f"[INFO] Pass 1: Extracting concepts from all {len(chunks)} chunk(s)...")
    all_concepts = []
    for i, chunk in enumerate(chunks):
        print(f"[INFO]   Extracting concepts from chunk {i+1}/{len(chunks)}...")
        try:
            chunk_concepts = extract_and_rank_concepts(chunk, model=model, chunk_index=i)
            all_concepts.append(chunk_concepts)
        except Exception as e:
            print(f"[WARN] Concept extraction failed for chunk {i+1}: {e}")

    aggregated_concepts = '\n'.join(all_concepts)
    if not any(line.startswith('[') for line in aggregated_concepts.split('\n')):
        raise RuntimeError("Concept extraction produced no ranked concepts for any chunk; no summary written.")

    # PASS 2: Select concepts ONCE globally, based on compression level.
    print(f"[INFO] Pass 2: Selecting concepts globally ({compression} compression)...")
    selected_concepts_global = select_concepts_by_compression(aggregated_concepts, compression=compression)

    # Split the globally-selected concepts back out per originating chunk.
    selected_by_chunk = split_concepts_by_chunk(selected_concepts_global, len(chunks))

    # PASS 3: Write each chunk's summary using only its share of the globally selected concepts.
    print(f"[INFO] Pass 3: Writing summaries for {len(chunks)} chunk(s)...")
    summaries = []
    for i, chunk in enumerate(chunks):
        print(f"[INFO]   Summarizing chunk {i+1}/{len(chunks)}...")
        try:
            chunk_selected = selected_by_chunk[i]
            if not chunk_selected.strip():
                # No concepts from this chunk survived global selection; skip it
                # rather than summarizing without any concept guidance.
                print(f"[INFO]   Chunk {i+1} contributed no selected concepts, skipping")
                continue
            chunk_summary = summarize_with_ollama(
                chunk,
                model=model,
                compression=compression,
                selected_concepts=chunk_selected
            )
            summaries.append(chunk_summary)
        except Exception as e:
            print(f"[WARN] Chunk {i+1} summarization failed: {e}")

    # Concatenate the already concept-selected chunk summaries directly.
    # No second LLM re-summarization pass here — that previously re-compressed
    # already-selected content and silently dropped topics.
    full_summary = "\n\n".join(summaries)
    if not full_summary.strip():
        # Never write an empty summary.txt that the job runner would report as completed.
        raise RuntimeError("Summarization produced an empty summary; no outputs written.")

    print(f"[INFO] Linking summary to video timestamps...")
    linked_segments = link_summary_to_segments(full_summary, segments)
    
    original_len = len(transcript)
    summary_len = len(full_summary)
    ratio = summary_len / original_len if original_len > 0 else 0
    
    print(f"[INFO] Saving outputs...")
    
    summary_path = output_dir / "summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(full_summary)
    print(f"[DONE] Summary: {summary_path}")
    
    summary_segments_path = output_dir / "summary_segments.json"
    with open(summary_segments_path, "w", encoding="utf-8") as f:
        json.dump(linked_segments, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Summary segments: {summary_segments_path}")
    
    metadata_path = output_dir / "summary_metadata.json"
    metadata = {
        "original_length": original_len,
        "summary_length": summary_len,
        "compression_ratio": ratio,
        "target_ratio": SUMMARY_RATIO,
        "model": model,
        "temperature": SUMMARY_TEMPERATURE,
        "num_chunks": len(chunks),
        "num_segments": len(linked_segments),
    }
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"[DONE] Metadata: {metadata_path}")
    
    print()
    print(f"[SUMMARY]")
    print(f"  Original: {original_len} chars")
    print(f"  Summary: {summary_len} chars ({ratio:.1%})")
    print(f"  Video segments: {len(linked_segments)} with timestamps")
    
    return {
        "text": full_summary,
        "segments": linked_segments,
        "original_length": original_len,
        "summary_length": summary_len,
        "ratio": ratio,
    }


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Summarize transcript with Ollama/Llama")
    parser.add_argument("transcript_path", nargs="?", default=None, help="Path to transcript.txt")
    parser.add_argument("--segments", "-s", help="Path to segments.json")
    parser.add_argument("--output", "-o", help="Output directory")
    parser.add_argument("--model", "-m", help="Ollama model to use")
    parser.add_argument("--test", action="store_true", help="Test Ollama connection")
    
    args = parser.parse_args()
    
    if args.test:
        if test_ollama():
            print("\n✅ Ollama connection successful!")
        else:
            print("\n❌ Ollama connection failed!")
            sys.exit(1)
    else:
        try:
            result = summarize_transcript(
                transcript_path=args.transcript_path,
                segments_path=args.segments,
                output_dir=args.output,
                model=args.model,
            )
            print("\n✅ Summarization complete!")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
