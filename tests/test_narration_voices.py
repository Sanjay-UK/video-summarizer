"""Regression tests: highlight language status records which TTS provider and voice produced the narration."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "")

from src.highlight_pipeline import summarize_narration_voices  # noqa: E402


class NarrationVoiceSummaryTests(unittest.TestCase):
    def test_single_edge_voice_across_scenes(self):
        manifest = [{"tts_voice": "en-US-GuyNeural"}, {"tts_voice": "en-US-GuyNeural"}]
        self.assertEqual(summarize_narration_voices(manifest), ["en-US-GuyNeural"])

    def test_gtts_fallback_is_visible(self):
        manifest = [{"tts_voice": "gtts:en"}]
        self.assertEqual(summarize_narration_voices(manifest), ["gtts:en"])

    def test_mixed_providers_are_listed_once_each(self):
        manifest = [{"tts_voice": "gtts:en"}, {"tts_voice": "en-US-GuyNeural"}, {"tts_voice": "gtts:en"}]
        self.assertEqual(summarize_narration_voices(manifest), ["en-US-GuyNeural", "gtts:en"])

    def test_missing_voice_is_reported_as_unknown_not_dropped(self):
        self.assertEqual(summarize_narration_voices([{}]), ["unknown"])

    def test_no_scenes_gives_empty_list(self):
        self.assertEqual(summarize_narration_voices([]), [])


if __name__ == "__main__":
    unittest.main()
