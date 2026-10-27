"""S3-stored highlights: stable keys in history, fresh one-hour links on every request, and safe failures.

No AWS calls are made. A fake S3 client records uploads and signing requests. Each test checks an observable
result: the saved record, the signing arguments, the HTTP response, or the executor's output.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from botocore.exceptions import ClientError  # noqa: E402

from src import s3_video_store as s3  # noqa: E402

BUCKET = s3.DEFAULT_BUCKET
_REAL_VIDEO_STORE = s3.VideoStore
LEAKY_KEY = "SYNTHETIC-TEST-KEY-NOT-REAL"  # synthetic, used to prove messages never echo credentials


VALID_KEY = "highlights/" + "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12 + "/en.mp4"


def _client_error(code: str, op: str = "HeadObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class FakeS3:
    """Records uploads, head checks, and presign calls. Errors can be injected per operation."""

    def __init__(self):
        self.objects: dict[str, int] = {}
        self.uploads: list[tuple[str, str, dict]] = []
        self.deleted: list[str] = []
        self.presign_calls: list[dict] = []
        self.upload_error: Exception | None = None
        self.upload_error_prefix: str | None = None  # limit upload_error to keys with this prefix
        self.head_error: Exception | None = None
        self.presign_error: Exception | None = None
        self.size_override: int | None = None
        self._n = 0

    def upload_file(self, path, bucket, key, ExtraArgs):
        if self.upload_error and (self.upload_error_prefix is None or key.startswith(self.upload_error_prefix)):
            raise self.upload_error
        self.objects[key] = Path(path).stat().st_size
        self.uploads.append((bucket, key, ExtraArgs))

    def head_object(self, Bucket, Key):
        if self.head_error:
            raise self.head_error
        if Key not in self.objects:
            raise _client_error("404")
        return {"ContentLength": self.size_override or self.objects[Key], "ContentType": "video/mp4"}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        if self.presign_error:
            raise self.presign_error
        self._n += 1
        self.presign_calls.append({"op": op, "Params": dict(Params), "ExpiresIn": ExpiresIn})
        # A different signature each call, like real presigning. The nonce stands in for X-Amz-Signature.
        return (
            f"https://{Params['Bucket']}.s3.{s3.DEFAULT_REGION}.example/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake{self._n}"
        )

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


class FakeCreds:
    def __init__(self, token=None, expiry=None):
        self.token = token
        self._expiry_time = expiry


class FakeSession:
    def __init__(self, creds):
        self._creds = creds

    def get_credentials(self):
        return self._creds


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job_dir = Path(self.tmp.name) / "job"
        self.job_dir.mkdir()
        self.mp4 = self.job_dir / "highlight_en.mp4"
        self.mp4.write_bytes(b"x" * 4096)
        self.fake = FakeS3()
        # Settings deliberately ask for 2 h to prove highlight links ignore VIDEO_S3_PRESIGN_EXPIRES.
        self.store = s3.VideoStore(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "7200"}), client=self.fake)

    def _upload(self, lang="en"):
        return s3.upload_highlight(self.job_dir, lang, self.mp4, self.store)


class UploadRecordTests(StoreTestBase):
    def test_upload_records_stable_key_and_metadata_not_a_url(self):
        record = self._upload()
        self.assertEqual(record["status"], "uploaded")
        self.assertEqual(record["bucket"], BUCKET)
        self.assertEqual(record["size_bytes"], 4096)
        self.assertEqual(record["content_type"], "video/mp4")
        self.assertTrue(s3._HIGHLIGHT_KEY_RE.match(record["key"]), record["key"])
        self.assertIn("uploaded_at", record)
        raw = (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).read_text(encoding="utf-8")
        self.assertNotIn("X-Amz", raw)
        self.assertNotIn("https://", raw)
        self.assertEqual(self.fake.uploads[0][2]["ServerSideEncryption"], "AES256")
        self.assertEqual(self.fake.uploads[0][2]["ContentType"], "video/mp4")

    def test_upload_failure_is_recorded_not_raised(self):
        self.fake.upload_error = _client_error("AccessDenied", "PutObject")
        record = s3.upload_highlight(self.job_dir, "en", self.mp4, self.store)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "AccessDenied")
        self.assertFalse(s3.has_stored_highlight(self.job_dir, "en"))

    def test_size_mismatch_is_a_failed_upload(self):
        self.fake.size_override = 1
        record = s3.upload_highlight(self.job_dir, "en", self.mp4, self.store)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_code"], "size_mismatch")

    def test_stored_langs_lists_only_verified_uploads(self):
        self._upload("en")
        self.fake.upload_error = _client_error("AccessDenied", "PutObject")
        s3.upload_highlight(self.job_dir, "hi", self.mp4, self.store)
        self.assertEqual(s3.stored_highlight_langs(self.job_dir), ["en"])


class FreshLinkTests(StoreTestBase):
    def test_link_is_signed_for_exactly_one_hour_even_if_settings_say_more(self):
        self._upload()
        payload = s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(payload["expires_in"], 3600)
        call = self.fake.presign_calls[-1]
        self.assertEqual(call["ExpiresIn"], 3600)
        self.assertEqual(call["Params"]["Bucket"], BUCKET)
        self.assertIn("X-Amz-Expires=3600", payload["url"])

    def test_each_request_gets_a_different_link(self):
        self._upload()
        first = s3.highlight_url(self.job_dir, "en", self.store)["url"]
        second = s3.highlight_url(self.job_dir, "en", self.store)["url"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.fake.presign_calls), 2)

    def test_download_link_sets_attachment_disposition_and_content_type(self):
        self._upload()
        s3.highlight_url(self.job_dir, "en", self.store, attachment=True)
        params = self.fake.presign_calls[-1]["Params"]
        self.assertTrue(params["ResponseContentDisposition"].startswith("attachment"))
        self.assertEqual(params["ResponseContentType"], "video/mp4")

    def test_nothing_is_written_when_a_link_is_issued(self):
        self._upload()
        before = (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).read_text(encoding="utf-8")
        s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual((self.job_dir / s3.HIGHLIGHT_RECORD_NAME).read_text(encoding="utf-8"), before)

    def test_not_uploaded_language_is_not_in_s3(self):
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "not_in_s3")
        self.assertEqual(self.fake.presign_calls, [])


class ProductionStoreCredentialTests(unittest.TestCase):
    """Regression: the expiry guard must check the live session. highlight_store() builds a new store per request,
    so the session only exists after the client is created. Checking before that skipped the guard entirely."""

    def _job_with_record(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        job = Path(tmp.name) / "job"
        job.mkdir()
        s3.write_highlight_record(job, "en", {"bucket": BUCKET, "key": VALID_KEY, "status": "uploaded"})
        return job

    def _boto_session(self, creds):
        fake = FakeS3()
        session = mock.Mock()
        session.get_credentials.return_value = creds
        session.client.return_value = fake
        return session, fake

    def test_temporary_credentials_ending_in_20_minutes_are_refused_through_highlight_store(self):
        job = self._job_with_record()
        soon = datetime.now(timezone.utc) + timedelta(minutes=20)
        session, fake = self._boto_session(FakeCreds(token="session-token", expiry=soon))
        with mock.patch("boto3.Session", return_value=session), mock.patch.dict(os.environ, {"VIDEO_S3_PROFILE": "video-summarizer"}):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.highlight_url(job, "en", s3.highlight_store())
        self.assertEqual(ctx.exception.code, "credentials_expire_early")
        self.assertEqual(fake.presign_calls, [], "no one-hour link may be signed")

    def test_long_term_credentials_through_highlight_store_are_issued(self):
        job = self._job_with_record()
        session, fake = self._boto_session(FakeCreds(token=None, expiry=None))
        fake.objects[VALID_KEY] = 1
        with mock.patch("boto3.Session", return_value=session):
            payload = s3.highlight_url(job, "en", s3.highlight_store())
        self.assertEqual(payload["expires_in"], 3600)
        self.assertEqual(len(fake.presign_calls), 1)


class HighlightValidationTests(StoreTestBase):
    def test_unsupported_language_is_refused_inside_highlight_url(self):
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "zz", self.store)
        self.assertEqual(ctx.exception.code, "invalid_language")
        self.assertEqual(self.fake.presign_calls, [])


class SigningHostTests(unittest.TestCase):
    """Regression: the real client must presign against the regional host. The global host made S3 answer
    SignatureDoesNotMatch for this bucket (ap-south-1), so no presigned link could be used."""

    def test_presigned_link_uses_the_regional_host_with_sigv4(self):
        env = {"AWS_ACCESS_KEY_ID": "SYNTHETIC-TEST-ACCESS", "AWS_SECRET_ACCESS_KEY": "synthetic-test-secret-not-real"}
        settings = s3.settings_from_env()
        settings["profile"] = None  # use the env credentials above, not a real profile
        with mock.patch.dict(os.environ, env):
            store = s3.VideoStore(settings)
            url = store.presign_get("highlights/x/en.mp4", expires=3600)
        host = url.split("/")[2]
        self.assertEqual(host, f"{BUCKET}.s3.{s3.DEFAULT_REGION}.amazonaws.com")
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-Expires=3600", url)


class LegacyAndTamperedRecordTests(StoreTestBase):
    def _write_raw(self, record):
        (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).write_text(
            json.dumps({"languages": {"en": record}}), encoding="utf-8"
        )

    def test_old_url_in_a_record_is_dropped_and_never_returned(self):
        self._upload()
        legacy_url = "https://old.example/expired?X-Amz-Signature=stale"
        raw = json.loads((self.job_dir / s3.HIGHLIGHT_RECORD_NAME).read_text(encoding="utf-8"))
        raw["languages"]["en"]["url"] = legacy_url
        raw["languages"]["en"]["presigned_url"] = legacy_url
        (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).write_text(json.dumps(raw), encoding="utf-8")
        self.assertNotIn("url", s3.read_highlight_records(self.job_dir)["en"])
        payload = s3.highlight_url(self.job_dir, "en", self.store)
        self.assertNotEqual(payload["url"], legacy_url)

    def test_record_rewrite_drops_legacy_url_fields(self):
        self._write_raw({"bucket": BUCKET, "key": "highlights/x", "status": "uploaded", "url": "https://old"})
        s3.write_highlight_record(self.job_dir, "hi", {"status": "failed"})
        self.assertNotIn("url", s3.read_highlight_records(self.job_dir)["en"])
        self.assertNotIn("https://old", (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).read_text(encoding="utf-8"))

    def test_invalid_key_is_refused(self):
        self._write_raw({"bucket": BUCKET, "key": "../../etc/passwd", "status": "uploaded"})
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "invalid_record")
        self.assertEqual(self.fake.presign_calls, [])

    def test_record_from_another_bucket_is_refused(self):
        self._write_raw({"bucket": "someone-elses-bucket", "key": f"highlights/{'a' * 8}-{'b' * 4}-{'c' * 4}-{'d' * 4}-{'e' * 12}/en.mp4", "status": "uploaded"})
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "invalid_record")

    def test_unreadable_record_file_means_no_records(self):
        (self.job_dir / s3.HIGHLIGHT_RECORD_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(s3.read_highlight_records(self.job_dir), {})


class FailureTests(StoreTestBase):
    def test_missing_object_is_a_clear_error(self):
        self._upload()
        self.fake.objects.clear()
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "object_missing")

    def test_access_denied_is_reported_without_echoing_the_error(self):
        self._upload()
        self.fake.head_error = _client_error("403")
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "s3_access")
        self.assertIn("403", str(ctx.exception))

    def test_expired_credentials_are_reported_by_aws_code_only(self):
        self._upload()
        self.fake.head_error = _client_error("ExpiredToken")
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "s3_access")
        self.assertIn("ExpiredToken", str(ctx.exception))

    def test_presign_failure_is_reported(self):
        self._upload()
        self.fake.presign_error = RuntimeError("signing broke")
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "presign_failed")

    def test_temporary_credentials_that_end_early_are_refused(self):
        self._upload()
        soon = datetime.now(timezone.utc) + timedelta(minutes=20)
        self.store._session = FakeSession(FakeCreds(token="session-token", expiry=soon))
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "credentials_expire_early")
        self.assertEqual(self.fake.presign_calls, [], "no link is signed when it would end early")

    def test_temporary_credentials_that_last_long_enough_are_accepted(self):
        self._upload()
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        self.store._session = FakeSession(FakeCreds(token="session-token", expiry=later))
        self.assertEqual(s3.highlight_url(self.job_dir, "en", self.store)["expires_in"], 3600)

    def test_long_term_keys_have_no_expiry_problem(self):
        self._upload()
        self.store._session = FakeSession(FakeCreds(token=None, expiry=None))
        self.assertEqual(s3.highlight_url(self.job_dir, "en", self.store)["expires_in"], 3600)

    def test_missing_credentials_are_reported(self):
        self._upload()
        self.store._session = FakeSession(None)
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertEqual(ctx.exception.code, "credentials_missing")

    def test_error_messages_never_contain_credentials_or_urls(self):
        self._upload()
        # The underlying exception carries a key-like string and a URL. The user-facing error must carry neither.
        self.fake.presign_error = RuntimeError(f"signing with {LEAKY_KEY} for https://bucket.example/x?X-Amz-Signature=abc")
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.highlight_url(self.job_dir, "en", self.store)
        self.assertNotIn(LEAKY_KEY, str(ctx.exception))
        self.assertNotIn("X-Amz", str(ctx.exception))
        self.assertNotIn("https://", str(ctx.exception))

    def test_error_code_uses_aws_code_or_class_name_only(self):
        self.assertEqual(s3.error_code(_client_error("NoSuchKey")), "NoSuchKey")
        self.assertEqual(s3.error_code(RuntimeError(f"contains {LEAKY_KEY}")), "RuntimeError")


class PipelineTests(unittest.TestCase):
    """The serverless executor uploads each highlight, records failures, and never serves a local copy on failure."""

    def setUp(self):
        from src import pipeline_executor as pe

        self.pe = pe
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job_dir = Path(self.tmp.name) / "job"
        self.job_dir.mkdir()
        self.video = self.job_dir / "source.mp4"
        self.video.write_bytes(b"source")
        self.fake = FakeS3()
        self.logs: list[str] = []
        env = {"VIDEO_S3_ENABLED": "true", "VIDEO_S3_WORKER_URL_SUPPORT": "true"}
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch("src.serverless_model_catalog.serverless_highlight_on_worker", return_value=True),
            mock.patch("src.runpod_serverless.load_endpoint_id_from_selection", return_value="ep1"),
            mock.patch.object(s3, "VideoStore", lambda settings, client=None: _REAL_VIDEO_STORE(settings, client=self.fake)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _worker_writes_highlight(self, langs):
        def fake_worker(**kwargs):
            for lang in langs:
                out = self.job_dir / "highlights" / lang
                out.mkdir(parents=True, exist_ok=True)
                (out / f"highlight_{lang}.mp4").write_bytes(b"h" * 2048)
            return None

        return mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless", side_effect=fake_worker)

    def _highlight_uploads(self):
        return [key for _, key, _ in self.fake.uploads if key.startswith("highlights/")]

    def _run(self, langs):
        executor = self.pe.ServerlessRunpodPipelineExecutor()
        return executor.run_highlight_phase(
            job_id="0a1b2c3d",
            video_path=self.video,
            segments=[],
            job_output_dir=self.job_dir,
            summarization_model="llama3.2:3b",
            compression="medium",
            languages=langs,
            log=self.logs.append,
        )

    def test_finished_highlight_is_uploaded_and_recorded_without_deleting_it(self):
        with self._worker_writes_highlight(["en"]):
            paths = self._run(["en"])
        self.assertEqual(set(paths), {"en"})
        self.assertTrue(s3.has_stored_highlight(self.job_dir, "en"))
        highlight_keys = [key for _, key, _ in self.fake.uploads if key.startswith("highlights/")]
        self.assertEqual(len(highlight_keys), 1)
        self.assertNotIn(highlight_keys[0], self.fake.deleted, "a stored highlight must not be deleted")
        self.assertTrue(any("Source video deleted" in line for line in self.logs), "source cleanup still runs")

    def test_upload_failure_marks_the_language_failed_and_does_not_serve_it(self):
        self.fake.upload_error = _client_error("AccessDenied", "PutObject")
        self.fake.upload_error_prefix = "highlights/"  # the source upload still succeeds
        with self._worker_writes_highlight(["en"]):
            paths = self._run(["en"])
        self.assertEqual(paths, {}, "a highlight that failed to upload must not be reported as available")
        rows = s3.read_highlight_records(self.job_dir)
        self.assertEqual(rows["en"]["status"], "failed")
        status = json.loads((self.job_dir / "highlights" / "highlight_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["languages"]["en"]["status"], "failed")
        self.assertEqual(status["languages"]["en"]["failure_kind"], "s3_upload")

    def test_worker_failure_never_falls_back_to_local_rendering(self):
        with mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless", side_effect=RuntimeError("boom")):
            paths = self._run(["en"])
        self.assertEqual(paths, {})
        self.assertEqual(self._highlight_uploads(), [], "no highlight is uploaded when the worker failed")
        rows = json.loads((self.job_dir / "highlights" / "highlight_status.json").read_text(encoding="utf-8"))
        self.assertEqual(rows["languages"]["en"]["failure_kind"], "runpod_worker")

    def test_missing_worker_output_is_reported_as_worker_failure(self):
        with mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless", return_value=None):
            paths = self._run(["en"])
        self.assertEqual(paths, {})
        self.assertEqual(self._highlight_uploads(), [])


class RouteTests(unittest.TestCase):
    """HTTP behaviour through the real Flask routes, with the fake S3 store and a temporary output folder."""

    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:  # the worker image ships src/ and runpod_worker/ only, not the Flask app
            raise unittest.SkipTest("app.py is not part of the worker image")

        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir()
        self.fake = FakeS3()
        self.store = s3.VideoStore(s3.settings_from_env(), client=self.fake)
        patches = [
            mock.patch.object(self.app_module, "OUTPUT_DIR", self.out),
            mock.patch.object(self.app_module, "jobs", {}),
            mock.patch.object(self.app_module, "job_history", [
                {"job_id": "0a1b2c3d", "source": "clip.mp4", "created_at": "2026-01-01T00:00:00", "status": "completed"},
            ]),
            mock.patch.object(s3, "highlight_store", return_value=self.store),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.job = self.out / "0a1b2c3d"
        (self.job / "highlights" / "en").mkdir(parents=True)
        self.mp4 = self.job / "highlights" / "en" / "highlight_en.mp4"
        self.mp4.write_bytes(b"h" * 1024)
        (self.job / "summary.txt").write_text("A short summary.", encoding="utf-8")
        s3.upload_highlight(self.job, "en", self.mp4, self.store)

    def _printed(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            response = fn()
        return response, buf.getvalue()

    def test_link_endpoint_returns_a_fresh_one_hour_url_with_no_cache(self):
        response, printed = self._printed(lambda: self.client.get("/api/highlight-url/0a1b2c3d/en"))
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["expires_in"], 3600)
        self.assertIn("X-Amz-Expires=3600", body["url"])
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertNotIn("X-Amz", printed, "full links must not reach the logs")
        self.assertNotIn("https://", printed)

    def test_reopening_after_an_earlier_link_gets_a_different_link(self):
        first = self.client.get("/api/highlight-url/0a1b2c3d/en").get_json()["url"]
        second = self.client.get("/api/highlight-url/0a1b2c3d/en").get_json()["url"]
        self.assertNotEqual(first, second)

    def test_stream_redirects_to_a_fresh_link_and_never_serves_the_local_copy(self):
        response = self.client.get("/stream/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 302)
        self.assertIn("X-Amz-Expires=3600", response.headers["Location"])
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertNotEqual(response.mimetype, "video/mp4")

    def test_download_redirects_with_attachment_disposition(self):
        response = self.client.get("/download/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.fake.presign_calls[-1]["Params"]["ResponseContentDisposition"].startswith("attachment"))

    def test_s3_failure_on_stream_is_an_error_not_a_local_fallback(self):
        self.fake.head_error = _client_error("403")
        response = self.client.get("/stream/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 503)
        self.assertNotEqual(response.mimetype, "video/mp4")
        self.assertNotIn(b"h" * 16, response.data)

    def test_missing_object_gets_a_clear_error_code(self):
        self.fake.objects.clear()
        response = self.client.get("/api/highlight-url/0a1b2c3d/en")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "object_missing")

    def test_local_only_highlight_reports_not_in_s3_for_the_ui_fallback(self):
        local = self.out / "1a2b3c4d" / "highlights" / "en"
        local.mkdir(parents=True)
        (local / "highlight_en.mp4").write_bytes(b"h")
        response = self.client.get("/api/highlight-url/1a2b3c4d/en")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "not_in_s3")

    def test_local_only_stream_still_serves_the_local_file(self):
        local = self.out / "1a2b3c4d" / "highlights" / "en"
        local.mkdir(parents=True)
        (local / "highlight_en.mp4").write_bytes(b"h" * 64)
        response = self.client.get("/stream/highlight/1a2b3c4d/en")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/mp4")
        response.close()

    def test_invalid_language_and_unknown_job_are_rejected(self):
        self.assertEqual(self.client.get("/api/highlight-url/0a1b2c3d/zz-invalid-zz").status_code, 400)
        self.assertEqual(self.client.get("/api/highlight-url/9f9f9f9f/en").status_code, 404)

    def test_history_and_results_never_expose_a_url(self):
        history = self.client.get("/api/history").get_data(as_text=True)
        results = self.client.get("/api/results/job1").get_data(as_text=True)
        for text in (history, results):
            self.assertNotIn("X-Amz", text)
            self.assertNotIn("https://", text)

    def test_history_entry_lists_s3_stored_languages_for_a_job_folder(self):
        entry = self.app_module.enrich_history_entry({"job_id": "0a1b2c3d"})
        self.assertEqual(entry["s3_stored_highlights"], ["en"])
        local_entry = self.app_module.enrich_history_entry({"job_id": "1a2b3c4d"})
        self.assertEqual(local_entry.get("s3_stored_highlights", []), [])


class RouteGuardTests(unittest.TestCase):
    """Failed uploads, RunPod retry refusal, and job-ID validation on the highlight routes."""

    setUpClass = classmethod(RouteTests.setUpClass.__func__)
    setUp = RouteTests.setUp
    _printed = RouteTests._printed

    def _mark_upload_failed(self):
        (self.job / "highlights" / "highlight_status.json").write_text(json.dumps({
            "languages": {"en": {"language": "en", "status": "failed", "failure_kind": "s3_upload",
                                 "message": "upload failed"}}
        }), encoding="utf-8")
        records = self.job / s3.HIGHLIGHT_RECORD_NAME
        if records.exists():
            records.unlink()
        s3.write_highlight_record(self.job, "en", {"status": "failed", "error_code": "AccessDenied"})

    def test_stream_refuses_the_local_copy_after_a_failed_s3_upload(self):
        self._mark_upload_failed()
        response = self.client.get("/stream/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 503)
        self.assertNotEqual(response.mimetype, "video/mp4")
        self.assertNotIn(b"h" * 64, response.data)
        self.assertEqual(self.fake.presign_calls, [])

    def test_download_refuses_the_local_copy_after_a_failed_s3_upload(self):
        self._mark_upload_failed()
        response = self.client.get("/download/highlight/0a1b2c3d/en")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"h" * 64, response.data)

    def test_invalid_job_ids_are_rejected_before_any_file_or_s3_lookup(self):
        for bad in ("ABCDEF12", "zzzzzzzz", "0a1b2c3", "0a1b2c3d4"):
            with self.subTest(job_id=bad):
                self.assertEqual(self.client.get(f"/api/highlight-url/{bad}/en").status_code, 404)
                self.assertEqual(self.client.get(f"/stream/highlight/{bad}/en").status_code, 404)
                self.assertEqual(self.client.get(f"/download/highlight/{bad}/en").status_code, 404)
        self.assertEqual(self.fake.presign_calls, [])

    def test_retry_is_refused_in_runpod_mode_without_rendering(self):
        with mock.patch("src.pipeline_runtime.load_pipeline_runtime", return_value="runpod"), \
                mock.patch.object(self.app_module, "retry_highlight_languages") as retry:
            response = self.client.post("/api/highlight/retry/0a1b2c3d", json={"languages": ["en"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "retry_unavailable_runpod")
        retry.assert_not_called()

    def test_retry_still_runs_locally_in_local_mode(self):
        with mock.patch("src.pipeline_runtime.load_pipeline_runtime", return_value="local"), \
                mock.patch.object(self.app_module, "retry_highlight_languages", return_value={}) as retry:
            response = self.client.post("/api/highlight/retry/0a1b2c3d", json={"languages": ["en"]})
        self.assertEqual(response.status_code, 200)
        retry.assert_called_once()


@unittest.skipUnless((ROOT / "app.py").exists(), "app.py is not part of the worker image")
class ServerBindingTests(unittest.TestCase):
    """The development server must listen on localhost only, with the debugger off by default."""

    def test_app_run_binds_to_localhost(self):
        import ast

        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run"]
        self.assertTrue(runs, "app.run call not found")
        host = {kw.arg: kw.value for kw in runs[-1].keywords}["host"]
        self.assertEqual(getattr(host, "value", None), "127.0.0.1")

    def test_debug_is_opt_in_by_environment(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("VIDEO_SUMMARIZER_DEBUG"', source)
        self.assertNotIn("_flask_debug = True", source)


if __name__ == "__main__":
    unittest.main()
