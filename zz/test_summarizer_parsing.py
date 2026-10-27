"""Regression tests for concept parsing and empty-summary guards in src/summarizer.py.

Run from the project root with the validated environment:
  venv-mac/bin/python -m unittest discover -s tests -v

No network and no Ollama server are needed: the Ollama client is mocked.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "")

from src import summarizer  # noqa: E402
from src.summarizer import (  # noqa: E402
    extract_and_rank_concepts,
    parse_ranked_concepts,
    select_concepts_by_compression,
    summarize_transcript,
)

# The real output that the 30-second smoke clip produced: tags on their own lines, bullets under them.
REAL_SHORT_CLIP_OUTPUT = """I'll extract and rank the concepts from the transcript.

**Extracted Concepts:**

* Sushi
* Sushi rice
* Types of sushi (Nagiri, Hanroll, Maki)

**Ranked Concepts:**

[CRITICAL]
* Sushi making: The fundamental concept of the transcript.
* Types of sushi: Nagiri, Hanroll and Maki are the three popular types.

[IMPORTANT]
* Sushi rice: Starch content and stickiness matter for sushi.
"""

SHORT_TRANSCRIPT = (
    "Today, I'm going to show you how to make the three most popular types of sushi, "
    "Nagiri, Hanroll, and Maki. First you rinse the sushi rice, then cook it."
)


def _always(text):
    """A patcher for ollama.Client whose generate() gives the same reply every time."""
    client = mock.MagicMock()
    client.generate.return_value = {"response": text}
    return mock.patch("src.summarizer.ollama.Client", return_value=client), client


def _mock_client(*responses):
    """Return a patcher for ollama.Client whose generate() yields the given response texts in order."""
    client = mock.MagicMock()
    client.generate.side_effect = [{"response": text} for text in responses]
    return mock.patch("src.summarizer.ollama.Client", return_value=client), client


class ParseRankedConceptsTests(unittest.TestCase):
    def test_real_short_clip_output_is_parsed(self):
        pairs = parse_ranked_concepts(REAL_SHORT_CLIP_OUTPUT)
        self.assertEqual(
            [p for p, _ in pairs],
            ["CRITICAL", "CRITICAL", "IMPORTANT"],
        )
        self.assertTrue(pairs[0][1].startswith("Sushi making"))

    def test_bullet_before_tag(self):
        self.assertEqual(
            parse_ranked_concepts("* [IMPORTANT] Foo: bar baz"),
            [("IMPORTANT", "Foo: bar baz")],
        )

    def test_bold_tag(self):
        self.assertEqual(
            parse_ranked_concepts("**[SUPPORTING]** Baz: detail"),
            [("SUPPORTING", "Baz: detail")],
        )

    def test_normal_inline_format(self):
        raw = "[CRITICAL] A: a\n[IMPORTANT] B: b\n[SUPPORTING] C: c"
        self.assertEqual(
            parse_ranked_concepts(raw),
            [("CRITICAL", "A: a"), ("IMPORTANT", "B: b"), ("SUPPORTING", "C: c")],
        )

    def test_numbered_bullets_under_tag(self):
        raw = "[SUPPORTING]\n1. First point\n2) Second point"
        self.assertEqual(
            parse_ranked_concepts(raw),
            [("SUPPORTING", "First point"), ("SUPPORTING", "Second point")],
        )

    def test_preamble_and_untagged_bullets_are_ignored(self):
        raw = (
            "Here are the concepts:\n"
            "This sentence is prose, not a concept.\n"
            "* A bullet with no section tag yet\n"
            "**Extracted Concepts:**\n"
            "* Another untagged bullet\n"
        )
        self.assertEqual(parse_ranked_concepts(raw), [])

    def test_tag_only_header_applies_only_to_following_bullets(self):
        raw = "[CRITICAL]\n* Kept: yes\nSome prose line after the bullets.\n* Also kept: yes"
        self.assertEqual(
            parse_ranked_concepts(raw),
            [("CRITICAL", "Kept: yes"), ("CRITICAL", "Also kept: yes")],
        )

    def test_empty_input_returns_nothing(self):
        self.assertEqual(parse_ranked_concepts(""), [])
        self.assertEqual(parse_ranked_concepts("   \n\n"), [])


class ExtractAndRankConceptsTests(unittest.TestCase):
    def test_short_clip_output_yields_tagged_concepts(self):
        patcher, _ = _mock_client(REAL_SHORT_CLIP_OUTPUT)
        with patcher:
            out = extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b", chunk_index=2)
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("[CRITICAL][chunk:2] Sushi making"))
        self.assertTrue(all(line.startswith("[") for line in lines))

    def test_empty_model_response_falls_back_to_the_transcript_sentences(self):
        patcher, _ = _always("")
        with patcher:
            out = extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b", chunk_index=0)
        lines = out.split("\n")
        self.assertTrue(lines and all(line.startswith("[IMPORTANT][chunk:0] ") for line in lines))
        self.assertIn(lines[0].split("] ", 1)[1].split(".")[0], SHORT_TRANSCRIPT)

    def test_unparseable_model_response_falls_back_after_the_retry(self):
        patcher, client = _always("Still no tags here.")
        with patcher:
            out = extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b", chunk_index=1)
        self.assertTrue(out.startswith("[IMPORTANT][chunk:1] "))
        self.assertGreaterEqual(client.generate.call_count, 2, "the format-reminder retry must still happen")

    def test_retry_recovers_when_second_attempt_is_formatted(self):
        patcher, client = _mock_client("I'm ready. Please paste the transcript.", REAL_SHORT_CLIP_OUTPUT)
        with patcher:
            out = extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b", chunk_index=0)
        self.assertEqual(client.generate.call_count, 2)
        self.assertTrue(out.startswith("[CRITICAL][chunk:0]"))
        second_prompt = client.generate.call_args_list[1].kwargs["prompt"]
        self.assertIn("Reply ONLY with lines", second_prompt)

    def test_empty_response_is_not_retried_with_a_format_reminder(self):
        patcher, client = _always("")
        with patcher:
            extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b")
        prompts = [c.kwargs["prompt"] for c in client.generate.call_args_list]
        self.assertFalse(any("Reply ONLY with lines" in p for p in prompts))


class SelectionCompatibilityTests(unittest.TestCase):
    def test_parsed_short_clip_output_survives_compression(self):
        patcher, _ = _mock_client(REAL_SHORT_CLIP_OUTPUT)
        with patcher:
            tagged = extract_and_rank_concepts(SHORT_TRANSCRIPT, model="llama3.2:3b", chunk_index=0)
        selected = select_concepts_by_compression(tagged, compression="medium")
        self.assertTrue(selected.strip(), "medium compression must keep at least one concept")
        self.assertIn("[CRITICAL][chunk:0]", selected)


class SummarizeTranscriptGuardTests(unittest.TestCase):
    def _write_inputs(self, tmp: Path, text: str):
        transcript = tmp / "transcript.txt"
        segments = tmp / "segments.json"
        transcript.write_text(text, encoding="utf-8")
        segments.write_text(
            json.dumps([{"start": 0.0, "end": 8.0, "text": text}]), encoding="utf-8"
        )
        return transcript, segments

    def test_short_clip_produces_a_real_summary(self):
        summary_text = "Sushi comes in three types: nigiri, hand roll, and maki."
        patcher, client = _mock_client(REAL_SHORT_CLIP_OUTPUT, summary_text)
        with tempfile.TemporaryDirectory() as tmp_str, patcher:
            tmp = Path(tmp_str)
            transcript, segments = self._write_inputs(tmp, SHORT_TRANSCRIPT)
            summarize_transcript(
                transcript_path=transcript,
                segments_path=segments,
                output_dir=tmp,
                model="llama3.2:3b",
                compression="medium",
            )
            written = (tmp / "summary.txt").read_text(encoding="utf-8")
            meta = json.loads((tmp / "summary_metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(written.strip())
        self.assertGreater(meta["summary_length"], 0)

    def test_empty_model_output_still_writes_a_summary_from_the_transcript(self):
        patcher, _ = _always("")
        with tempfile.TemporaryDirectory() as tmp_str, patcher:
            tmp = Path(tmp_str)
            transcript, segments = self._write_inputs(tmp, SHORT_TRANSCRIPT)
            summarize_transcript(
                transcript_path=transcript,
                segments_path=segments,
                output_dir=tmp,
                model="llama3.2:3b",
                compression="medium",
            )
            written = (tmp / "summary.txt").read_text(encoding="utf-8")
            notes = json.loads((tmp / "summary_metadata.json").read_text(encoding="utf-8"))["summary_notes"]
        self.assertTrue(written.strip(), "the job must end with a summary")
        self.assertFalse(notes["ai_summary_complete"])
        self.assertGreaterEqual(notes["fallback_chunks"], 1)

    def test_refused_concepts_still_produce_a_summary_and_say_so(self):
        patcher, _ = _always("Sorry, I can't do that.")
        with tempfile.TemporaryDirectory() as tmp_str, patcher:
            tmp = Path(tmp_str)
            transcript, segments = self._write_inputs(tmp, SHORT_TRANSCRIPT)
            summarize_transcript(
                transcript_path=transcript,
                segments_path=segments,
                output_dir=tmp,
                model="llama3.2:3b",
                compression="medium",
            )
            written = (tmp / "summary.txt").read_text(encoding="utf-8")
            notes = json.loads((tmp / "summary_metadata.json").read_text(encoding="utf-8"))["summary_notes"]
        self.assertTrue(written.strip())
        self.assertNotIn("sorry", written.lower())
        self.assertFalse(notes["ai_summary_complete"])


if __name__ == "__main__":
    unittest.main()
