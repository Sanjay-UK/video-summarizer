"""Keeps the summary steps from failing on text the model refuses or cannot use.

Three layers, applied in order:
1. Retry a refused piece of text, skipping only the sentences that cause the refusal. Sentences are found by
   halving, so a few bad sentences cost a few extra calls, not one call per sentence.
2. If the model still gives nothing usable, build the output from the text itself (extractive: a spread of
   sentences from the transcript).
3. Every skip and every fallback is counted, so the results page can say what happened.

This module never calls the model. The caller passes an `attempt` function that returns a result, or None when the
model refused or gave nothing usable. Errors from the model (connection, timeout) are not handled here: the caller
decides what to do with them.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Common ways a model says it will not do something. Checked at the start of the reply only.
_REFUSAL = re.compile(
    r"^\W*("
    r"i\s+(can(?:'|’)?t|cannot|can\s+not|am\s+unable|won(?:'|’)t|will\s+not)\b"
    r"|i(?:'|’)m\s+(sorry|unable|not\s+able)"
    r"|sorry,?\s+i\b"
    r"|as\s+an\s+ai\b"
    r")",
    re.IGNORECASE,
)
# Sentence ends for Latin, Devanagari (danda) and Kannada/Telugu-style full stops.
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")

DEFAULT_MAX_CALLS = 40


def is_refusal(reply: Optional[str]) -> bool:
    """True when a model reply is empty or is a refusal rather than an answer."""
    text = (reply or "").strip()
    return not text or bool(_REFUSAL.match(text[:200]))


def split_sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    return parts or [text]


def resolve_with_skips(
    text: str,
    attempt: Callable[[str], Optional[Any]],
    *,
    max_calls: int = DEFAULT_MAX_CALLS,
) -> tuple[Optional[Any], list[str]]:
    """Run `attempt` on the text. If it is refused, retry with only the refusing sentences removed.

    Returns (result, skipped_sentences). `result` is None when no usable result was found within the call budget;
    the caller then falls back to extractive output for this piece.
    """
    budget = [max_calls]

    def ask(piece: str) -> Optional[Any]:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return attempt(piece)

    first = ask(text)
    if first is not None:
        return first, []

    sentences = split_sentences(text)
    if len(sentences) <= 1:
        # The only sentence is refused, so it is skipped. Nothing else is left to summarize.
        return None, sentences

    kept, skipped = _keep_accepted(sentences, ask)
    if not kept:
        return None, skipped
    return ask(" ".join(kept)), skipped


def _keep_accepted(sentences: list[str], ask: Callable[[str], Optional[Any]]) -> tuple[list[str], list[str]]:
    """Split the sentences into those the model accepts and those it refuses, by halving."""
    if not sentences:
        return [], []
    if ask(" ".join(sentences)) is not None:
        return list(sentences), []
    if len(sentences) == 1:
        return [], list(sentences)
    middle = len(sentences) // 2
    left_kept, left_skipped = _keep_accepted(sentences[:middle], ask)
    right_kept, right_skipped = _keep_accepted(sentences[middle:], ask)
    return left_kept + right_kept, left_skipped + right_skipped


def extractive_sentences(text: str, target_chars: int) -> list[str]:
    """A spread of sentences from the text, in order, up to about target_chars. Needs no model."""
    sentences = split_sentences(text)
    if not sentences:
        return []
    if sum(len(s) for s in sentences) <= target_chars:
        return sentences
    average = max(1, sum(len(s) for s in sentences) // len(sentences))
    count = max(1, min(len(sentences), target_chars // average))
    indexes = sorted({int(i * len(sentences) / count) for i in range(count)})
    return [sentences[i] for i in indexes]


def extractive_text(text: str, target_chars: int) -> str:
    return " ".join(extractive_sentences(text, target_chars))
