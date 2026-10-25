"""Regression tests: S3 source-video storage for RunPod highlight jobs.

AWS and the network are fully mocked. Covered: upload with SSE-S3, unique root-level keys, presigned GET
URLs with the configured expiry, the worker-side download and its limits, failure handling, cleanup after
success only, no presigned URL or credential stored, and local mode with no S3 configuration.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import pipeline_executor, runpod_serverless_payload as payload, s3_video_store as s3  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(\.mp4)?$")
SIGNED = "https://video-summarizer-757374023708-ap-south-1-an.s3.ap-south-1.amazonaws.com/k.mp4?X-Amz-Signature=secret123"


class FakeS3Client:
    def __init__(self, fail_upload=False, fail_delete=False):
        self.uploads = []
        self.deletes = []
        self.presigns = []
        self.fail_upload = fail_upload
        self.fail_delete = fail_delete

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        if self.fail_upload:
            raise ConnectionError("network down")
        self.uploads.append({"filename": filename, "bucket": bucket, "key": key, "extra": ExtraArgs})

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigns.append({"op": op, "params": Params, "expires": ExpiresIn})
        return SIGNED

    def delete_object(self, Bucket, Key):
        if self.fail_delete:
            raise PermissionError("denied")
        self.deletes.append({"bucket": Bucket, "key": Key})


class KeyAndSettingsTests(unittest.TestCase):
    def test_key_is_uuid_with_original_extension(self):
        key = s3.make_object_key("my clip.MP4")
        self.assertRegex(key, UUID_RE)
        self.assertTrue(key.endswith(".mp4"))

    def test_key_is_at_bucket_root_with_no_prefix(self):
        for _ in range(20):
            self.assertNotIn("/", s3.make_object_key("a.mov"))

    def test_unknown_extension_is_dropped(self):
        key = s3.make_object_key("payload.exe")
        self.assertNotIn(".exe", key)
        self.assertRegex(key, r"^[0-9a-f-]{36}$")

    def test_keys_are_unique_across_jobs(self):
        keys = {s3.make_object_key("same.mp4") for _ in range(500)}
        self.assertEqual(len(keys), 500)

    def test_s3_is_off_by_default(self):
        self.assertFalse(s3.s3_enabled({}))
        self.assertFalse(s3.s3_enabled({"VIDEO_S3_ENABLED": "no"}))

    def test_storage_flag_alone_does_not_enable_s3(self):
        # Without worker support, URL jobs would fail on the deployed worker, so one flag must not be enough.
        self.assertFalse(s3.s3_enabled({"VIDEO_S3_ENABLED": "true"}))
        self.assertFalse(s3.s3_enabled({"VIDEO_S3_WORKER_URL_SUPPORT": "true"}))

    def test_both_flags_enable_s3(self):
        self.assertTrue(s3.s3_enabled({"VIDEO_S3_ENABLED": "true", "VIDEO_S3_WORKER_URL_SUPPORT": "true"}))

    def test_settings_defaults_and_overrides(self):
        d = s3.settings_from_env({})
        self.assertEqual(d["bucket"], "video-summarizer-757374023708-ap-south-1-an")
        self.assertEqual(d["region"], "ap-south-1")
        self.assertEqual(d["presign_expires"], 3600)
        self.assertEqual(d["profile"], "video-summarizer")
        o = s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "900", "VIDEO_S3_PROFILE": "p1"})
        self.assertEqual(o["presign_expires"], 900)
        self.assertEqual(o["profile"], "p1")

    def test_profile_is_never_the_default_chain_and_ignores_aws_profile(self):
        # AWS_PROFILE set to another project's identity must not redirect uploads.
        d = s3.settings_from_env({"AWS_PROFILE": "trading-auto"})
        self.assertEqual(d["profile"], "video-summarizer")
        self.assertIsNotNone(d["profile"])

    def test_invalid_expiry_falls_back_and_is_clamped(self):
        self.assertEqual(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "abc"})["presign_expires"], 3600)
        self.assertEqual(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "999999999"})["presign_expires"], 7 * 24 * 3600)


class VideoStoreTests(unittest.TestCase):
    def test_upload_uses_sse_s3_and_key(self):
        client = FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        store.upload_source(Path("/tmp/x.mp4"), "abc.mp4")
        up = client.uploads[0]
        self.assertEqual(up["bucket"], "video-summarizer-757374023708-ap-south-1-an")
        self.assertEqual(up["key"], "abc.mp4")
        self.assertEqual(up["extra"]["ServerSideEncryption"], "AES256")

    def test_presigned_get_uses_configured_expiry_and_exact_key(self):
        client = FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "3600"}), client=client)
        url = store.presign_get("abc.mp4")
        self.assertEqual(url, SIGNED)
        self.assertEqual(client.presigns[0]["op"], "get_object")
        self.assertEqual(client.presigns[0]["params"]["Key"], "abc.mp4")
        self.assertEqual(client.presigns[0]["expires"], 3600)

    def test_client_is_not_created_until_used(self):
        store = s3.VideoStore(s3.settings_from_env({}))
        self.assertIsNone(store._client)

    def test_job_record_holds_key_and_never_a_url_or_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            s3.write_record(Path(tmp), {"bucket": "b", "key": "k.mp4", "status": "uploaded"})
            text = (Path(tmp) / s3.S3_RECORD_NAME).read_text(encoding="utf-8")
            self.assertIn("k.mp4", text)
            self.assertNotIn("X-Amz", text)
            self.assertNotIn("secret", text.lower())


class FakeHttpResponse:
    def __init__(self, chunks, content_length=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(content_length)} if content_length is not None else {}

    def read(self, _n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WorkerDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "job" / "source.mp4"

    def test_download_writes_file(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse([b"abc", b"def"], 6)):
            out = payload.download_video_from_url(SIGNED, self.dest)
        self.assertEqual(out.read_bytes(), b"abcdef")

    def test_declared_size_over_limit_is_rejected_before_download(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse([b"x"], 10_000)):
            with self.assertRaises(ValueError):
                payload.download_video_from_url(SIGNED, self.dest, max_bytes=100)

    def test_streamed_size_over_limit_is_rejected(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse([b"x" * 80, b"x" * 80])):
            with self.assertRaises(ValueError):
                payload.download_video_from_url(SIGNED, self.dest, max_bytes=100)

    def test_empty_download_is_rejected(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse([])):
            with self.assertRaises(ValueError):
                payload.download_video_from_url(SIGNED, self.dest)

    def test_http_error_message_does_not_contain_the_url(self):
        import urllib.error

        err = urllib.error.HTTPError(SIGNED, 403, "Forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                payload.download_video_from_url(SIGNED, self.dest)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("may have expired", str(ctx.exception))
        self.assertIn("No local rendering", str(ctx.exception))
        self.assertNotIn("X-Amz", str(ctx.exception))
        self.assertNotIn("secret123", str(ctx.exception))

    def test_network_error_message_does_not_contain_the_url(self):
        import urllib.error

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(RuntimeError) as ctx:
                payload.download_video_from_url(SIGNED, self.dest)
        self.assertNotIn("secret123", str(ctx.exception))

    def test_decode_uses_url_when_present_and_stays_inside_dest_dir(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse([b"video"], 5)):
            out = payload.decode_video_to_path({"video_url": SIGNED, "video_file": "../../evil.mp4"}, self.dest)
        self.assertEqual(out.parent, self.dest.parent)
        self.assertEqual(out.name, "evil.mp4")

    def test_safe_video_name_strips_paths_and_unknown_extensions(self):
        self.assertEqual(payload.make_safe_video_name("../../x.MOV"), "x.mov")
        self.assertEqual(payload.make_safe_video_name("bad name!.exe"), "badname.mp4")


class BuildInputTests(unittest.TestCase):
    def test_url_input_carries_no_video_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "big.mp4"
            video.write_bytes(b"x" * 50_000_000)  # far over the /run limit, must not matter in URL mode
            body = payload.build_highlight_phase_input(
                video_path=video, segments_json="[]", languages="en", job_id="j1",
                summarization_model="llama3.2:3b", compression="medium", video_url=SIGNED,
            )
        self.assertEqual(body["video_url"], SIGNED)
        self.assertNotIn("video_gzip_b64", body)
        self.assertLess(len(json.dumps(body)), 10_000)


class PipelineS3FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job_dir = Path(self.tmp.name) / "job"
        self.job_dir.mkdir()
        self.video = Path(self.tmp.name) / "clip.mp4"
        self.video.write_bytes(b"video")
        self.segments = [{"start": 0, "end": 1, "text": "hi"}]

    def _run(self, *, s3_on, remote=None, client=None, upload_store=None):
        ex = pipeline_executor.ServerlessRunpodPipelineExecutor()
        logs = []
        patches = [
            mock.patch("src.serverless_model_catalog.serverless_highlight_on_worker", return_value=True),
            mock.patch("src.runpod_serverless.load_endpoint_id_from_selection", return_value="ep1"),
            mock.patch("src.s3_video_store.s3_enabled", return_value=s3_on),
            mock.patch("src.s3_video_store.settings_from_env", return_value=s3.settings_from_env({})),
            mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless", side_effect=remote or (lambda **k: {})),
        ]
        if upload_store is not None:
            patches.append(mock.patch("src.s3_video_store.VideoStore", return_value=upload_store))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = ex.run_highlight_phase(
            job_id="job1", video_path=self.video, segments=self.segments, job_output_dir=self.job_dir,
            summarization_model="llama3.2:3b", compression="medium", languages=["en"], log=logs.append,
        )
        return result, logs

    def _record(self):
        return json.loads((self.job_dir / s3.S3_RECORD_NAME).read_text(encoding="utf-8"))

    def test_local_mode_without_s3_never_builds_a_store(self):
        remote = mock.Mock(return_value={})
        with mock.patch("src.s3_video_store.VideoStore") as store_cls:
            self._run(s3_on=False, remote=remote)
        store_cls.assert_not_called()
        self.assertIsNone(remote.call_args.kwargs.get("video_url"))

    def test_success_uploads_passes_url_then_deletes_after_completion(self):
        client = FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        with mock.patch("src.s3_video_store.VideoStore", return_value=store):
            remote = mock.Mock(return_value={})
            self._run(s3_on=True, remote=remote)
        key = client.uploads[0]["key"]
        self.assertEqual(remote.call_args.kwargs["video_url"], SIGNED)
        self.assertEqual(client.deletes, [{"bucket": store.bucket, "key": key}])
        record = self._record()
        self.assertEqual(record["status"], "deleted")
        self.assertEqual(record["key"], key)
        self.assertNotIn("X-Amz", json.dumps(record))

    def test_remote_failure_keeps_source_and_reports_failure(self):
        client = FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        remote = mock.Mock(side_effect=RuntimeError("worker crashed"))
        with mock.patch("src.s3_video_store.VideoStore", return_value=store):
            result, logs = self._run(s3_on=True, remote=remote)
        self.assertEqual(result, {})
        self.assertEqual(client.deletes, [], "source must be kept when the job fails")
        self.assertEqual(self._record()["status"], "retained_after_failure")
        self.assertTrue(any("kept for retry" in line for line in logs))
        failure = json.loads((self.job_dir / "highlights" / "en" / "language_status.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], "failed")
        self.assertIn("worker crashed", failure["message"])

    def test_upload_failure_does_not_start_remote_job_or_render_locally(self):
        client = FakeS3Client(fail_upload=True)
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        remote = mock.Mock(return_value={})
        with mock.patch("src.s3_video_store.VideoStore", return_value=store):
            result, _logs = self._run(s3_on=True, remote=remote)
        remote.assert_not_called()
        self.assertEqual(result, {})
        failure = json.loads((self.job_dir / "highlights" / "en" / "language_status.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_kind"], "s3_upload")
        self.assertNotIn("network down", failure["message"], "message names the exception type, not the detail")

    def test_delete_failure_is_reported_and_key_is_kept(self):
        client = FakeS3Client(fail_delete=True)
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        remote = mock.Mock(return_value={})
        with mock.patch("src.s3_video_store.VideoStore", return_value=store):
            _result, logs = self._run(s3_on=True, remote=remote)
        record = self._record()
        self.assertEqual(record["status"], "delete_failed")
        self.assertIn("key", record)
        self.assertTrue(any("could not delete S3 source" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
