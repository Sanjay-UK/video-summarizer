"""English audio is generated once per highlight job, and the English download is the highlight's own narration.

Text-to-speech, FFmpeg and the Flask app are faked where needed, so nothing is synthesized and no paid service is called.
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

from src import highlight_assembler as ha  # noqa: E402
from src import highlight_pipeline as hp  # noqa: E402
from src import highlight_shared_video as shared  # noqa: E402
from src.highlight_tts import TtsResult  # noqa: E402
from src.topic_segmentation import TopicScene  # noqa: E402
from src.visual_extractor import VisualSelection  # noqa: E402

ENGLISH = "Welcome to the lesson. Today we cover loops."


def _tts(path: Path, text: str, seconds: float = 4.2) -> TtsResult:
    path.write_bytes(b"ID3-" + text.encode("utf-8"))
    return TtsResult(audio_path=path, spoken_text=text, duration_seconds=seconds, boundary_kind="none",
                     boundaries=[], voice_used="en-US-GuyNeural")


def _job_with_shared_scene(job_dir: Path, text: str = ENGLISH):
    """A job folder with one scene's shared timing, visual and master narration, as the timing pass leaves it."""
    scene_dir = shared.shared_highlight_dir(job_dir) / "scene_000"
    scene_dir.mkdir(parents=True)
    (scene_dir / "visual.mp4").write_bytes(b"visual")
    master = _tts(scene_dir / shared.MASTER_NARRATION_AUDIO, text)
    shared.save_master_narration(scene_dir, text, master)
    shared.shared_highlight_dir(job_dir).mkdir(parents=True, exist_ok=True)
    shared.scene_timing_manifest_path(job_dir).write_text(
        json.dumps({"scenes": [{"scene_index": 0, "display_seconds": 6.0, "master_narration_seconds": 4.2,
                                "visual_path": "highlights/_shared/scene_000/visual.mp4"}]}),
        encoding="utf-8",
    )
    return scene_dir


def _render_inputs():
    scenes = [TopicScene(segment_id=0, source_time=0.0, topic_title="Loops", summary_text="Loops repeat.")]
    visuals = [VisualSelection(scene_index=0, mode="still", source_time=1.0, visual_path=Path("still.jpg"))]
    return scenes, visuals


class MasterNarrationReuseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job_dir = Path(tmp.name)

    def test_saved_master_is_reused_only_for_the_same_text(self):
        _job_with_shared_scene(self.job_dir)
        reused = shared.load_master_narration(self.job_dir, 0, ENGLISH)
        self.assertIsNotNone(reused)
        self.assertEqual(reused.duration_seconds, 4.2)
        self.assertEqual(reused.voice_used, "en-US-GuyNeural")
        self.assertIsNone(shared.load_master_narration(self.job_dir, 0, "Different sentence."),
                          "changed text must never reuse the old audio")

    def test_missing_or_unreadable_master_returns_none(self):
        self.assertIsNone(shared.load_master_narration(self.job_dir, 0, ENGLISH), "nothing saved yet")
        scene_dir = _job_with_shared_scene(self.job_dir)
        (scene_dir / shared.MASTER_NARRATION_META).write_text("not json", encoding="utf-8")
        self.assertIsNone(shared.load_master_narration(self.job_dir, 0, ENGLISH), "corrupt metadata")
        (scene_dir / shared.MASTER_NARRATION_AUDIO).unlink()
        self.assertIsNone(shared.load_master_narration(self.job_dir, 0, ENGLISH), "audio file deleted")

    def test_copy_gives_the_same_audio_bytes(self):
        scene_dir = _job_with_shared_scene(self.job_dir)
        master = shared.load_master_narration(self.job_dir, 0, ENGLISH)
        copied = shared.copy_master_narration(master, self.job_dir / "en" / "scene_000" / "narration.mp3")
        self.assertEqual(copied.audio_path.read_bytes(), (scene_dir / shared.MASTER_NARRATION_AUDIO).read_bytes())
        self.assertEqual(copied.duration_seconds, master.duration_seconds)


class TimingPassSynthesisTests(unittest.TestCase):
    def test_timing_pass_saves_master_metadata_for_each_scene(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        job_dir = Path(tmp.name)
        scenes, visuals = _render_inputs()

        def fake_tts(text, output_path, **_kwargs):
            return _tts(output_path, text)

        with mock.patch.object(shared, "generate_scene_narration", side_effect=fake_tts) as tts, \
                mock.patch.object(shared, "build_scene_visual_only", side_effect=lambda v, d, sd, out: out.write_bytes(b"v")):
            shared.ensure_shared_scene_visuals(job_dir, visuals, [ENGLISH], rebuild=True)
        self.assertEqual(tts.call_count, 1, "one English synthesis per scene in the timing pass")
        self.assertIsNotNone(shared.load_master_narration(job_dir, 0, ENGLISH))


class EnglishRenderReuseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job_dir = Path(tmp.name)
        self.scenes, self.visuals = _render_inputs()

    def _render(self, *, tts_side_effect=None, extract_side_effect=None):
        def fake_extract(video, out):
            if extract_side_effect:
                raise extract_side_effect
            out.write_bytes(b"mp3")
            return out

        def fake_concat(paths, output):
            output.write_bytes(b"mp4")
            return output

        with mock.patch.object(hp, "generate_scene_narration", side_effect=tts_side_effect) as tts, \
                mock.patch.object(hp, "build_scene_ass", return_value=self.job_dir / "c.ass"), \
                mock.patch.object(hp, "mux_language_scene", side_effect=lambda *a, **k: a[-1].parent / "scene.mp4"), \
                mock.patch.object(hp, "concat_scenes", side_effect=fake_concat), \
                mock.patch.object(ha, "extract_narration_track", side_effect=fake_extract) as extract, \
                mock.patch.object(hp, "merge_language_status"), \
                mock.patch("sys.stderr"):
            hp._render_language_media(
                "en", self.scenes, self.visuals, [ENGLISH], [ENGLISH], self.job_dir,
                compression="high", translation_seconds=0.0, translator_backend="source",
            )
        return tts, extract

    def test_english_render_reuses_the_timing_pass_audio_without_new_tts(self):
        _job_with_shared_scene(self.job_dir)
        tts, extract = self._render(tts_side_effect=AssertionError("English TTS must not run again"))
        tts.assert_not_called()
        self.assertTrue((self.job_dir / "highlights" / "en" / "scene_000" / "narration.mp3").is_file())
        extract.assert_called_once()

    def test_english_track_is_taken_from_the_finished_video(self):
        _job_with_shared_scene(self.job_dir)
        _tts_mock, extract = self._render(tts_side_effect=AssertionError("unused"))
        video, out = extract.call_args.args
        self.assertEqual(video.name, "highlight_en.mp4")
        self.assertEqual(out, self.job_dir / "highlights" / "en" / hp.HIGHLIGHT_NARRATION_FILE)

    def test_without_saved_master_the_render_still_synthesizes(self):
        # An older job: the timing manifest exists, but no master narration was saved for the scene.
        shared.shared_highlight_dir(self.job_dir).mkdir(parents=True, exist_ok=True)
        shared.scene_timing_manifest_path(self.job_dir).write_text(
            json.dumps({"scenes": [{"scene_index": 0, "display_seconds": 6.0}]}), encoding="utf-8")
        tts, _extract = self._render(tts_side_effect=lambda text, path, **_k: _tts(path, text))
        self.assertEqual(tts.call_count, 1, "older jobs without master metadata keep working")

    def test_stale_english_track_is_removed_when_the_new_one_cannot_be_saved(self):
        _job_with_shared_scene(self.job_dir)
        stale = self.job_dir / "highlights" / "en" / hp.HIGHLIGHT_NARRATION_FILE
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"old narration from a previous render")
        # The new track cannot be saved, so nothing old may be left for the download to serve.
        self._render(tts_side_effect=AssertionError("unused"), extract_side_effect=RuntimeError("encode failed"))
        self.assertFalse(stale.exists(), "a stale narration must never be served")


class ExtractNarrationTrackTests(unittest.TestCase):
    def test_extract_uses_the_video_audio_without_video_and_writes_mp3(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "narration.mp3"

        def write_partial(args):
            Path(args[-1]).write_bytes(b"ID3-real-audio")
            return ""

        with mock.patch.object(ha, "run_ffmpeg", side_effect=write_partial) as run:
            ha.extract_narration_track(Path("highlight_en.mp4"), out)
        args = run.call_args.args[0]
        self.assertIn("-vn", args)
        self.assertIn("libmp3lame", args)
        self.assertEqual(out.read_bytes(), b"ID3-real-audio")
        self.assertFalse(out.with_name("narration.partial.mp3").exists())

    def test_failure_leaves_no_partial_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "narration.mp3"

        def fail(args):
            Path(args[-1]).write_bytes(b"partial")
            raise RuntimeError("encode failed")

        with mock.patch.object(ha, "run_ffmpeg", side_effect=fail):
            with self.assertRaises(RuntimeError):
                ha.extract_narration_track(Path("highlight_en.mp4"), out)
        self.assertFalse(out.exists())
        self.assertFalse(out.with_name("narration.partial.mp3").exists())

    def test_empty_extraction_is_an_error_not_an_empty_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "narration.mp3"

        def write_nothing(args):
            Path(args[-1]).write_bytes(b"")
            return ""

        with mock.patch.object(ha, "run_ffmpeg", side_effect=write_nothing):
            with self.assertRaises(RuntimeError):
                ha.extract_narration_track(Path("highlight_en.mp4"), out)
        self.assertFalse(out.exists())


def _ffmpeg_available():
    try:
        from src.highlight_ffmpeg import resolve_ffmpeg_bin
        import subprocess
        return subprocess.run([resolve_ffmpeg_bin(), "-version"], capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _make_highlight_video(path: Path, seconds: float = 3.0) -> Path:
    """A real MP4 with a picture and a tone, like a finished highlight video. Made with FFmpeg, not mocked."""
    from src.highlight_ffmpeg import resolve_ffmpeg_bin
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [resolve_ffmpeg_bin(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def _duration_seconds(path: Path) -> float:
    from src.highlight_ffmpeg import probe_duration_seconds
    return probe_duration_seconds(path)


@unittest.skipUnless(_ffmpeg_available(), "FFmpeg is not available")
class EnglishAudioDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job_dir = Path(tmp.name) / "a1b2c3d4"
        self.job_dir.mkdir()
        (self.job_dir / "audio_explanation.mp3").write_bytes(b"SUMMARY-AUDIO-2-MINUTES")
        patcher = mock.patch.object(self.app_module, "_job_output_dir", return_value=self.job_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.english = self.job_dir / "highlights" / "en"

    def _set_requested(self, languages):
        status = {"languages": {}, "requested_languages": languages}
        (self.job_dir / "highlights").mkdir(exist_ok=True)
        (self.job_dir / "highlights" / "highlight_status.json").write_text(json.dumps(status), encoding="utf-8")

    def _get_audio(self, query=""):
        resp = self.client.get(f"/download/audio/a1b2c3d4{query}")
        self.addCleanup(resp.close)
        return resp

    def test_remote_english_highlight_without_narration_file_downloads_the_video_audio(self):
        """Reproduces the 0:00 player: the worker returns only highlight_en.mp4, and no narration file exists."""
        self._set_requested(["en", "kn"])
        video = _make_highlight_video(self.english / "highlight_en.mp4", seconds=3.0)
        self.assertFalse((self.english / hp.HIGHLIGHT_NARRATION_FILE).exists())
        resp = self._get_audio()
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(resp.data), 0, "an empty body is the 0:00 player")
        track = self.english / hp.HIGHLIGHT_NARRATION_FILE
        self.assertEqual(resp.data, track.read_bytes(), "the response is the saved track")
        self.assertAlmostEqual(_duration_seconds(track), _duration_seconds(video), delta=0.2)
        self.assertGreater(_duration_seconds(track), 2.5)

    def test_zero_byte_track_is_never_served_with_200(self):
        self._set_requested(["en"])
        _make_highlight_video(self.english / "highlight_en.mp4")
        (self.english / hp.HIGHLIGHT_NARRATION_FILE).write_bytes(b"")
        resp = self._get_audio()
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(len(resp.data), 0)

    def test_stale_track_older_than_the_video_is_rebuilt(self):
        self._set_requested(["en"])
        track = self.english / hp.HIGHLIGHT_NARRATION_FILE
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_bytes(b"OLD-TRACK-FROM-AN-EARLIER-RENDER")
        os.utime(track, (1_000_000, 1_000_000))
        _make_highlight_video(self.english / "highlight_en.mp4")
        resp = self._get_audio()
        self.assertNotEqual(resp.data, b"OLD-TRACK-FROM-AN-EARLIER-RENDER")
        self.assertGreater(len(resp.data), 0)

    def test_missing_english_video_is_reported_and_summary_audio_is_never_substituted(self):
        self._set_requested(["en", "kn"])
        resp = self._get_audio()
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(b"SUMMARY-AUDIO", resp.data)
        self.assertIn("not available", resp.get_json()["error"])
        self.assertFalse((self.english / hp.HIGHLIGHT_NARRATION_FILE).exists())

    def test_zero_byte_english_video_is_not_used(self):
        self._set_requested(["en"])
        self.english.mkdir(parents=True)
        (self.english / "highlight_en.mp4").write_bytes(b"")
        resp = self._get_audio()
        self.assertEqual(resp.status_code, 404)

    def test_without_highlights_the_summary_audio_is_served_as_before(self):
        resp = self._get_audio()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"SUMMARY-AUDIO-2-MINUTES")

    def test_source_summary_returns_the_full_narration_on_purpose(self):
        self._set_requested(["en"])
        _make_highlight_video(self.english / "highlight_en.mp4")
        resp = self._get_audio("?source=summary")
        self.assertEqual(resp.data, b"SUMMARY-AUDIO-2-MINUTES")

    def test_invalid_job_id_is_refused(self):
        resp = self.client.get("/download/audio/..%2F..")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
