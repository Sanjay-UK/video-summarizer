"""The summary steps finish even when the model refuses text or fails, and they say what they skipped.

The model is a fake Ollama client: a sentence containing FORBIDDEN makes it refuse, and a client can be told to fail.
Nothing calls a real model.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import summarizer  # noqa: E402
from src import summary_guard as guard  # noqa: E402

REFUSAL = "I can't assist with that request."
TRANSCRIPT = (
    "Rice is rinsed first to remove starch. The grains stay separate after cooking. "
    "FORBIDDEN sentence that the model will refuse. Vinegar and sugar season the rice. "
    "Shape the rice while it is warm. Serve it with vegetables and enjoy."
)


class FakeOllama:
    """Replies like the model: refuses prompts with FORBIDDEN in them, otherwise gives concepts or a summary."""

    def __init__(self, fail_with=None, refuse_all=False):
        self.fail_with = fail_with
        self.refuse_all = refuse_all
        self.calls = 0

    def Client(self, host=None):  # noqa: N802 — mirrors ollama.Client
        return self

    def generate(self, model, prompt, stream=False, options=None):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.refuse_all or "FORBIDDEN" in prompt:
            return {"response": REFUSAL}
        if "RANKED CONCEPTS:" in prompt:
            return {"response": "[CRITICAL] Rice preparation: rinse and cook the rice\n[IMPORTANT] Seasoning: vinegar and sugar"}
        return {"response": "The lesson explains how to prepare vinegar rice."}


def _run_summary(fake: FakeOllama, transcript: str = TRANSCRIPT):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    transcript_path = root / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    segments_path = root / "segments.json"
    segments_path.write_text(json.dumps([{"start": 0.0, "end": 5.0, "text": transcript}]), encoding="utf-8")
    out = root / "out"
    with mock.patch.object(summarizer.ollama, "Client", fake.Client):
        summarizer.summarize_transcript(
            transcript_path=transcript_path, segments_path=segments_path, output_dir=out,
            model="llama3.2:3b", compression="medium",
        )
    metadata = json.loads((out / "summary_metadata.json").read_text(encoding="utf-8"))
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    tmp.cleanup()
    return summary, metadata


class ResolveWithSkipsTests(unittest.TestCase):
    def test_only_the_refused_sentence_is_skipped(self):
        def attempt(piece):
            return None if "bad" in piece else piece

        result, skipped = guard.resolve_with_skips("Good one. A bad sentence. Good two.", attempt)
        self.assertEqual(skipped, ["A bad sentence."])
        self.assertEqual(result, "Good one. Good two.")

    def test_everything_refused_gives_no_result_and_reports_every_sentence(self):
        result, skipped = guard.resolve_with_skips("One. Two. Three.", lambda piece: None)
        self.assertIsNone(result)
        self.assertEqual(skipped, ["One.", "Two.", "Three."])

    def test_the_call_budget_stops_endless_retries(self):
        calls = []

        def attempt(piece):
            calls.append(piece)
            return None

        guard.resolve_with_skips(" ".join(f"Sentence {i}." for i in range(200)), attempt, max_calls=12)
        self.assertLessEqual(len(calls), 12)

    def test_a_single_refused_sentence_is_skipped_not_retried(self):
        result, skipped = guard.resolve_with_skips("Just one sentence.", lambda piece: None)
        self.assertIsNone(result)
        self.assertEqual(skipped, ["Just one sentence."])

    def test_refusal_detection_covers_common_wording_and_empty_replies(self):
        for reply in (REFUSAL, "I cannot help with that.", "I'm sorry, but I can't.", "", "   "):
            self.assertTrue(guard.is_refusal(reply), reply)
        for reply in ("Rice is rinsed first.", "[CRITICAL] Rice: rinse it"):
            self.assertFalse(guard.is_refusal(reply), reply)

    def test_extractive_text_spreads_across_the_whole_text(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(40))
        picked = guard.extractive_sentences(text, target_chars=120)
        self.assertGreater(len(picked), 1)
        self.assertIn("Sentence number 0 is here.", picked)
        self.assertGreater(int(picked[-1].split()[2]), 20, "the picks must reach the end of the text")


class SummarizerResilienceTests(unittest.TestCase):
    def test_a_refused_sentence_is_skipped_and_the_rest_is_summarized(self):
        summary, metadata = _run_summary(FakeOllama())
        self.assertTrue(summary.strip())
        self.assertNotIn("FORBIDDEN", summary)
        notes = metadata["summary_notes"]
        self.assertGreaterEqual(notes["skipped_sentences"], 1)
        self.assertEqual(notes["fallback_chunks"], 0)
        self.assertTrue(notes["ai_summary_complete"])

    def test_when_the_model_refuses_everything_the_job_still_gets_a_summary(self):
        summary, metadata = _run_summary(FakeOllama(refuse_all=True))
        self.assertTrue(summary.strip(), "the job must not end without a summary")
        self.assertNotIn(REFUSAL, summary)
        notes = metadata["summary_notes"]
        self.assertGreaterEqual(notes["fallback_chunks"], 1)
        self.assertFalse(notes["ai_summary_complete"])

    def test_a_model_connection_error_still_gives_a_summary(self):
        summary, metadata = _run_summary(FakeOllama(fail_with=ConnectionError("ollama not running")))
        self.assertTrue(summary.strip())
        notes = metadata["summary_notes"]
        self.assertGreaterEqual(notes["llm_errors"], 1)
        self.assertFalse(notes["ai_summary_complete"])

    def test_a_transcript_with_no_speech_gets_a_clear_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            _run_summary(FakeOllama(), transcript="   ")
        self.assertIn("No speech was detected", str(ctx.exception))

    def test_unicode_transcripts_are_handled_without_refusal_loops(self):
        kannada = "ಒಂದು ಊರಿನಲ್ಲಿ ಒಬ್ಬ ರೈತ ವಾಸಿಸುತ್ತಿದ್ದನು. ಅವನ ಬಳಿ ಒಂದು ಕುರಿಮರಿ ಇತ್ತು."
        summary, _metadata = _run_summary(FakeOllama(), transcript=kannada)
        self.assertTrue(summary.strip())


if __name__ == "__main__":
    unittest.main()


class NarrationRefusalTests(unittest.TestCase):
    """A refused narration rewrite must never become the spoken text of a highlight."""

    def test_refused_rewrite_falls_back_to_the_trimmed_summary(self):
        from src import narration_rewrite as nr

        summary = "Rice is rinsed first to remove starch. Vinegar and sugar season the rice."
        with mock.patch.object(nr, "_call_ollama", return_value=REFUSAL), \
                mock.patch.object(nr, "ollama", object()):
            spoken = nr.rewrite_for_speech(summary, "Rice", compression="medium")
        self.assertNotIn("can't", spoken.lower())
        self.assertIn("Rice is rinsed first", spoken)

    def test_refused_shortening_does_not_replace_the_narration(self):
        from src import narration_rewrite as nr

        long_text = " ".join(f"Word{i}." for i in range(400))
        with mock.patch.object(nr, "_call_ollama", return_value=REFUSAL):
            result = nr._enforce_word_limit(long_text, 50, "m", "")
        self.assertNotIn("can't", result.lower())
        self.assertTrue(result.startswith("Word0."))


class TranscriberLanguageTests(unittest.TestCase):
    """Whisper must detect the language, and the transcript's reliability must be recorded."""

    def test_language_is_detected_not_forced_and_low_confidence_is_recorded(self):
        from src import transcriber

        seen = {}

        class Seg:
            def __init__(self, text, logprob):
                self.start, self.end, self.text, self.avg_logprob = 0.0, 2.0, text, logprob

        class Info:
            duration = 4.0
            language = "kn"
            language_probability = 0.97

        class FakeWhisper:
            def __init__(self, *_a, **_k):
                pass

            def transcribe(self, path, **kwargs):
                seen.update(kwargs)
                return iter([Seg("some speech", -1.8)]), Info()

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.mp3"
            audio.write_bytes(b"x")
            out = Path(tmp) / "out"
            with mock.patch.object(transcriber, "WhisperModel", FakeWhisper):
                transcriber.transcribe_audio(audio, model="base", output_dir=out, device="cpu")
            notes = json.loads((out / "transcript_notes.json").read_text(encoding="utf-8"))
        self.assertIsNone(seen.get("language"), "language must be detected, not forced to English")
        self.assertEqual(notes["language"], "kn")
        self.assertTrue(notes["low_confidence"])
