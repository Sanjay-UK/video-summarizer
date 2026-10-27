"""Regression tests for highlight narration TTS: edge-tts primary, gTTS fallback.

The primary (edge-tts) error must never be hidden behind the fallback's error. No network is used:
edge-tts and gTTS are replaced with stubs that fail or succeed on demand.
"""

import asyncio
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "")

from src import highlight_tts  # noqa: E402
from src.highlight_tts import TtsResult, generate_scene_narration  # noqa: E402


def _failing_edge(*_args, **_kwargs):
    async def boom(*_a, **_k):
        raise ConnectionError("edge-tts websocket closed by remote host")

    return boom()


def _fake_gtts_result(output_path: Path) -> TtsResult:
    return TtsResult(
        audio_path=output_path,
        spoken_text="hello",
        duration_seconds=1.0,
        boundary_kind="none",
        boundaries=[],
        voice_used="gtts:en",
    )


class HighlightNarrationFallbackTests(unittest.TestCase):
    def _run(self, *, edge_ok: bool, gtts_side_effect=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "scene.mp3"

        async def edge_success(*_a, **_k):
            return TtsResult(
                audio_path=out,
                spoken_text="hello",
                duration_seconds=1.0,
                boundary_kind="none",
                boundaries=[],
                voice_used="edge:en",
            )

        async def edge_failure(*_a, **_k):
            raise ConnectionError("edge-tts websocket closed by remote host")

        edge = edge_success if edge_ok else edge_failure
        gtts = mock.Mock(side_effect=gtts_side_effect or (lambda text, output_path, *, gtts_lang: _fake_gtts_result(output_path)))
        stderr = io.StringIO()
        with mock.patch.object(highlight_tts, "_synthesize_async", side_effect=edge), mock.patch.object(
            highlight_tts, "_gtts_fallback", gtts
        ), redirect_stderr(stderr):
            try:
                result = generate_scene_narration("hello", out)
                error = None
            except Exception as exc:  # noqa: BLE001
                result, error = None, exc
        return result, error, gtts, stderr.getvalue()

    def test_edge_success_does_not_call_gtts(self):
        result, error, gtts, _ = self._run(edge_ok=True)
        self.assertIsNone(error)
        self.assertEqual(result.voice_used, "edge:en")
        self.assertFalse(gtts.called)

    def test_edge_failure_with_gtts_success_is_logged_with_edge_error(self):
        result, error, gtts, stderr = self._run(edge_ok=False)
        self.assertIsNone(error)
        self.assertEqual(result.voice_used, "gtts:en")
        self.assertTrue(gtts.called)
        self.assertIn("edge-tts failed (ConnectionError: edge-tts websocket closed", stderr)

    def test_both_failing_reports_both_causes(self):
        def gtts_missing(text, output_path, *, gtts_lang):
            raise ImportError("No module named 'gtts'")

        result, error, gtts, _ = self._run(edge_ok=False, gtts_side_effect=gtts_missing)
        self.assertIsNotNone(error, "the failure must be raised, not silently returned")
        message = str(error)
        self.assertIn("ConnectionError: edge-tts websocket closed", message)
        self.assertIn("ImportError: No module named 'gtts'", message)
        self.assertIsInstance(error.__cause__, ImportError)


@unittest.skipUnless(importlib.util.find_spec("gtts"), "gtts not installed in this environment")
class GttsDependencyTests(unittest.TestCase):
    def test_gtts_imports_and_constructs_offline(self):
        from gtts import gTTS

        tts = gTTS(text="Good morning.", lang="en")
        self.assertEqual(tts.lang, "en")


if __name__ == "__main__":
    unittest.main()
