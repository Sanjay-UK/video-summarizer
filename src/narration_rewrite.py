"""Rewrite topic summaries into natural spoken narration."""

from __future__ import annotations
from src.log_safety import safe_exc

import re
import sys
from typing import Optional

from config import HIGHLIGHT_NARRATION_MAX_WORDS, OLLAMA_HOST, SUMMARIZATION_MODEL
from src.highlight_llm import highlight_ollama_options

try:
    import ollama
except ImportError:
    ollama = None  # type: ignore


def max_narration_words(compression: str) -> int:
    return int(HIGHLIGHT_NARRATION_MAX_WORDS.get(compression, HIGHLIGHT_NARRATION_MAX_WORDS["medium"]))


def count_words(text: str) -> int:
    return len(text.split())


def truncate_at_sentence_boundary(text: str, max_words: int) -> str:
    """Keep full sentences only until word budget is reached."""
    text = text.strip()
    if count_words(text) <= max_words:
        return text

    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept: list[str] = []
    total = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        words = count_words(sentence)
        if kept and total + words > max_words:
            break
        kept.append(sentence)
        total += words
        if total >= max_words:
            break

    if kept:
        return " ".join(kept)

    return " ".join(text.split()[:max_words])


def _call_ollama(prompt: str, model: str) -> str:
    client = ollama.Client(host=OLLAMA_HOST)
    response = client.generate(
        model=model,
        prompt=prompt,
        stream=False,
        options=highlight_ollama_options(num_ctx=4096),
    )
    return response.get("response", "").strip()


def _build_rewrite_prompt(
    summary_text: str,
    topic_title: str,
    max_words: int,
    *,
    shorten: bool = False,
    current_text: str = "",
) -> str:
    title_line = f"Topic: {topic_title}\n" if topic_title else ""
    if shorten:
        return f"""Shorten the spoken narration below to at most {max_words} words.
Rules:
- Plain sentences only (no bullets or markdown).
- Keep the most important facts; remove repetition.
- Aim for 2-3 short sentences.

{title_line}NARRATION TO SHORTEN:
{current_text.strip()}

SHORTENED NARRATION (max {max_words} words):
"""

    return f"""Rewrite the following educational summary as natural spoken narration.
Rules:
- Plain sentences only (no bullets, markdown, or labels).
- Keep the same facts; do not add new information.
- Use 2-3 short sentences, conversational tone for a student.
- Maximum {max_words} words total.

{title_line}SUMMARY:
{summary_text.strip()}

SPOKEN NARRATION (max {max_words} words):
"""


def _enforce_word_limit(text: str, max_words: int, model: str, topic_title: str) -> str:
    if count_words(text) <= max_words:
        return text

    print(
        f"[INFO] Narration over limit ({count_words(text)} > {max_words}); re-prompting to shorten",
        file=sys.stderr,
    )
    prompt = _build_rewrite_prompt("", topic_title, max_words, shorten=True, current_text=text)
    try:
        shortened = _call_ollama(prompt, model)
        if shortened and count_words(shortened) <= max_words:
            return shortened
        if shortened:
            text = shortened
    except Exception as exc:
        print(f"[WARN] Shorten re-prompt failed: {safe_exc(exc)}", file=sys.stderr)

    truncated = truncate_at_sentence_boundary(text, max_words)
    if count_words(truncated) > max_words:
        truncated = " ".join(truncated.split()[:max_words])
    print(
        f"[INFO] Narration truncated to {count_words(truncated)} words (limit {max_words})",
        file=sys.stderr,
    )
    return truncated


def rewrite_for_speech(
    summary_text: str,
    topic_title: str = "",
    *,
    model: Optional[str] = None,
    compression: str = "medium",
) -> str:
    """Light Ollama rewrite for TTS with compression-scoped word limit."""
    if model is None:
        model = SUMMARIZATION_MODEL
    max_words = max_narration_words(compression)

    if ollama is None:
        return truncate_at_sentence_boundary(summary_text.strip(), max_words)

    prompt = _build_rewrite_prompt(summary_text, topic_title, max_words)
    try:
        text = _call_ollama(prompt, model)
    except Exception as exc:
        print(f"[WARN] Narration rewrite failed: {safe_exc(exc)}", file=sys.stderr)
        text = ""

    if not text:
        print("[WARN] Empty narration rewrite; using trimmed summary", file=sys.stderr)
        text = summary_text.strip()

    return _enforce_word_limit(text, max_words, model, topic_title)
