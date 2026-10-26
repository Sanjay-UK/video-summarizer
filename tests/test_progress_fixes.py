"""Regression tests for the code-review findings on progress, completion, errors, and fonts.

A full process_video run is used where the state transitions matter. The executor, audio, and subtitle steps are
stubbed; the job's own state and the highlight/completion code are real. No network, RunPod, or GPU call is made.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import job_progress as jp  # noqa: E402
from src import youtube_errors as ye  # noqa: E402

try:
    import app as app_module  # noqa: E402
except ImportError:  # the worker image does not ship the Flask app
    app_module = None

URL = "https://www.youtube.com/watch?v=qAp-8nGOGos"
LOCAL_RUNTIME = {"mode": "local", "location_label": "this machine", "heavy_location": "local",
                 "light_location": "local"}


def _write_heavy_outputs(job_dir: Path):
    (job_dir / "transcript.txt").write_text("hello world", encoding="utf-8")
    (job_dir / "segments.json").write_text("[]", encoding="utf-8")
    (job_dir / "summary.txt").write_text("A short summary of the video.", encoding="utf-8")
    (job_dir / "summary_segments.json").write_text("[]", encoding="utf-8")


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class FullRunTestBase(unittest.TestCase):
    """Runs process_video end to end with the external steps stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "output"
        self.out.mkdir()
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"v")
        self.audio = self.root / "clip.mp3"
        self.audio.write_bytes(b"a")
        self.snapshots = []
        self.executor = mock.Mock()
        self.executor.run_heavy_phase.side_effect = self._heavy
        self.highlight_behaviour = None  # set by tests: a callable or None
        patches = [
            mock.patch.object(app_module, "OUTPUT_DIR", self.out),
            mock.patch.object(app_module, "jobs", {}),
            mock.patch.object(app_module, "job_history", []),
            mock.patch.object(app_module, "resolve_runtime_for_job", return_value=LOCAL_RUNTIME),
            mock.patch.object(app_module, "apply_runtime_side_effects", return_value={}),
            mock.patch.object(app_module, "restore_runtime_side_effects", return_value=None),
            mock.patch.object(app_module, "get_local_video", return_value=self.video),
            mock.patch.object(app_module, "copy_source_video_to_job", side_effect=lambda v, d: v),
            mock.patch.object(app_module, "extract_audio", return_value=self.audio),
            mock.patch.object(app_module, "choose_whisper_model", return_value="base"),
            mock.patch("src.highlight_ffmpeg.probe_duration_seconds", return_value=30.0),
            mock.patch.object(app_module, "build_executor", return_value=self.executor),
            mock.patch.object(app_module, "generate_audio_explanation",
                              return_value=(self.root / "a.mp3", "audio text")),
            mock.patch.object(app_module, "generate_subtitles", return_value=self.root / "s.srt"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Record every status snapshot the API could show, so invariants can be checked over the whole run.
        original = app_module.JobProgress.to_dict

        def recording_to_dict(job_self):
            snap = original(job_self)
            self.snapshots.append(snap)
            return snap

        patch = mock.patch.object(app_module.JobProgress, "to_dict", recording_to_dict)
        patch.start()
        self.addCleanup(patch.stop)

    def _heavy(self, **kwargs):
        _write_heavy_outputs(kwargs["job_output_dir"])

    def _run(self, job_id="0a1b2c3d", highlight=True, source=None):
        # Local clip by default: no test may reach YouTube. A YouTube URL is used only with the download patched.
        source = source or str(self.video)
        job = app_module.JobProgress(job_id, source)
        app_module.jobs[job_id] = job
        ok = app_module.process_video(job_id, source, highlight_video=highlight)
        return job, ok

    def _assert_never_completed_early(self):
        for snap in self.snapshots:
            if snap["status"] != "completed":
                self.assertLess(snap["progress"], 1.0, f"100% while {snap['status']}: {snap}")


class HighlightStageTests(FullRunTestBase):
    """H1: the highlight call is one indeterminate stage; the sub-stage percentages no longer appear."""

    def test_blocking_highlight_call_is_one_indeterminate_stage(self):
        seen = {}

        def highlight(**kwargs):
            job = app_module.jobs["0a1b2c3d"]
            seen.update(step=job.current_step, indeterminate=job.indeterminate, progress=job.progress)
            return {"en": kwargs["job_output_dir"] / "highlights" / "en" / "highlight_en.mp4"}

        self.executor.run_highlight_phase.side_effect = highlight
        job, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(seen["step"], "highlight_assembly")
        self.assertTrue(seen["indeterminate"])
        self.assertAlmostEqual(seen["progress"], 0.92)

    def test_no_invented_sub_stage_percentages_are_logged(self):
        job, _ = self._run()
        joined = "\n".join(job.log_lines)
        self.assertNotIn("highlight_topics", joined)
        self.assertNotIn("highlight_visuals", joined)

    def test_successful_highlight_returns_to_a_measured_state_then_completes(self):
        self.executor.run_highlight_phase.return_value = {
            "en": self.out / "x" / "highlights" / "en" / "highlight_en.mp4"}
        job, ok = self._run()
        self.assertTrue(ok)
        self.assertFalse(job.indeterminate)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.progress, 1.0)
        self.assertEqual(job.current_step, "highlight_complete")

    def test_failed_highlight_does_not_stay_indeterminate_and_the_job_still_settles(self):
        self.executor.run_highlight_phase.side_effect = RuntimeError("ffmpeg exploded")
        job, ok = self._run()
        self.assertTrue(ok, "a highlight failure is recorded per language; the job still completes")
        self.assertFalse(job.indeterminate)
        self.assertIn("Highlight video failed: ffmpeg exploded", "\n".join(job.log_lines))
        self.assertEqual(job.status, "completed")


class CompletionTests(FullRunTestBase):
    """M4: 100% is set only together with the completed status, and never while processing."""

    def test_progress_is_never_one_hundred_while_processing(self):
        self.executor.run_highlight_phase.return_value = {}
        job, _ = self._run()
        job.to_dict()  # the state a poll would read after the run
        self._assert_never_completed_early()
        self.assertTrue(any(s["status"] == "completed" and s["progress"] == 1.0 for s in self.snapshots))

    def test_status_and_progress_change_together_on_success(self):
        job, _ = self._run(highlight=False)
        final = job.to_dict()
        self.assertEqual((final["status"], final["progress"]), ("completed", 1.0))

    def test_failure_keeps_progress_below_one_and_says_error(self):
        self.executor.run_heavy_phase.side_effect = RuntimeError("worker failed")
        job, ok = self._run(highlight=False)
        self.assertFalse(ok)
        final = job.to_dict()
        self.assertEqual(final["status"], "error")
        self.assertLess(final["progress"], 1.0)
        self._assert_never_completed_early()


class FailureMessageTests(FullRunTestBase):
    """M3 and L1: actionable guidance for anti-bot failures; typed codes only."""

    def test_anti_bot_download_failure_gives_upload_guidance_in_the_log(self):
        error = ye.YouTubeDownloadError(ye.YOUTUBE_AUTH_REQUIRED, "Video page needs to be reloaded (SABR)")
        with mock.patch.object(app_module, "download_youtube_video", side_effect=error):
            job, ok = self._run(highlight=False, source=URL)
        self.assertFalse(ok)
        log = "\n".join(job.log_lines)
        self.assertIn("anti-bot protection", log)
        self.assertIn("upload it instead", log)
        self.assertIn("Upload Video tab", job.error)
        self.assertNotIn("SABR", job.error, "technical detail stays out of the user message")

    def test_error_code_is_stored_only_for_the_typed_youtube_error(self):
        self.executor.run_heavy_phase.side_effect = RuntimeError("boom")
        job, _ = self._run(highlight=False)
        self.assertIsNone(job.error_code)
        meta = json.loads((job.job_output_dir / "summary_metadata.json").read_text(encoding="utf-8"))
        self.assertIsNone(meta["error_code"])

    def test_foreign_exception_code_attribute_is_not_persisted(self):
        http_like = RuntimeError("HTTP 404")
        http_like.code = 404
        self.executor.run_heavy_phase.side_effect = http_like
        job, _ = self._run(highlight=False)
        self.assertIsNone(job.error_code)

    def test_reporter_is_reset_after_a_failed_run(self):
        self.executor.run_heavy_phase.side_effect = RuntimeError("worker failed")
        self._run(highlight=False)
        self.assertIsNone(jp._reporter.get())


class TranslationStageTests(unittest.TestCase):
    """M5: translation is its own labelled stage, and only when translation actually runs."""

    def _run_heavy(self, languages, reported):
        from src import pipeline_executor as pe

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        job_dir = Path(tmp.name)
        _write_heavy_outputs(job_dir)
        token = jp.set_reporter(lambda kind, value, step, ind: reported.append((kind, step, ind)))
        self.addCleanup(jp.reset_reporter, token)
        with mock.patch.object(pe, "transcribe_audio"), mock.patch.object(pe, "summarize_transcript"), \
                mock.patch.object(pe, "translate_summary_for_languages") as translate:
            pe.LocalPipelineExecutor().run_heavy_phase(
                job_id="x", audio_path=Path("a.mp3"), job_output_dir=job_dir, whisper_model="base",
                summarization_model="m", compression="medium", highlight_languages=languages, log=lambda m: None)
        return translate

    def test_translation_is_labelled_and_indeterminate_when_it_runs(self):
        reported = []
        translate = self._run_heavy("en,hi", reported)
        translate.assert_called_once()
        self.assertIn(("stage", "translating", True), reported)

    def test_no_translation_label_when_only_english_is_requested(self):
        reported = []
        self._run_heavy("en", reported)
        self.assertFalse(any(step == "translating" for _kind, step, _ind in reported))


class ErrorPrecedenceTests(unittest.TestCase):
    """M2 and L2: a non-retryable error is not overwritten by a later retryable one; cookies alone are not sign-in."""

    def test_sign_in_from_one_client_beats_a_rate_limit_from_another(self):
        self.assertEqual(ye.pick_error_text(["Sign in to confirm your age", "HTTP Error 429: Too Many Requests"]),
                         "Sign in to confirm your age")

    def test_order_does_not_matter_for_the_non_retryable_error(self):
        self.assertEqual(ye.pick_error_text(["HTTP Error 429: Too Many Requests", "Sign in to confirm your age"]),
                         "Sign in to confirm your age")

    def test_retryable_errors_keep_the_latest_message(self):
        self.assertEqual(ye.pick_error_text(["urlopen error timed out", "HTTP Error 429"]), "HTTP Error 429")

    def test_downloader_uses_the_precedence_across_clients(self):
        try:
            from src import downloader as dl
        except ImportError:
            self.skipTest("yt-dlp is not part of the worker image")

        with mock.patch.object(dl, "_try_download",
                               side_effect=["HTTP Error 429: Too Many Requests", "Sign in to confirm your age"] +
                               ["HTTP Error 429"] * 10):
            path, error = dl._download_with_clients("https://www.youtube.com/watch?v=x",
                                                     Path(tempfile.mkdtemp()), "x", 360)
        self.assertIsNone(path)
        self.assertEqual(ye.classify_youtube_error(error), ye.YOUTUBE_AUTH_REQUIRED)

    def test_a_generic_cookies_mention_is_not_classified_as_sign_in(self):
        self.assertEqual(ye.classify_youtube_error("Cookies are no longer supported by this extractor"),
                         ye.YOUTUBE_DOWNLOAD_FAILED)

    def test_reference_age_gate_still_classifies_as_sign_in(self):
        self.assertEqual(ye.classify_youtube_error(
            "ERROR: [youtube] qAp: Sign in to confirm your age. Use --cookies-from-browser or --cookies"),
            ye.YOUTUBE_AUTH_REQUIRED)


class FontDirectoryTests(unittest.TestCase):
    """M6: rendering reads the bundled fonts in place. Concurrent jobs never copy into a shared folder."""

    def test_concurrent_calls_return_the_bundled_directory_without_copying(self):
        from src import highlight_assembler as ha
        from src.locales import FONTS_DIR

        results = []
        with mock.patch("shutil.copy2", side_effect=AssertionError("no copy while jobs render")):
            threads = [threading.Thread(target=lambda: results.append(ha._fonts_dir_for_ffmpeg())) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == FONTS_DIR for r in results))


@unittest.skipUnless((ROOT / ".dockerignore").exists(), ".dockerignore is not in the worker image")
class DockerignoreTests(unittest.TestCase):
    """L4: secrets and local data are excluded; every worker build input stays. An approximation of Docker's rules."""

    def _patterns(self):
        lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        return [l.strip().rstrip("/") for l in lines if l.strip() and not l.startswith("#")]

    def _excluded(self, path):
        import fnmatch

        parts = path.split("/")
        for pattern in self._patterns():
            for i in range(len(parts)):
                prefix = "/".join(parts[: i + 1])
                if fnmatch.fnmatch(prefix, pattern) or fnmatch.fnmatch(parts[i], pattern):
                    return True
        return False

    def test_secrets_and_local_data_are_excluded(self):
        for path in (".env", "aws-video-summarizer_accessKeys.csv", "output/18019c85/summary.txt",
                     "downloads/x.mp4", "data/compute_selection.json", ".git/config"):
            with self.subTest(path=path):
                self.assertTrue(self._excluded(path))

    def test_every_worker_build_input_is_kept(self):
        for path in ("config.py", "src/downloader.py", "runpod_worker/handler.py", "runpod_worker/Dockerfile",
                     "requirements/worker.lock", "requirements/worker-gtts.lock", ".cache/worker-wheels/x.whl",
                     "static/fonts/NotoSansKannada-Regular.ttf"):
            with self.subTest(path=path):
                self.assertFalse(self._excluded(path))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class FailedJobPayloadTests(FullRunTestBase):
    """Task 1: the live progress payload carries the job's own sanitized error; other payloads are unchanged."""

    def test_processing_and_completed_payloads_have_no_error(self):
        job = app_module.JobProgress("5a5a5a5a", URL)
        self.assertIsNone(job.to_dict()["error"])
        job.set_step("transcribing", 0.3)
        self.assertIsNone(job.to_dict()["error"])
        job.mark_completed()
        self.assertIsNone(job.to_dict()["error"])

    def test_successful_run_payload_is_unchanged_apart_from_the_new_key(self):
        job, ok = self._run(highlight=False)
        payload = job.to_dict()
        self.assertTrue(ok)
        self.assertEqual((payload["status"], payload["progress"], payload["error"]), ("completed", 1.0, None))
        self.assertTrue({"job_id", "current_step", "progress", "status", "log_lines"} <= set(payload))

    def test_failed_run_payload_carries_the_actual_failure_message(self):
        self.executor.run_heavy_phase.side_effect = RuntimeError("Could not decode the audio stream")
        job, ok = self._run(highlight=False)
        self.assertFalse(ok)
        self.assertEqual(job.to_dict()["error"], "Could not decode the audio stream")

    def test_youtube_guidance_reaches_the_live_payload(self):
        error = ye.YouTubeDownloadError(ye.YOUTUBE_AUTH_REQUIRED, "Sign in to confirm your age")
        with mock.patch.object(app_module, "download_youtube_video", side_effect=error):
            job, _ = self._run(highlight=False, source=URL)
        self.assertEqual(job.to_dict()["error"], ye.USER_MESSAGES[ye.YOUTUBE_AUTH_REQUIRED])

    def test_secrets_and_urls_never_reach_the_payload_results_or_metadata(self):
        leaky = "Could not open https://cdn.example/clip.mp4?X-Amz-Signature=SECRETSIG Authorization: Bearer TOKEN123"
        self.executor.run_heavy_phase.side_effect = RuntimeError(leaky)
        job, _ = self._run(highlight=False)
        results = app_module.app.test_client().get(f"/api/results/{job.job_id}")
        meta = (job.job_output_dir / "summary_metadata.json").read_text(encoding="utf-8")
        for text in (json.dumps(job.to_dict()), results.get_data(as_text=True), meta):
            self.assertNotIn("SECRETSIG", text)
            self.assertNotIn("TOKEN123", text)
            self.assertNotIn("https://cdn.example", text)

    def test_history_and_direct_results_show_the_same_failure_message(self):
        self.executor.run_heavy_phase.side_effect = RuntimeError("Could not decode the audio stream")
        job, _ = self._run(highlight=False)
        self.app_client = app_module.app.test_client()
        live = job.to_dict()["error"]
        results_body = self.app_client.get(f"/api/results/{job.job_id}").get_json()
        self.assertEqual(results_body["error"], live)
        self.assertEqual(app_module._failed_job_payload(job.job_output_dir)["error"], live)
