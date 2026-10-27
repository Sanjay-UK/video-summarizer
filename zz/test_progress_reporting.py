"""Progress reporting for local and RunPod jobs: measured values only, monotonic, never 100% before completion.

Fakes stand in for Whisper, yt-dlp, and the RunPod executor. The tests check the values the job reports, not the
rendering. Browser rendering is covered by test_progress_view_js.py.
"""

import os
import sys
import tempfile
import unittest

import fake_s3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import job_progress as jp  # noqa: E402

try:
    import app as app_module  # noqa: E402
except ImportError:  # the worker image does not ship the Flask app
    app_module = None


class ReporterTests(unittest.TestCase):
    def test_reports_are_ignored_without_a_reporter(self):
        jp.report_progress(0.5)
        jp.report_stage("summarizing", 0.6, indeterminate=True)  # nothing to assert: must simply not raise

    def test_a_failing_reporter_cannot_fail_the_caller(self):
        def broken(kind, value, step, indeterminate):
            raise RuntimeError("reporter broke")

        token = jp.set_reporter(broken)
        try:
            jp.report_progress(0.3)
            jp.report_stage("x", 0.1)
        finally:
            jp.reset_reporter(token)

    def test_reporter_receives_stage_and_progress(self):
        calls = []
        token = jp.set_reporter(lambda *args: calls.append(args))
        try:
            jp.report_stage("summarizing", 0.6, indeterminate=True)
            jp.report_progress(0.25)
        finally:
            jp.reset_reporter(token)
        self.assertEqual(calls, [("stage", 0.6, "summarizing", True), ("progress", 0.25, None, False)])


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class JobProgressRulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        patch = mock.patch.object(app_module, "OUTPUT_DIR", self.out)
        patch.start()
        self.addCleanup(patch.stop)

    def _job(self, job_id="0a1b2c3d"):
        return app_module.JobProgress(job_id, "clip.mp4")

    def test_measured_progress_never_moves_backward(self):
        job = self._job()
        job.set_progress(0.40)
        job.set_progress(0.20)  # a late or out-of-order report must not pull the bar back
        self.assertEqual(job.progress, 0.40)

    def test_measured_progress_never_reaches_one_before_completion(self):
        job = self._job()
        job.set_progress(1.0)
        self.assertLess(job.progress, 1.0)
        self.assertLessEqual(job.progress, 0.99)

    def test_invalid_measurements_are_ignored(self):
        job = self._job()
        job.set_progress("not a number")
        job.set_progress(None)
        self.assertEqual(job.progress, 0.0)

    def test_indeterminate_stage_is_marked_and_a_measured_value_clears_it(self):
        job = self._job()
        job.set_step("summarizing", 0.60, indeterminate=True)
        self.assertTrue(job.to_dict()["indeterminate"])
        job.set_progress(0.61)
        self.assertFalse(job.to_dict()["indeterminate"])

    def test_payload_carries_the_fields_the_bar_needs(self):
        job = self._job()
        job.set_step("transcribing", 0.35)
        payload = job.to_dict()
        for key in ("indeterminate", "stage_elapsed_sec", "progress_source", "progress", "current_step"):
            self.assertIn(key, payload)
        self.assertGreaterEqual(payload["stage_elapsed_sec"], 0)

    def test_download_hook_maps_bytes_into_the_download_range(self):
        job = self._job()
        hook = app_module._download_progress_hook(job)
        hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        self.assertAlmostEqual(job.progress, 0.075)
        hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
        self.assertAlmostEqual(job.progress, 0.075, msg="finished events do not move the bar")

    def test_download_hook_never_fails_the_download(self):
        job = self._job()
        hook = app_module._download_progress_hook(job)
        hook(None)
        hook({"status": "downloading", "downloaded_bytes": "x", "total_bytes": 100})
        self.assertEqual(job.progress, 0.0)

    def test_local_reporter_maps_transcription_into_its_range(self):
        job = self._job()
        reporter = app_module._job_reporter(job, transcribe_range=(0.35, 0.60))
        reporter("progress", 0.5, None, False)
        self.assertAlmostEqual(job.progress, 0.475)
        reporter("stage", 0.60, "summarizing", True)
        self.assertEqual(job.current_step, "summarizing")
        self.assertTrue(job.indeterminate)


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class TranscriptionProgressTests(unittest.TestCase):
    def test_transcription_reports_audio_seconds_done_as_fractions(self):
        from src import transcriber

        segments = [SimpleNamespace(start=0.0, end=10.0, text="a"), SimpleNamespace(start=10.0, end=30.0, text="b")]

        class FakeModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, *args, **kwargs):
                return iter(segments), SimpleNamespace(duration=40.0)

        reported = []
        token = jp.set_reporter(lambda kind, value, step, ind: reported.append((kind, round(value, 3))))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audio = Path(tmp.name) / "a.mp3"
        audio.write_bytes(b"x")
        try:
            with mock.patch.object(transcriber, "WhisperModel", FakeModel), \
                    mock.patch.object(transcriber, "correct_transcript", side_effect=lambda s: (s, [], [])):
                transcriber.transcribe_audio(audio, model="base", output_dir=Path(tmp.name) / "out", device="cpu")
        finally:
            jp.reset_reporter(token)
        fractions = [v for kind, v in reported if kind == "progress"]
        self.assertEqual(fractions, [0.25, 0.75])  # 10 s and 30 s of a 40 s file
        self.assertEqual(fractions, sorted(fractions), "measured transcription progress never goes backward")


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class RunpodHeavyPhaseTests(unittest.TestCase):
    """During the RunPod heavy phase the bar is indeterminate and never shows a number it cannot measure."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "output"
        self.out.mkdir()
        self.downloads = Path(tmp.name) / "downloads"
        self.downloads.mkdir()
        self.video = self.downloads / "clip.mp4"
        self.video.write_bytes(b"v")
        self.audio = self.downloads / "clip.mp3"
        self.audio.write_bytes(b"a")
        patches = [
            mock.patch.object(app_module, "OUTPUT_DIR", self.out),
            mock.patch.object(app_module, "jobs", {}),
            mock.patch.object(app_module, "_s3_store", return_value=fake_s3.new_store()),
            mock.patch.object(app_module, "resolve_runtime_for_job", return_value={
                "mode": "hybrid_runpod", "location_label": "RunPod", "heavy_location": "runpod_serverless",
                "light_location": "local"}),
            mock.patch.object(app_module, "apply_runtime_side_effects", return_value={}),
            mock.patch.object(app_module, "restore_runtime_side_effects", return_value=None),
            mock.patch.object(app_module, "get_local_video", return_value=self.video),
            mock.patch.object(app_module, "copy_source_video_to_job", side_effect=lambda v, d: v),
            mock.patch.object(app_module, "extract_audio", return_value=self.audio),
            mock.patch.object(app_module, "choose_whisper_model", return_value="base"),
            mock.patch("src.highlight_ffmpeg.probe_duration_seconds", return_value=30.0),
            mock.patch("src.compute_deploy.load_compute_mode", return_value="serverless"),
            mock.patch("src.serverless_model_catalog.load_saved_serverless_models", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_heavy_phase_is_indeterminate_and_never_reports_a_fake_percent(self):
        seen = {}
        job_id = "1b2c3d4e"
        executor = mock.Mock()

        def heavy(**kwargs):
            job = app_module.jobs[job_id]
            seen.update(indeterminate=job.indeterminate, progress=job.progress, source=job.progress_source,
                        payload=job.to_dict())
            raise RuntimeError("stop after the heavy phase")

        executor.run_heavy_phase.side_effect = heavy
        job = app_module.JobProgress(job_id, str(self.video))
        app_module.jobs[job_id] = job
        with mock.patch.object(app_module, "build_executor", return_value=executor):
            ok = app_module.process_video(job_id, str(self.video), highlight_video=False)
        self.assertFalse(ok)
        self.assertTrue(seen["indeterminate"])
        self.assertEqual(seen["source"], "runpod")
        self.assertAlmostEqual(seen["progress"], 0.35)
        self.assertLess(job.progress, 1.0)
        self.assertEqual(job.status, "error", "a failed stage stops the normal progress")

    def test_failed_job_never_reaches_one_hundred_percent(self):
        executor = mock.Mock()
        executor.run_heavy_phase.side_effect = RuntimeError("worker failed")
        job_id = "2b2c3d4e"
        job = app_module.JobProgress(job_id, str(self.video))
        app_module.jobs[job_id] = job
        with mock.patch.object(app_module, "build_executor", return_value=executor):
            app_module.process_video(job_id, str(self.video), highlight_video=False)
        self.assertEqual(job.status, "error")
        self.assertLess(job.progress, 1.0)


if __name__ == "__main__":
    unittest.main()
