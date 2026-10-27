"""Regression tests: the RunPod /run size guard for highlight videos.

The guard must shrink the video when it can, and when it cannot, fail with a clear message that does not
suggest a local render. ffmpeg and the payload size are replaced by stubs, so nothing is encoded.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import runpod_serverless_payload as payload  # noqa: E402

SEGMENTS = "[]"


class VideoSizeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "source.mp4"
        self.video.write_bytes(b"x" * 10)

    def _run(self, sizes, reencode_side_effect=None):
        """sizes: payload estimates returned in order; each re-encode attempt consumes one."""
        estimates = iter(sizes)
        with mock.patch.object(payload, "estimate_highlight_phase_payload_bytes", side_effect=lambda **_k: next(estimates)), mock.patch.object(
            payload, "_reencode_video_for_runpod", side_effect=reencode_side_effect or (lambda src, dst, **_k: dst.write_bytes(b"small"))
        ):
            return payload.ensure_video_fits_runpod_payload(
                self.video, "job1", segments_json=SEGMENTS, languages="en"
            )

    def test_video_that_already_fits_is_used_unchanged(self):
        result = self._run([payload.SAFE_PAYLOAD_BYTES - 1])
        self.assertEqual(result, self.video)

    def test_oversized_video_is_shrunk_and_the_smaller_copy_is_used(self):
        mb = 1024 * 1024
        # Original too big; first re-encode still too big; second fits.
        result = self._run([12 * mb, 11 * mb, 8 * mb])
        self.assertEqual(result.name, "source_runpod.mp4")

    def test_video_that_cannot_shrink_fails_with_clear_message_and_no_local_render(self):
        mb = 1024 * 1024
        sizes = [20 * mb] + [20 * mb] * 12  # 1 original + 12 re-encode attempts (3 heights x 4 crf)
        with self.assertRaises(ValueError) as ctx:
            self._run(sizes)
        message = str(ctx.exception)
        self.assertIn("RunPod /run", message)
        self.assertIn("no local rendering was done", message)
        self.assertNotIn("run locally", message.lower())


if __name__ == "__main__":
    unittest.main()
