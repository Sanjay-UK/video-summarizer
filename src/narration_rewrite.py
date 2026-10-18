"""Rewrite topic summaries into natural spoken narration."""

from __future__ import annotations

import sys
from typing import Optional

from config import OLLAMA_HOST, SUMMARIZATION_MODEL
from src.highlight_llm import highlight_ollama_options

try:
    import ollama
except ImportError:
    ollama = None  # type: ignore


def rewrite_for_speech(summary_text: str, topic_title: str = "", *, model: Optional[str] = None) -> str:
    """Light Ollama rewrite for TTS (plain spoken English)."""
    if model is None:
        model = SUMMARIZATION_MODEL
    if ollama is None:
        return summary_text.strip()

    title_line = f"Topic: {topic_title}\n" if topic_title else ""
    prompt = f"""Rewrite the following educational summary as natural spoken narration.
Rules:
- Plain sentences only (no bullets, markdown, or labels).
- Keep the same facts; do not add new information.
- 2-4 short sentences, conversational tone for a student.

{title_line}SUMMARY:
{summary_text.strip()}

SPOKEN NARRATION:
"""

    client = ollama.Client(host=OLLAMA_HOST)
    response = client.generate(
        model=model,
        prompt=prompt,
        stream=False,
        options=highlight_ollama_options(num_ctx=4096),
    )
    text = response.get("response", "").strip()
    if not text:
        print("[WARN] Empty narration rewrite; using original summary", file=sys.stderr)
        return summary_text.strip()
    return text
