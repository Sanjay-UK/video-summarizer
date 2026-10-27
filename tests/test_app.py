"""Flask-level tests: corrupted input, mocked translation, and results retrieved through the app's routes.

Builds on tests/test_video_pipeline_integration.py, which is imported for its synthetic video, fake Whisper/Ollama/TTS
stand-ins and environment patches. The real process_video pipeline and the real Flask routes (app.test_client()) run.

Replaced (external services only):
- Google Translate: google_translate_batch is mocked and the HTTP layer is guarded, so no call reaches Google.
- NLLB (local translation model): mocked to fail, so the fallback chain ends without loading a model.
- Whisper, Ollama, edge-tts: the same stand-ins as test_video_pipeline_integration.py.
- RunPod, GPU workers, S3: not used. The runtime is patched to local, and S3 is off unless VIDEO_S3_ENABLED is set.

Not covered: YouTube download, translation quality or Hindi speech, RunPod workers, and browser behaviour.
The run-based classes need an FFmpeg build with libass (the subtitles filter) and are skipped without one.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Most modules use this placeholder. The first setdefault wins across the suite, and the live-status tests need a non-empty key.
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

import app as app_module  # noqa: E402
from src import highlight_jobs as hj  # noqa: E402
from src.highlight_assembler import HIGHLIGHT_NARRATION_FILE  # noqa: E402
from src.highlight_pipeline import record_unrecorded_highlight_failures  # noqa: E402
from src.highlight_translate import LanguageTranslationOutcome  # noqa: E402
from src.user_messages import GENERIC_HIGHLIGHT_FAILURE  # noqa: E402
from src.summary_translate import summary_text_for_language, translate_summary_for_languages  # noqa: E402
from src.translators.google_backend import GoogleTranslateError  # noqa: E402

from test_video_pipeline_integration import (  # noqa: E402
    FFMPEG,
    SUMMARY,
    FakeWhisperModel,
    _ffprobe,
    _make_video,
    _start_environment,
)

HINDI_SUMMARY = "चावल को धोकर पकाएँ, फिर सिरका, चीनी और नमक से स्वाद दें, और हाथ से आकार दें।"
NOT_A_VIDEO = b"this is plain text, not an MP4 container\n" * 200
SRT_CUE = re.compile(r"(\d+)\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})\n(.+?)(?:\n\n|\Z)", re.S)


def _google_returns_hindi(texts, target_lang, throttle=True):
    """Stands in for Google Translate: one Devanagari sentence per input text."""
    return [HINDI_SUMMARY for _ in texts]


def _nllb_unavailable(locale, english_texts, **kwargs):
    """Stands in for the local NLLB model, which is not loaded in tests."""
    return LanguageTranslationOutcome(
        language=locale.code,
        spoken_texts=[None] * len(english_texts),
        ok=False,
        message="NLLB is not available in this test",
        failure_kind="error",
        translator_backend=kwargs.get("backend_name", "nllb"),
    )


def _offline_translation(stack: contextlib.ExitStack, google_behaviour):
    """Replace Google and NLLB. Returns (google batch mock, nllb mock) so tests can inspect calls."""
    google = stack.enter_context(
        mock.patch("src.highlight_translate.google_translate_batch", side_effect=google_behaviour)
    )
    stack.enter_context(
        mock.patch("src.translators.google_backend._one_http_translate",
                   side_effect=AssertionError("a live HTTP call to Google was attempted"))
    )
    nllb = stack.enter_context(
        mock.patch("src.highlight_translate._translate_sequential_for_backend", side_effect=_nllb_unavailable)
    )
    return google, nllb


class _InlineThread:
    """Runs the target inside start(), so a route test sees the finished job without waiting on a background thread."""

    def __init__(self, target, args=(), daemon=None, **_):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class _PipelineTestBase(unittest.TestCase):
    """Temp output folder, patched external services, and a Google/NLLB stand-in that tests can re-configure."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "output"
        self.out.mkdir()
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.jobs = {}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        _start_environment(stack, self.out, self.jobs, FakeWhisperModel)
        # self.google.side_effect and self.nllb.side_effect can be changed per test.
        self.google, self.nllb = _offline_translation(stack, _google_returns_hindi)
        self.client = app_module.app.test_client()


@unittest.skipUnless(shutil.which("ffmpeg"), "the audio step calls 'ffmpeg' by name, so it must be on PATH")
class CorruptedInputTests(_PipelineTestBase):
    """A file that is not a video must fail the job with a clear error and leave no completed outputs."""

    JOB_ID = "b00c0001"

    def test_corrupted_video_fails_the_job_and_writes_no_results(self):
        broken = self.root / "broken.mp4"
        broken.write_bytes(NOT_A_VIDEO)
        job = app_module.JobProgress(self.JOB_ID, str(broken))
        self.jobs[self.JOB_ID] = job

        ok = app_module.process_video(self.JOB_ID, str(broken), highlight_video=True)

        self.assertFalse(ok)
        snap = job.to_dict()
        self.assertEqual(snap["status"], "error")
        self.assertEqual(snap["error"], "FFmpeg extraction failed")
        self.assertEqual(snap["progress"], 0.15, "the job should stop at the extraction step")

        job_dir = self.out / self.JOB_ID
        for name in ("transcript.txt", "segments.json", "summary.txt", "summary.srt", "audio_explanation.mp3"):
            self.assertFalse((job_dir / name).exists(), f"{name} must not exist after a failed extraction")
        self.assertEqual(list(job_dir.glob("audio.*")), [], "no audio file may be left from a failed extraction")
        meta = json.loads((job_dir / "summary_metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "error")
        self.assertEqual(meta["error"], "FFmpeg extraction failed")

    def test_upload_route_reports_a_corrupted_video_as_a_failed_job(self):
        inline = SimpleNamespace(RLock=threading.RLock, Thread=_InlineThread)
        with mock.patch.object(app_module, "UPLOAD_DIR", self.uploads), mock.patch.object(app_module, "threading", inline):
            resp = self.client.post(
                "/api/summarize",
                data={"file": (io.BytesIO(NOT_A_VIDEO), "broken.mp4"), "compression": "medium"},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            job_id = resp.get_json()["job_id"]
            self.assertTrue((self.uploads / "broken.mp4").is_file(), "the upload should be saved before processing")

            progress = self.client.get(f"/api/progress/{job_id}")
            self.assertEqual(progress.status_code, 200)
            self.assertEqual(progress.get_json()["status"], "error")

            results = self.client.get(f"/api/results/{job_id}")
            self.assertEqual(results.status_code, 500)
            self.assertEqual(results.get_json()["error"], "FFmpeg extraction failed")

    def test_unsupported_upload_type_is_refused_before_any_job_starts(self):
        with mock.patch.object(app_module, "UPLOAD_DIR", self.uploads):
            resp = self.client.post(
                "/api/summarize",
                data={"file": (io.BytesIO(b"hello"), "notes.txt")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(".txt", resp.get_json()["error"])
        self.assertEqual(self.jobs, {}, "no job may be created for a refused upload")
        self.assertFalse((self.uploads / "notes.txt").exists(), "a refused upload must not be saved")


class RecordUnrecordedHighlightFailuresTests(unittest.TestCase):
    """The helper marks only requested languages that have neither a file nor a record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job = Path(tmp.name) / "job"
        self.job.mkdir()

    def _status(self, payload):
        path = self.job / "highlights" / "highlight_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_requested_languages_without_output_or_record_become_failed(self):
        self._status({"requested_languages": ["en", "hi"], "languages": {}})

        record_unrecorded_highlight_failures(self.job, ["en", "hi"], "boom", failure_kind="error")

        self.assertEqual(hj.failed_highlight_languages(self.job), ["en", "hi"])
        rows = {row["code"]: row for row in hj.list_highlight_language_status(self.job)}
        self.assertEqual(rows["en"]["message"], "boom")
        self.assertEqual(rows["en"]["failure_kind"], "error")

    def test_a_language_with_a_file_or_a_record_is_left_alone(self):
        self._status({"requested_languages": ["en", "hi", "ta"],
                      "languages": {"hi": {"status": "success"}, "ta": {"status": "failed", "message": "kept"}}})
        out = self.job / "highlights" / "en" / "highlight_en.mp4"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"mp4")

        record_unrecorded_highlight_failures(self.job, ["en", "hi", "ta"], "boom", failure_kind="error")

        languages = json.loads((self.job / "highlights" / "highlight_status.json").read_text())["languages"]
        self.assertNotIn("en", languages, "a finished file is never overwritten with a failure record")
        self.assertEqual(languages["hi"], {"status": "success"}, "an existing record is never replaced")
        self.assertEqual(languages["ta"]["message"], "kept")


@unittest.skipUnless(shutil.which("ffmpeg"), "the audio step calls 'ffmpeg' by name, so it must be on PATH")
class HighlightStepCrashTests(_PipelineTestBase):
    """If the highlight step raises, the job still completes, and the requested language shows as failed with a retry,
    not as 'Not generated' with no reason."""

    JOB_ID = "b00c0003"

    def test_crash_in_the_highlight_step_records_the_language_as_failed(self):
        video = self.root / "lecture.mp4"
        _make_video(video)
        job = app_module.JobProgress(self.JOB_ID, str(video))
        self.jobs[self.JOB_ID] = job
        crash = RuntimeError("ffmpeg exploded at /private/tmp/secret_path")

        with mock.patch("src.highlight_pipeline.run_highlight_pipeline", side_effect=crash):
            ok = app_module.process_video(self.JOB_ID, str(video), highlight_video=True)

        job_dir = self.out / self.JOB_ID
        self.assertTrue(ok, "the summary still completes when the highlight step fails")
        self.assertEqual(hj.failed_highlight_languages(job_dir), ["en"], "the retry list must include the failed language")
        results = self.client.get(f"/api/results/{self.JOB_ID}").get_json()
        self.assertFalse(results["highlight_video"])
        [row] = results["highlight_language_status"]
        self.assertEqual(row["code"], "en")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["message"], GENERIC_HIGHLIGHT_FAILURE)
        self.assertNotIn("secret_path", json.dumps(results), "raw exception text must not reach the user")


class TranslationWithMockedGoogleTests(_PipelineTestBase):
    """Summary translation with Google and NLLB replaced by stand-ins. The translated text is checked, not just its presence."""

    JOB_ID = "b00c0002"

    def setUp(self):
        super().setUp()
        self.job_dir = self.out / self.JOB_ID
        self.job_dir.mkdir()

    def _translate(self):
        return translate_summary_for_languages(self.job_dir, SUMMARY, ["en", "hi"])

    def test_google_output_in_the_target_script_becomes_the_hindi_summary(self):
        payload = self._translate()

        entry = payload["languages"]["hi"]
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["translator"], "google")
        self.assertEqual(entry["text"], HINDI_SUMMARY)
        self.assertEqual((self.job_dir / "summary_hi.txt").read_text(encoding="utf-8"), HINDI_SUMMARY)
        self.assertEqual(summary_text_for_language(self.job_dir, "hi"), HINDI_SUMMARY)

        texts, target = self.google.call_args.args
        self.assertEqual(target, "hi")
        self.assertIn("vinegar, sugar and salt", " ".join(texts), "Google must receive the English summary")
        self.nllb.assert_not_called()

    @unittest.expectedFailure
    def test_english_text_from_google_is_not_accepted_as_a_hindi_summary(self):
        """KNOWN GAP (expected failure). When Google returns English, the Indic finalizer transliterates it into
        Devanagari (term_protection.normalize_stray_latin_with_log), the quality check passes, and the job records
        'success'. The saved Hindi text is English sounds in Devanagari. Whether that should count as a translation is
        a product decision. Remove this decorator once the validation rejects it."""
        self.google.side_effect = lambda texts, lang, throttle=True: list(texts)  # returns English, not Hindi
        payload = self._translate()

        self.assertEqual(payload["languages"]["hi"]["status"], "failed")
        self.assertFalse((self.job_dir / "summary_hi.txt").exists())
        self.assertIsNone(summary_text_for_language(self.job_dir, "hi"))
        self.nllb.assert_called()  # the English text was rejected, so the chain moved on to the next backend

    def test_google_outage_is_recorded_as_failed_and_the_route_returns_404(self):
        self.google.side_effect = GoogleTranslateError("Google Translate returned HTTP 503")
        payload = self._translate()

        self.assertEqual(payload["languages"]["hi"]["status"], "failed")
        self.assertFalse((self.job_dir / "summary_hi.txt").exists())
        resp = self.client.get(f"/download/summary/{self.JOB_ID}/hi")
        self.assertEqual(resp.status_code, 404, "a failed translation must not be downloadable as a file")


@unittest.skipUnless(FFMPEG, "needs an FFmpeg build with libass (subtitles filter) on this machine")
class _CompletedRunBase(unittest.TestCase):
    """One real process_video run per class. Every test reads the saved files or the route responses for that run."""

    JOB_ID = ""
    RUN_OPTIONS: dict = {}

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cls.video = root / "lecture.mp4"
        _make_video(cls.video)
        cls.out = root / "output"
        cls.out.mkdir()
        cls.jobs = {}
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        _start_environment(stack, cls.out, cls.jobs, FakeWhisperModel)
        _offline_translation(stack, _google_returns_hindi)

        cls.job = app_module.JobProgress(cls.JOB_ID, str(cls.video))
        cls.jobs[cls.JOB_ID] = cls.job
        cls.ok = app_module.process_video(cls.JOB_ID, str(cls.video), **cls.RUN_OPTIONS)
        cls.job_dir = cls.out / cls.JOB_ID
        cls.client = app_module.app.test_client()
        if not cls.ok:
            raise AssertionError("pipeline failed: " + " | ".join(cls.job.log_lines[-5:]))


class CompletedJobRoutesTests(_CompletedRunBase):
    """Results of a finished English + Hindi job, retrieved through /api/results and the /download routes."""

    JOB_ID = "b00c0003"
    # Requesting Hindi also renders the highlight (app.py: wants_highlight_now), so the English highlight exists here.
    RUN_OPTIONS = {"highlight_languages": "en,hi"}

    def test_results_route_returns_the_completed_payload_with_the_hindi_summary(self):
        resp = self.client.get(f"/api/results/{self.JOB_ID}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("vinegar, sugar and salt", body["summary"])
        self.assertEqual(body["summary_length"], len(body["summary"]))
        self.assertTrue(body["has_audio"])
        self.assertTrue(body["highlight_video"])
        self.assertEqual(body["summary_translations"], {"hi": HINDI_SUMMARY})
        rows = {row["code"]: row for row in body["summary_translation_status"]}
        self.assertEqual((rows["hi"]["status"], rows["hi"]["translator"], rows["hi"]["available"]),
                         ("success", "google", True))

    def test_results_route_rebuilds_the_same_content_from_disk_after_a_restart(self):
        live = self.client.get(f"/api/results/{self.JOB_ID}").get_json()
        with mock.patch.object(app_module, "jobs", {}):
            resp = self.client.get(f"/api/results/{self.JOB_ID}")
        self.assertEqual(resp.status_code, 200)
        disk = resp.get_json()
        for key in ("status", "summary", "summary_translations", "has_audio", "highlight_video"):
            self.assertEqual(disk[key], live[key], key)

    def test_summary_downloads_return_the_english_and_hindi_files(self):
        en = self.client.get(f"/download/summary/{self.JOB_ID}")
        self.addCleanup(en.close)
        self.assertEqual(en.status_code, 200)
        self.assertIn("summary.txt", en.headers["Content-Disposition"])
        self.assertEqual(en.data, (self.job_dir / "summary.txt").read_bytes())
        self.assertIn(b"vinegar, sugar and salt", en.data)

        hi = self.client.get(f"/download/summary/{self.JOB_ID}/hi")
        self.addCleanup(hi.close)
        self.assertEqual(hi.status_code, 200)
        self.assertIn("summary_hi.txt", hi.headers["Content-Disposition"])
        self.assertEqual(hi.data.decode("utf-8"), HINDI_SUMMARY)

    def test_subtitle_download_is_a_valid_srt_in_time_order(self):
        resp = self.client.get(f"/download/subtitles/{self.JOB_ID}")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary.srt", resp.headers["Content-Disposition"])
        text = resp.data.decode("utf-8")
        self.assertEqual(text, (self.job_dir / "summary.srt").read_text(encoding="utf-8"))

        cues = SRT_CUE.findall(text)
        self.assertTrue(cues, "the subtitle file has no parseable cues")
        self.assertEqual([int(c[0]) for c in cues], list(range(1, len(cues) + 1)), "cue numbers must run 1..n")
        starts = []
        for c in cues:
            start = int(c[1]) * 3600 + int(c[2]) * 60 + int(c[3]) + int(c[4]) / 1000
            end = int(c[5]) * 3600 + int(c[6]) * 60 + int(c[7]) + int(c[8]) / 1000
            self.assertLess(start, end, f"cue {c[0]} ends before it starts")
            starts.append(start)
        self.assertEqual(starts, sorted(starts), "cues must be in time order")
        self.assertIn("vinegar", " ".join(c[9] for c in cues).lower())

    def test_default_audio_download_is_the_english_highlight_narration(self):
        """With English highlights requested, the default audio is the narration track of the highlight video."""
        resp = self.client.get(f"/download/audio/{self.JOB_ID}")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(HIGHLIGHT_NARRATION_FILE, resp.headers["Content-Disposition"])
        track = self.job_dir / "highlights" / "en" / HIGHLIGHT_NARRATION_FILE
        self.assertEqual(resp.data, track.read_bytes())
        probe = _ffprobe(track)
        self.assertGreater(float(probe["format"]["duration"]), 1.0)

    def test_source_summary_audio_download_is_the_full_summary_narration(self):
        resp = self.client.get(f"/download/audio/{self.JOB_ID}?source=summary")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        narration = self.job_dir / "audio_explanation.mp3"
        self.assertEqual(resp.data, narration.read_bytes())
        probe = _ffprobe(narration)
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertGreater(float(probe["format"]["duration"]), 1.0)

    def test_unknown_job_and_unknown_language_are_refused(self):
        missing = self.client.get("/api/results/ffffffff")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"], "Job not found")

        bad_lang = self.client.get(f"/download/summary/{self.JOB_ID}/xx")
        self.addCleanup(bad_lang.close)
        self.assertEqual(bad_lang.status_code, 400)


class HighlightRouteTests(_CompletedRunBase):
    """The rendered English highlight video, retrieved through its download route and the results payload."""

    JOB_ID = "b00c0004"
    RUN_OPTIONS = {"highlight_video": True}

    def test_highlight_download_is_the_rendered_720p_video_with_audio(self):
        resp = self.client.get(f"/download/highlight/{self.JOB_ID}")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "video/mp4")
        self.assertIn("highlight_en.mp4", resp.headers["Content-Disposition"])
        highlight = self.job_dir / "highlights" / "en" / "highlight_en.mp4"
        self.assertEqual(resp.data, highlight.read_bytes())

        probe = _ffprobe(highlight)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"]
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        self.assertEqual((video[0]["width"], video[0]["height"]), (1280, 720))
        self.assertEqual(len(audio), 1)

    def test_results_payload_lists_the_english_highlight_for_download(self):
        body = self.client.get(f"/api/results/{self.JOB_ID}").get_json()
        self.assertTrue(body["highlight_video"])
        self.assertEqual([h["code"] for h in body["highlight_languages"]], ["en"])


if __name__ == "__main__":
    unittest.main()
