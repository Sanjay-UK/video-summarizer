"""Regression tests for the FFmpeg libass 'subtitles' capability check (src/highlight_ffmpeg.py).

The check parses `ffmpeg -filters`. Flag columns differ by build: macOS ffmpeg-full prints ".." and
Ubuntu ffmpeg 4.4 prints "...". A check that only accepts ".." reports a false "missing libass" on the worker.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "")

from src import highlight_ffmpeg  # noqa: E402

FILTERS_UBUNTU_4_4 = """Filters:
  T.. = Timeline support
  ... ass              V->V       Render ASS subtitles onto input video using the libass library.
  ... subtitles        V->V       Render text subtitles onto input video using the libass library.
  ... scale            V->V       Scale the input video size and/or convert the image format.
"""

FILTERS_MAC_FFMPEG_FULL = """Filters:
  T.. = Timeline support
  .. ass               V->V       Render ASS subtitles onto input video using the libass library.
  .. subtitles         V->V       Render text subtitles onto input video using the libass library.
"""

FILTERS_WITHOUT_LIBASS = """Filters:
  ... scale            V->V       Scale the input video size and/or convert the image format.
  ... overlay          VV->V      Overlay a video source on top of the input.
"""


def _completed(stdout: str, returncode: int = 0):
    result = mock.Mock()
    result.stdout = stdout
    result.returncode = returncode
    return result


class SubtitlesFilterCheckTests(unittest.TestCase):
    def _check(self, stdout: str, returncode: int = 0) -> bool:
        with mock.patch.object(highlight_ffmpeg, "resolve_ffmpeg_bin", return_value="ffmpeg"), mock.patch(
            "src.highlight_ffmpeg.subprocess.run", return_value=_completed(stdout, returncode)
        ):
            return highlight_ffmpeg.ffmpeg_has_subtitles_filter()

    def test_ubuntu_three_character_flags_are_accepted(self):
        self.assertTrue(self._check(FILTERS_UBUNTU_4_4))

    def test_macos_two_character_flags_are_accepted(self):
        self.assertTrue(self._check(FILTERS_MAC_FFMPEG_FULL))

    def test_build_without_libass_is_rejected(self):
        self.assertFalse(self._check(FILTERS_WITHOUT_LIBASS))

    def test_ass_filter_alone_is_not_enough(self):
        self.assertFalse(self._check("  ... ass              V->V       Render ASS subtitles.\n"))

    def test_nonzero_exit_is_rejected(self):
        self.assertFalse(self._check(FILTERS_UBUNTU_4_4, returncode=1))

    def test_missing_binary_is_rejected(self):
        with mock.patch.object(highlight_ffmpeg, "resolve_ffmpeg_bin", return_value="/no/such/ffmpeg"), mock.patch(
            "src.highlight_ffmpeg.subprocess.run", side_effect=OSError("not found")
        ):
            self.assertFalse(highlight_ffmpeg.ffmpeg_has_subtitles_filter())


if __name__ == "__main__":
    unittest.main()
