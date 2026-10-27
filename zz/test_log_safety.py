"""Sensitive values never reach logs, the server console, job logs, or API error text.

All secrets below are synthetic (SYNTHETIC_*). No real credential is read, printed, or used.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import log_safety as ls  # noqa: E402
from src import youtube_errors as ye  # noqa: E402

SIGNED = "https://video-bucket.s3.example.test/clip.mp4?X-Amz-Signature=SYNTHETIC_SIG_9f8e7d&X-Amz-Credential=SYNTHETIC_CRED"
TOKEN = "SYNTHETIC_TOKEN_1234567890ABCDEFGHIJKLMNOP"  # token-shaped: long, mixed letters and digits
BEARER = "Authorization: Bearer " + "SYNTHETIC_" + "BEARER_abc123"  # split so no literal matches a bearer-token pattern
AWS_ID = "AKIA" + "EXAMPLE000000000"  # shaped like an AWS key ID (AKIA + 16); synthetic, not a real key
COOKIE = "cookie=SYNTHETIC_COOKIE_value; session=SYNTHETIC_SESSION"
SECRETS = ["SYNTHETIC_SIG_9f8e7d", "SYNTHETIC_CRED", TOKEN, "SYNTHETIC_BEARER_abc123",
           "SYNTHETIC_COOKIE_value", "SYNTHETIC_SESSION"]


def _assert_clean(test, text):
    for secret in SECRETS:
        test.assertNotIn(secret, text, f"secret leaked: {secret}")
    test.assertNotIn("X-Amz-Signature=SYNTHETIC", text)


class RedactionTests(unittest.TestCase):
    def test_signed_url_keeps_host_and_path_and_drops_the_query(self):
        out = ls.redact(f"fetch failed: {SIGNED}")
        self.assertIn("https://video-bucket.s3.example.test/clip.mp4", out)
        self.assertIn("[redacted-query]", out)
        _assert_clean(self, out)

    def test_token_bearer_cookie_and_key_id_values_are_removed(self):
        out = ls.redact(f"{TOKEN} {BEARER} {COOKIE} {AWS_ID}")
        self.assertIn("[redacted-token]", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("SYNTHETIC_BEARER_abc123", out)
        self.assertNotIn("SYNTHETIC_COOKIE_value", out)
        self.assertNotIn(AWS_ID, out)
        self.assertIn("[redacted-key-id]", out)

    def test_ordinary_text_is_unchanged(self):
        text = "Transcribing 30.0 s of audio with the base model (4 segments)."
        self.assertEqual(ls.redact(text), text)

    def test_cookies_from_browser_flag_value_is_removed(self):
        self.assertNotIn("chrome", ls.redact("Use --cookies-from-browser chrome to continue"))

    def test_redaction_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("cannot convert")

        self.assertEqual(ls.redact(Hostile()), "[unreadable message redacted]")

    def test_safe_exc_keeps_the_type_and_drops_the_secrets(self):
        out = ls.safe_exc(OSError(f"cannot open {SIGNED}"))
        self.assertTrue(out.startswith("OSError: "))
        _assert_clean(self, out)


class NewPatternTests(unittest.TestCase):
    """Patterns added after review. Every value is synthetic; the test asserts the value is gone."""

    def test_json_quoted_sensitive_keys_lose_their_values(self):
        samples = {
            "token": '{"token": "SYNTHETIC_JSON_TOKEN_VALUE"}',
            "password": '{"password":"SYNTHETIC_PASSWORD_VALUE"}',
            "secret": '{ "secret" : "SYNTHETIC_SECRET_VALUE" }',
            "access_key": '{"access_key": "SYNTHETIC_ACCESS_KEY_VALUE", "region": "ap-south-1"}',
        }
        for key, text in samples.items():
            with self.subTest(key=key):
                out = ls.redact(text)
                self.assertNotIn("SYNTHETIC", out)
                self.assertIn(f'"{key}"', out, "the key stays so the message is still readable")

    def test_json_redaction_leaves_unrelated_fields_alone(self):
        text = '{"status": "error", "job_id": "0a1b2c3d", "progress": 0.35, "token": "SYNTHETIC_X_VALUE"}'
        out = ls.redact(text)
        self.assertIn('"status": "error"', out)
        self.assertIn('"job_id": "0a1b2c3d"', out)
        self.assertIn('"progress": 0.35', out)
        self.assertNotIn("SYNTHETIC_X_VALUE", out)

    def test_basic_auth_username_and_password_are_removed_host_and_path_kept(self):
        text = "connect https://synthetic_user:SYNTHETIC_PASSWORD_VALUE@api.example.test/v2/clip.mp4 failed"
        out = ls.redact(text)
        self.assertNotIn("synthetic_user", out)
        self.assertNotIn("SYNTHETIC_PASSWORD_VALUE", out)
        self.assertIn("https://[redacted]@api.example.test/v2/clip.mp4", out)

    def test_basic_auth_user_without_password_is_removed(self):
        out = ls.redact("https://SYNTHETIC_ONLY_USER@host.example.test/x")
        self.assertNotIn("SYNTHETIC_ONLY_USER", out)
        self.assertIn("host.example.test/x", out)

    def test_percent_encoded_query_delimiter_is_handled_in_both_cases(self):
        for delimiter in ("%3F", "%3f"):
            with self.subTest(delimiter=delimiter):
                text = f"https://bucket.example.test/clip.mp4{delimiter}X-Amz-Signature%3DSYNTHETIC_ENC_SIG&X-Amz-Expires%3D3600"
                out = ls.redact(text)
                self.assertNotIn("SYNTHETIC_ENC_SIG", out)
                self.assertIn("https://bucket.example.test/clip.mp4", out)

    def test_percent_encoded_sensitive_parameter_without_a_url_is_redacted(self):
        out = ls.redact("log: X-Amz-Signature%3DSYNTHETIC_ENC_SIG2 token%3DSYNTHETIC_ENC_TOK2")
        self.assertNotIn("SYNTHETIC_ENC_SIG2", out)
        self.assertNotIn("SYNTHETIC_ENC_TOK2", out)

    def test_any_query_string_is_dropped_but_host_and_path_are_kept(self):
        out = ls.redact("Downloaded https://www.youtube.com/watch?v=abc123 in 4 s")
        self.assertIn("https://www.youtube.com/watch", out)
        self.assertNotIn("abc123", out)
        self.assertIn("in 4 s", out, "ordinary text around the URL is kept")


class TracebackTests(unittest.TestCase):
    def _deep_failure(self):
        def inner(url):
            raise OSError(f"S3 refused {url} with {TOKEN}")

        def outer():
            inner(SIGNED)

        outer()

    def test_traceback_keeps_structure_without_secrets(self):
        buffer = io.StringIO()
        try:
            self._deep_failure()
        except OSError:
            with contextlib.redirect_stdout(buffer):
                print(ls.safe_traceback())
        text = buffer.getvalue()
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("_deep_failure", text)
        self.assertIn("OSError", text)
        self.assertIn("https://video-bucket.s3.example.test/clip.mp4", text)
        _assert_clean(self, text)


class PipelinePathTests(unittest.TestCase):
    def test_download_failure_console_output_is_clean(self):
        try:
            import yt_dlp

            from src import downloader as dl
        except ImportError:
            self.skipTest("yt-dlp is not part of the worker image")

        leaky = yt_dlp.utils.DownloadError(f"Unable to fetch {SIGNED} with {BEARER} and {COOKIE}")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buffer = io.StringIO()
        with mock.patch.object(dl, "get_video_meta", side_effect=leaky), mock.patch.object(dl.time, "sleep"), \
                contextlib.redirect_stdout(buffer):
            with self.assertRaises(ye.YouTubeDownloadError) as ctx:
                dl.download_youtube_video("https://www.youtube.com/watch?v=abc", Path(tmp.name), max_retries=2)
        _assert_clean(self, buffer.getvalue())
        _assert_clean(self, str(ctx.exception))
        self.assertIn("[WARN]", buffer.getvalue(), "diagnostics are kept, only the secrets are removed")

    @unittest.skipIf(not (ROOT / "app.py").exists(), "app.py is not part of the worker image")
    def test_processing_failure_job_log_console_and_api_are_clean(self):
        from tests.test_progress_fixes import FullRunTestBase, app_module

        class Probe(FullRunTestBase):
            def runTest(self):
                self.executor.run_heavy_phase.side_effect = OSError(f"cannot read {SIGNED} {COOKIE}")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    job, ok = self._run(highlight=False)
                self.assertFalse(ok)
                job_log = "\n".join(job.log_lines)
                api_body = app_module.app.test_client().get(f"/api/results/{job.job_id}").get_data(as_text=True)
                for text in (buffer.getvalue(), job_log, job.error, api_body, job.to_dict()["error"]):
                    _assert_clean(self, text)

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [], result.errors + result.failures)

    @unittest.skipIf(not (ROOT / "app.py").exists(), "app.py is not part of the worker image")
    def test_highlight_failure_is_redacted_in_log_and_persisted_status(self):
        from tests.test_progress_fixes import FullRunTestBase

        class Probe(FullRunTestBase):
            def runTest(self):
                self.executor.run_highlight_phase.side_effect = RuntimeError(f"render read {SIGNED} {TOKEN}")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    job, _ = self._run(highlight=True)
                _assert_clean(self, "\n".join(job.log_lines))
                _assert_clean(self, buffer.getvalue())

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [], result.errors + result.failures)

    def test_failed_language_message_is_redacted_before_it_is_saved(self):
        from src.highlight_pipeline import _write_lang_failure

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        job_dir = Path(tmp.name)
        _write_lang_failure(job_dir, "hi", f"render failed for {SIGNED}", failure_kind="error")
        saved = (job_dir / "highlights" / "highlight_status.json").read_text(encoding="utf-8")
        _assert_clean(self, saved)
        self.assertIn("could not be created", saved)  # the user sees neutral text; the detail is logged


class CallSiteTests(unittest.TestCase):
    """The places fixed after review: each is driven through its failure path with synthetic secrets."""

    def _capture(self, fn):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            result = fn()
        return result, buffer.getvalue()

    def test_lease_release_warning_is_sanitised(self):
        from src import endpoint_capacity as ec

        messages = []
        with mock.patch.object(ec, "_paths", side_effect=RuntimeError(f"cannot open {SIGNED} with {TOKEN}")):
            ec.release_lease("EP1", "tok", messages.append)
        self.assertTrue(messages, "the warning is still written")
        self.assertIn("RuntimeError", messages[0])
        _assert_clean(self, "\n".join(messages))

    def test_capacity_status_text_is_sanitised(self):
        from src import endpoint_capacity as ec

        with mock.patch.object(ec, "startup_recovery", side_effect=None):
            pass
        with mock.patch("src.runpod_serverless.patch_serverless_endpoint",
                        side_effect=RuntimeError(f"HTTP 500 at {SIGNED}")):
            result = ec._set_capacity_zero("EP1")
        self.assertFalse(result.ok)
        _assert_clean(self, result.message)

    def test_google_retry_warning_and_final_error_are_sanitised(self):
        from src.translators import google_backend as gb

        error = urllib.error.URLError(f"connection to {SIGNED} reset with {TOKEN}")
        buffer = io.StringIO()
        with mock.patch.object(gb, "_one_http_translate", side_effect=error), mock.patch.object(gb.time, "sleep"), \
                contextlib.redirect_stderr(buffer):
            with self.assertRaises(gb.GoogleTranslateError) as ctx:
                gb.google_translate_text("hello", "hi", throttle=False)
        _assert_clean(self, buffer.getvalue())
        _assert_clean(self, str(ctx.exception))
        self.assertIn("[WARN] Google", buffer.getvalue(), "the diagnostic line is kept")

    def test_downloader_attempt_error_text_is_sanitised(self):
        try:
            import yt_dlp
            from src import downloader as dl
        except ImportError:
            self.skipTest("yt-dlp is not part of the worker image")
        with mock.patch.object(yt_dlp, "YoutubeDL") as ydl_cls:
            ydl_cls.return_value.__enter__.return_value.download.side_effect = yt_dlp.utils.DownloadError(
                f"HTTP Error 403 for {SIGNED} with {BEARER}")
            text = dl._try_download("https://www.youtube.com/watch?v=abc", {})
        self.assertIsNotNone(text)
        _assert_clean(self, text)
        self.assertIn("HTTP Error 403", text)

    @unittest.skipUnless((ROOT / "app.py").exists(), "app.py is not part of the worker image")
    def test_highlight_store_error_response_is_sanitised(self):
        import app as app_module
        from src import s3_video_store as s3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "output"
        (out / "0a1b2c3d").mkdir(parents=True)
        error = s3.HighlightStoreError("s3_access", f"S3 refused the request at {SIGNED} with {TOKEN}")
        with mock.patch.object(app_module, "OUTPUT_DIR", out), \
                mock.patch.object(s3, "has_stored_highlight", return_value=True), \
                mock.patch.object(s3, "upload_failed_for_lang", return_value=False), \
                mock.patch.object(s3, "highlight_url", side_effect=error), \
                mock.patch.object(s3, "highlight_store", return_value=None):
            response = app_module.app.test_client().get("/stream/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 503)
        _assert_clean(self, response.get_data(as_text=True))


# Static guard: every logging call and every JSON error body must go through redaction. The check reads whole
# statements, so a call that spans several lines is judged as one piece. A new raw interpolation fails this test.
LOG_CALL = re.compile(r"(?<![A-Za-z0-9_])(print|add_log|log|warn|warning|error|info|debug|exception|logger\.\w+)\s*\(")
RAW_EXC = re.compile(r"\{(e|exc|err)\}|\{type\((e|exc|err)\)\.__name__\}: \{(e|exc|err)\}|\bstr\((e|exc|err)\)|\{(e|exc|err)!r\}|\brepr\((e|exc|err)\)")
SAFE = re.compile(r"\bsafe_exc\(|\bredact\(|\bsafe_traceback\(")
TRACEBACK = re.compile(r"traceback\.(print_exc|format_exc|print_exception|format_exception)\(")


def _statement(lines, start, limit=25):
    """The logging call starting at lines[start], up to the line where its parentheses balance."""
    depth = 0
    collected = []
    for line in lines[start:start + limit]:
        code = line.split("#", 1)[0] if '"' not in line and "'" not in line else line
        collected.append(line)
        depth += code.count("(") - code.count(")")
        if depth <= 0:
            break
    return "\n".join(collected)


def find_unsafe_logging(text: str) -> list[int]:
    """1-based line numbers of logging statements that interpolate a raw exception, plus raw traceback calls."""
    lines = text.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if TRACEBACK.search(line):
            offenders.append(i + 1)
            continue
        if LOG_CALL.search(line):
            statement = _statement(lines, i)
            if RAW_EXC.search(statement) and not SAFE.search(statement):
                offenders.append(i + 1)
    return offenders


def _repo_sources():
    files = [ROOT / "app.py", ROOT / "main.py", ROOT / "download_transcript.py", ROOT / "scripts" / "check_video_s3.py"]
    files += sorted((ROOT / "src").glob("*.py")) + sorted((ROOT / "src" / "translators").glob("*.py"))
    return [f for f in files if f.is_file() and f.name != "log_safety.py"]


class StaticGuardTests(unittest.TestCase):
    def test_no_logging_statement_interpolates_a_raw_exception(self):
        offenders = []
        for path in _repo_sources():
            for number in find_unsafe_logging(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_detector_catches_multi_line_and_missed_shapes(self):
        cases = {
            "print continuation {exc}": 'print(\n    f"[WARN] failed ({exc}); retry",\n    file=sys.stderr,\n)\n',
            "log continuation typed": 'log(\n    f"WARNING: lease ({type(exc).__name__}: {exc}); "\n    f"expires"\n)\n',
            "logger multi-line": 'logger.warning(\n    "failed: %s",\n    f"{e}",\n)\n',
            "add_log str(e)": 'job.add_log(\n    f"Failed: {str(e)}"\n)\n',
            "repr form": 'print(f"oops {e!r}")\n',
            "traceback print_exc": "import traceback\ntraceback.print_exc()\n",
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                self.assertTrue(find_unsafe_logging(source), f"not detected: {name}")

    def test_detector_accepts_safe_and_harmless_code(self):
        cases = {
            "safe_exc continuation": 'print(\n    f"failed: {safe_exc(exc)}",\n    file=sys.stderr,\n)\n',
            "redact str": 'job.add_log(\n    f"Failed: {redact(str(e))}"\n)\n',
            "comment mentions exc": '# print(f"{exc}") was the old form\nvalue = 1\n',
            "non-log call with e": 'result = build(\n    f"{e}",\n    flag=True,\n)\n',
            "safe_traceback": "print(safe_traceback())\n",
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                self.assertEqual(find_unsafe_logging(source), [], f"false positive: {name}")

    def test_no_json_error_body_returns_raw_exception_text(self):
        offenders = []
        for path in [ROOT / "app.py"]:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if re.search(r'"error"\s*:\s*str\(', line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
