"""YouTube download failures: classification, retry rules, job and API behaviour, and secret handling.

The reference failure (job 606d4df8: age-restricted video, "Sign in to confirm your age") is reproduced with a mocked
yt-dlp exception. No YouTube request is made and the reference video is never downloaded.
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

try:
    import yt_dlp  # noqa: E402
except ImportError:  # the worker image does not install yt-dlp
    raise unittest.SkipTest("yt-dlp is not part of the worker image")

from src import downloader as dl  # noqa: E402
from src import youtube_errors as ye  # noqa: E402

AGE_MESSAGE = (
    "ERROR: [youtube] qAp-8nGOGos: Sign in to confirm your age. This video may be inappropriate for some users. "
    "Use --cookies-from-browser or --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
)
URL = "https://www.youtube.com/watch?v=qAp-8nGOGos"


def _download_error(message):
    return yt_dlp.utils.DownloadError(message)


class ClassificationTests(unittest.TestCase):
    def test_age_restriction_and_sign_in_map_to_auth_required(self):
        for message in (AGE_MESSAGE, "Sign in to confirm you're not a bot", "This video is age-restricted",
                        "requires login to watch", "Use --cookies-from-browser"):
            with self.subTest(message=message):
                self.assertEqual(ye.classify_youtube_error(message), ye.YOUTUBE_AUTH_REQUIRED)

    def test_private_unavailable_rate_limit_and_network(self):
        self.assertEqual(ye.classify_youtube_error("ERROR: Private video. Sign in if you've been granted access"),
                         ye.YOUTUBE_PRIVATE)
        self.assertEqual(ye.classify_youtube_error("ERROR: Video unavailable. This video has been removed"),
                         ye.YOUTUBE_UNAVAILABLE)
        self.assertEqual(ye.classify_youtube_error("HTTP Error 429: Too Many Requests"), ye.YOUTUBE_RATE_LIMITED)
        self.assertEqual(ye.classify_youtube_error("urlopen error timed out"), ye.YOUTUBE_NETWORK)

    def test_unknown_and_empty_messages_are_not_authentication_failures(self):
        self.assertEqual(ye.classify_youtube_error("something odd happened"), ye.YOUTUBE_DOWNLOAD_FAILED)
        self.assertEqual(ye.classify_youtube_error(""), ye.YOUTUBE_DOWNLOAD_FAILED)
        self.assertEqual(ye.classify_youtube_error(None), ye.YOUTUBE_DOWNLOAD_FAILED)

    def test_only_deterministic_codes_are_non_retryable(self):
        self.assertEqual(ye.NON_RETRYABLE, {ye.YOUTUBE_AUTH_REQUIRED, ye.YOUTUBE_PRIVATE, ye.YOUTUBE_UNAVAILABLE})
        self.assertFalse(ye.YouTubeDownloadError(ye.YOUTUBE_AUTH_REQUIRED).retryable)
        self.assertTrue(ye.YouTubeDownloadError(ye.YOUTUBE_NETWORK).retryable)
        self.assertTrue(ye.YouTubeDownloadError(ye.YOUTUBE_DOWNLOAD_FAILED).retryable)


class UserMessageTests(unittest.TestCase):
    def test_auth_message_is_actionable_and_does_not_show_technical_text(self):
        error = ye.YouTubeDownloadError(ye.YOUTUBE_AUTH_REQUIRED, AGE_MESSAGE)
        self.assertEqual(str(error), ye.USER_MESSAGES[ye.YOUTUBE_AUTH_REQUIRED])
        self.assertIn("Upload Video tab", str(error))
        for hidden in ("--cookies", "qAp-8nGOGos", "github.com", "Traceback"):
            self.assertNotIn(hidden, str(error))

    def test_detail_is_sanitized_for_logs(self):
        detail = "see https://x.example/a?token=SECRET Authorization: Bearer ABC123 cookie=abc123"
        error = ye.YouTubeDownloadError(ye.YOUTUBE_DOWNLOAD_FAILED, detail)
        for secret in ("SECRET", "ABC123", "abc123", "https://x.example"):
            self.assertNotIn(secret, error.detail)

    def test_user_actionable_codes_are_the_three_that_need_the_user(self):
        self.assertEqual(ye.USER_ACTIONABLE, ye.NON_RETRYABLE)


class DownloaderRetryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        sleep = mock.patch.object(dl.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_age_restriction_stops_at_once_without_retries_or_download_attempts(self):
        with mock.patch.object(dl, "get_video_meta", side_effect=_download_error(AGE_MESSAGE)) as meta, \
                mock.patch.object(dl, "_download_with_clients") as clients:
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video(URL, self.out, max_retries=3)
        self.assertEqual(ctx.exception.code, ye.YOUTUBE_AUTH_REQUIRED)
        self.assertEqual(meta.call_count, 1, "deterministic failures must not be retried")
        clients.assert_not_called()
        self.sleep.assert_not_called()
        self.assertNotIn("--cookies", str(ctx.exception))

    def test_client_fallback_auth_error_is_classified_and_not_retried(self):
        with mock.patch.object(dl, "get_video_meta", return_value=("Title", "vid", [])), \
                mock.patch.object(dl, "_try_download", return_value=AGE_MESSAGE) as attempt:
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video(URL, self.out, max_retries=3)
        self.assertEqual(ctx.exception.code, ye.YOUTUBE_AUTH_REQUIRED)
        self.assertEqual(attempt.call_count, len(dl.YOUTUBE_CLIENT_FALLBACKS), "one pass over the clients only")
        self.sleep.assert_not_called()

    def test_unknown_errors_keep_the_previous_retry_behaviour(self):
        with mock.patch.object(dl, "get_video_meta", side_effect=_download_error("something odd happened")) as meta:
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video(URL, self.out, max_retries=3)
        self.assertEqual(ctx.exception.code, ye.YOUTUBE_DOWNLOAD_FAILED)
        self.assertEqual(meta.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_network_errors_are_retried(self):
        with mock.patch.object(dl, "get_video_meta", side_effect=_download_error("urlopen error timed out")) as meta:
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video(URL, self.out, max_retries=2)
        self.assertEqual(ctx.exception.code, ye.YOUTUBE_NETWORK)
        self.assertEqual(meta.call_count, 2)

    def test_successful_download_is_unchanged(self):
        expected = self.out / "Title" / "Title.mp4"

        def fake_clients(url, out_dir, base_name, quality, progress_hook=None):
            expected.parent.mkdir(parents=True, exist_ok=True)
            expected.write_bytes(b"x")
            return expected, None

        with mock.patch.object(dl, "get_video_meta", return_value=("Title", "vid", [])), \
                mock.patch.object(dl, "_download_with_clients", side_effect=fake_clients):
            self.assertEqual(dl.download_youtube_video(URL, self.out), expected)

    def test_raised_error_carries_no_chained_yt_dlp_exception(self):
        with mock.patch.object(dl, "get_video_meta", side_effect=_download_error(AGE_MESSAGE)):
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video(URL, self.out)
        self.assertIsNone(ctx.exception.__context__)
        self.assertIsNone(ctx.exception.__cause__)

    def test_progress_hook_is_passed_to_yt_dlp_options(self):
        hook = mock.Mock()
        opts = dl._build_download_opts(self.out, "x", 360, progress_hook=hook)
        self.assertEqual(opts["progress_hooks"], [hook])
        self.assertNotIn("progress_hooks", dl._build_download_opts(self.out, "x", 360))


class JobAndApiTests(unittest.TestCase):
    """A failed download ends the job before any GPU, RunPod, or S3 work, and the API reports it correctly."""

    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app = app_module

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "output"
        self.out.mkdir()
        patches = [
            mock.patch.object(self.app, "OUTPUT_DIR", self.out),
            mock.patch.object(self.app, "jobs", {}),
            mock.patch.object(self.app, "job_history", []),
            mock.patch.object(self.app, "resolve_runtime_for_job",
                              return_value={"location_label": "this machine", "heavy_location": "local",
                                            "light_location": "local"}),
            mock.patch.object(self.app, "apply_runtime_side_effects", return_value={}),
            mock.patch.object(self.app, "restore_runtime_side_effects", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.executor = mock.Mock()
        build = mock.patch.object(self.app, "build_executor", return_value=self.executor)
        build.start()
        self.addCleanup(build.stop)

    def _run_failed_download(self, job_id="0a1b2c3d", code=ye.YOUTUBE_AUTH_REQUIRED):
        job = self.app.JobProgress(job_id, URL)
        self.app.jobs[job_id] = job
        error = ye.YouTubeDownloadError(code, AGE_MESSAGE)
        with mock.patch.object(self.app, "download_youtube_video", side_effect=error), \
                mock.patch.object(self.app, "extract_audio") as extract:
            ok = self.app.process_video(job_id, URL, highlight_video=True)
        return job, ok, extract

    def test_failed_download_marks_job_failed_with_code_and_no_downstream_work(self):
        job, ok, extract = self._run_failed_download()
        self.assertFalse(ok)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error_code, ye.YOUTUBE_AUTH_REQUIRED)
        self.assertEqual(job.error, ye.USER_MESSAGES[ye.YOUTUBE_AUTH_REQUIRED])
        extract.assert_not_called()
        for method in ("run_heavy_phase", "run_highlight_phase", "run_transcription", "run_summary"):
            getattr(self.executor, method, mock.Mock()).assert_not_called()
        self.assertNotIn("sabr", job.error.lower())

    def test_failed_job_is_recorded_on_disk_with_code_and_no_summary(self):
        job, _, _ = self._run_failed_download("1a2b3c4d")
        metadata = json.loads((job.job_output_dir / "summary_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "error")
        self.assertEqual(metadata["error_code"], ye.YOUTUBE_AUTH_REQUIRED)
        self.assertFalse((job.job_output_dir / "summary.txt").exists())

    def test_results_api_returns_422_with_code_for_user_actionable_failures(self):
        self._run_failed_download("2a2b3c4d")
        self.app.jobs.pop("2a2b3c4d")  # the next request reads the job from disk
        response = self.app.app.test_client().get("/api/results/2a2b3c4d")
        self.assertEqual(response.status_code, 422)
        body = response.get_json()
        self.assertEqual(body["code"], ye.YOUTUBE_AUTH_REQUIRED)
        self.assertIn("Upload Video tab", body["error"])

    def test_results_api_keeps_500_for_unclassified_failures(self):
        job = self.app.JobProgress("3a2b3c4d", URL)
        job.status, job.error, job.error_code = "error", "boom", None
        self.app.jobs["3a2b3c4d"] = job
        response = self.app.app.test_client().get("/api/results/3a2b3c4d")
        self.assertEqual(response.status_code, 500)

    def test_partial_summary_cannot_make_a_failed_job_look_successful(self):
        job_dir = self.out / "4a2b3c4d"
        job_dir.mkdir()
        (job_dir / "summary.txt").write_text("partial", encoding="utf-8")
        (job_dir / "summary_metadata.json").write_text(
            json.dumps({"status": "error", "error": "failed", "error_code": ye.YOUTUBE_PRIVATE}), encoding="utf-8")
        response = self.app.app.test_client().get("/api/results/4a2b3c4d")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("summary", response.get_json())

    def test_successful_upload_path_still_returns_results(self):
        job_dir = self.out / "5a2b3c4d"
        job_dir.mkdir()
        (job_dir / "summary.txt").write_text("A summary.", encoding="utf-8")
        (job_dir / "summary_metadata.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        response = self.app.app.test_client().get("/api/results/5a2b3c4d")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "completed")

    def test_history_shows_the_failed_job_as_an_error(self):
        job_dir = self.out / "6a2b3c4d"
        job_dir.mkdir()
        (job_dir / "summary_metadata.json").write_text(
            json.dumps({"status": "error", "error": "x", "error_code": ye.YOUTUBE_AUTH_REQUIRED,
                        "source": "youtube"}), encoding="utf-8")
        entry = self.app.enrich_history_entry({"job_id": "6a2b3c4d"})
        self.assertEqual(entry["status"], "error")
        self.assertFalse(entry["has_summary"])

    def test_uploaded_video_flow_is_not_affected_by_the_youtube_branch(self):
        job = self.app.JobProgress("7a2b3c4d", "/tmp/clip.mp4")
        self.app.jobs["7a2b3c4d"] = job
        with mock.patch.object(self.app, "get_local_video", side_effect=RuntimeError("stop here")) as local:
            ok = self.app.process_video("7a2b3c4d", "/tmp/clip.mp4")
        local.assert_called_once()
        self.assertFalse(ok)
        self.assertIsNone(job.error_code, "a non-YouTube failure keeps the existing message and no code")


if __name__ == "__main__":
    unittest.main()
