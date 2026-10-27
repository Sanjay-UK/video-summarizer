"""Regression tests: S3 storage for RunPod highlight jobs, and the RunPod worker's download of the source.

AWS and the network are fully mocked (zz/fake_s3.py). Covered: settings, key layout, SSE-S3 upload, presigned GET
links with the configured expiry, the worker-side download and its limits, and the highlight executor's use of the
source URL.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

import fake_s3  # noqa: E402
from src import pipeline_executor, runpod_serverless_payload as payload, s3_video_store as s3  # noqa: E402

SIGNED = "https://video-summarizer-757374023708-ap-south-1-an.s3.ap-south-1.amazonaws.com/k.mp4?X-Amz-Signature=secret123"
UID = "7d9c2f1e-4b8a-4c3d-9e2f-1a2b3c4d5e6f"


class LayoutAndSettingsTests(unittest.TestCase):
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

    def test_invalid_expiry_falls_back_and_is_clamped(self):
        self.assertEqual(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "abc"})["presign_expires"], 3600)
        self.assertEqual(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "999999999"})["presign_expires"], 7 * 24 * 3600)

    def test_worker_url_support_is_separate_from_storage(self):
        self.assertFalse(s3.worker_url_support({}))
        self.assertTrue(s3.worker_url_support({"VIDEO_S3_WORKER_URL_SUPPORT": "true"}))

    def test_each_video_gets_its_own_unique_id(self):
        ids = {s3.make_video_uid() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for uid in ids:
            self.assertRegex(uid, s3.VIDEO_UID_RE)

    def test_keys_are_built_from_job_id_and_video_id(self):
        self.assertEqual(s3.job_prefix("0a1b2c3d", UID), f"jobs/0a1b2c3d/{UID}/")
        self.assertEqual(s3.artifact_key("0a1b2c3d", UID, "highlights/en/highlight_en.mp4"),
                         f"jobs/0a1b2c3d/{UID}/highlights/en/highlight_en.mp4")
        self.assertEqual(s3.manifest_key("0a1b2c3d", UID), f"jobs/0a1b2c3d/{UID}/manifest.json")

    def test_invalid_job_id_video_id_or_path_is_refused(self):
        with self.assertRaises(ValueError):
            s3.job_prefix("../etc", UID)
        with self.assertRaises(ValueError):
            s3.job_prefix("0a1b2c3d", "not-a-uuid")
        with self.assertRaises(ValueError):
            s3.artifact_key("0a1b2c3d", UID, "../../escape.txt")
        with self.assertRaises(ValueError):
            s3.artifact_key("0a1b2c3d", UID, "/absolute.txt")

    def test_local_bookkeeping_and_render_scratch_are_not_uploaded(self):
        self.assertFalse(s3._should_upload("s3_video.json"))
        self.assertFalse(s3._should_upload("s3_highlights.json"))
        self.assertFalse(s3._should_upload("highlights/en/scene_000/build/scene_000_final.mp4"))
        self.assertFalse(s3._should_upload("audio.partial.mp3"))
        self.assertTrue(s3._should_upload("highlights/_shared/scene_000/visual.mp4"))
        self.assertTrue(s3._should_upload("highlights/en/highlight_en.mp4"))


class VideoStoreTests(unittest.TestCase):
    def test_upload_uses_sse_s3_and_the_content_type(self):
        client = fake_s3.FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({}), client=client)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.mp4"
            path.write_bytes(b"video")
            store.upload_file(path, "jobs/0a1b2c3d/u/source.mp4", content_type="video/mp4")
        self.assertEqual(client.sse["jobs/0a1b2c3d/u/source.mp4"], "AES256")
        self.assertEqual(client.content_types["jobs/0a1b2c3d/u/source.mp4"], "video/mp4")

    def test_presigned_get_uses_configured_expiry_and_exact_key(self):
        client = fake_s3.FakeS3Client()
        store = s3.VideoStore(s3.settings_from_env({"VIDEO_S3_PRESIGN_EXPIRES": "3600"}), client=client)
        url = store.presign_get("abc.mp4")
        self.assertIn("abc.mp4", url)
        self.assertEqual(client.presigned[0]["op"], "get_object")
        self.assertEqual(client.presigned[0]["params"]["Key"], "abc.mp4")
        self.assertEqual(client.presigned[0]["expires"], 3600)

    def test_client_is_not_created_until_used(self):
        store = s3.VideoStore(s3.settings_from_env({}))
        self.assertIsNone(store._client)


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




class PipelineSourceUrlTests(unittest.TestCase):
    """The RunPod highlight step receives the source link from the job. It never uploads anything itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job_dir = Path(self.tmp.name) / "job"
        self.job_dir.mkdir()
        self.video = Path(self.tmp.name) / "source.mp4"
        self.video.write_bytes(b"video")
        self.segments = [{"start": 0, "end": 1, "text": "hi"}]

    def _run(self, *, source_url, remote=None):
        ex = pipeline_executor.ServerlessRunpodPipelineExecutor()
        logs = []
        patches = [
            mock.patch("src.serverless_model_catalog.serverless_highlight_on_worker", return_value=True),
            mock.patch("src.runpod_serverless.load_endpoint_id_from_selection", return_value="ep1"),
            mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless",
                       side_effect=remote or (lambda **k: {})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = ex.run_highlight_phase(
            job_id="job1", video_path=self.video, segments=self.segments, job_output_dir=self.job_dir,
            summarization_model="llama3.2:3b", compression="medium", languages=["en"], log=logs.append,
            source_url=source_url,
        )
        return result, logs

    def test_source_link_is_passed_to_the_worker(self):
        remote = mock.Mock(return_value={})
        self._run(source_url=SIGNED, remote=remote)
        self.assertEqual(remote.call_args.kwargs.get("video_url"), SIGNED)

    def test_without_a_link_the_worker_gets_no_url(self):
        remote = mock.Mock(return_value={})
        self._run(source_url=None, remote=remote)
        self.assertIsNone(remote.call_args.kwargs.get("video_url"))

    def test_the_executor_never_builds_a_store(self):
        with mock.patch("src.s3_video_store.VideoStore") as store_cls:
            self._run(source_url=SIGNED)
        store_cls.assert_not_called()

    def test_worker_failure_marks_each_language_failed_without_any_local_render(self):
        def boom(**_kwargs):
            raise RuntimeError("worker failed")

        result, _logs = self._run(source_url=SIGNED, remote=boom)
        self.assertEqual(result, {})
        status = json.loads((self.job_dir / "highlights" / "highlight_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["languages"]["en"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
