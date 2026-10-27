"""Regression tests: S3 is the only store for a job's files, its History entry, results, downloads and retries.

AWS and the network are fully mocked (zz/fake_s3.py). Covered: the job/video layout, the manifest written last, a failed
upload leaving no job in History, History read from S3 only, fresh one-hour links with no URL saved, no local fallback,
delete, retry that uploads only what changed, storage readiness, and error text that never carries credentials or links.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

import fake_s3  # noqa: E402
from src import s3_video_store as s3  # noqa: E402

JOB = "0a1b2c3d"
TOKEN = "SYNTHETIC_TOKEN_1234567890ABCDEFGHIJKLMNOP"
SECRET_URL = "https://bucket.s3.example.test/k.mp4?X-Amz-Signature=SYNTHETIC_SIG_9f8e7d"


def _job_folder(root: Path, *, with_summary=True, with_highlight=True) -> Path:
    """A finished job folder as the pipeline leaves it, with the scratch files that must not be uploaded."""
    folder = root / JOB
    folder.mkdir(parents=True)
    (folder / "source.mp4").write_bytes(b"SOURCE" * 100)
    (folder / "segments.json").write_text("[]", encoding="utf-8")
    (folder / "transcript.txt").write_text("hello", encoding="utf-8")
    if with_summary:
        (folder / "summary.txt").write_text("A short summary.", encoding="utf-8")
    if with_highlight:
        (folder / "highlights" / "en").mkdir(parents=True)
        (folder / "highlights" / "en" / "highlight_en.mp4").write_bytes(b"h" * 2048)
        (folder / "highlights" / "highlight_status.json").write_text(
            json.dumps({"languages": {}, "requested_languages": ["en"]}), encoding="utf-8")
    (folder / "s3_video.json").write_text("{}", encoding="utf-8")  # local bookkeeping, never uploaded
    scratch = folder / "highlights" / "en" / "scene_000" / "build"
    scratch.mkdir(parents=True)
    (scratch / "scene_000_final.mp4").write_bytes(b"scratch")
    return folder


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = _job_folder(Path(self.tmp.name))
        self.store = fake_s3.new_store()

    def _publish(self, known=None):
        results = {"status": "completed", "summary": "A short summary.", "summary_length": 16}
        return s3.publish_job(
            self.store, self.folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
            created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00", results=results,
            known_files=known,
        )

    def test_every_file_is_uploaded_and_the_manifest_is_written_last(self):
        manifest = self._publish()
        keys = list(self.store.client.objects)
        self.assertTrue(keys[-1].endswith("/manifest.json"), "the manifest must be the last object written")
        self.assertEqual(set(manifest["files"]), {
            "source.mp4", "segments.json", "transcript.txt", "summary.txt",
            "highlights/en/highlight_en.mp4", "highlights/highlight_status.json",
        })
        for rel, entry in manifest["files"].items():
            self.assertEqual(entry["key"], f"jobs/{JOB}/{manifest['video_uid']}/{rel}")
            self.assertEqual(self.store.client.objects[entry["key"]], (self.folder / rel).read_bytes())

    def test_scratch_and_local_bookkeeping_are_not_in_the_manifest(self):
        manifest = self._publish()
        self.assertNotIn("s3_video.json", manifest["files"])
        self.assertFalse(any("/build/" in rel for rel in manifest["files"]))

    def test_a_failed_upload_writes_no_manifest_so_the_job_is_not_listed(self):
        with mock.patch.object(self.store.client, "upload_file", side_effect=ConnectionError("down")):
            with self.assertRaises(ConnectionError):
                self._publish()
        self.assertEqual(s3.list_job_manifests(self.store), [])
        self.assertFalse(any(k.endswith("manifest.json") for k in self.store.client.objects))

    def test_upload_size_mismatch_is_refused(self):
        with mock.patch.object(self.store.client, "head_object", return_value={"ContentLength": 1}):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                self._publish()
        self.assertEqual(ctx.exception.code, "size_mismatch")
        self.assertFalse(any(k.endswith("manifest.json") for k in self.store.client.objects))

    def test_a_file_already_in_storage_is_not_sent_again(self):
        uid = s3.make_video_uid()
        known = {"source.mp4": s3.upload_job_file(self.store, JOB, uid, self.folder / "source.mp4", "source.mp4")}
        with mock.patch.object(self.store.client, "upload_file", wraps=self.store.client.upload_file) as up:
            manifest = s3.publish_job(
                self.store, self.folder, job_id=JOB, video_uid=uid, source_name="clip.mp4",
                created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
                results={"status": "completed"}, known_files=known,
            )
        uploaded_keys = [c.args[2] for c in up.call_args_list]
        self.assertNotIn(f"jobs/{JOB}/{uid}/source.mp4", uploaded_keys)
        self.assertEqual(manifest["files"]["source.mp4"], known["source.mp4"])

    def test_failed_job_manifest_lists_only_files_already_stored(self):
        s3.publish_failure(
            self.store, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
            created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:01:00",
            error="The video could not be read.", error_code=None, known_files={},
        )
        manifest = fake_s3.manifest(self.store, JOB)
        self.assertEqual(manifest["status"], "error")
        self.assertEqual(manifest["files"], {})


class HistoryAndManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fake_s3.new_store()

    def test_only_jobs_with_a_manifest_are_listed(self):
        folder = _job_folder(Path(self.tmp.name))
        s3.publish_job(self.store, folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
                       created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
                       results={"status": "completed"})
        # A job whose manifest was never written (an interrupted upload) must not appear in History.
        self.store.client.objects[f"jobs/ffffffff/{s3.make_video_uid()}/source.mp4"] = b"orphan"
        listed = s3.list_job_manifests(self.store)
        self.assertEqual([m["job_id"] for m in listed], [JOB])

    def test_newest_job_is_listed_first(self):
        for job, created in (("1a1a1a1a", "2026-01-01T00:00:00"), ("2b2b2b2b", "2026-02-01T00:00:00")):
            uid = s3.make_video_uid()
            self.store.put_json(s3.manifest_key(job, uid), {
                "schema": s3.MANIFEST_SCHEMA, "job_id": job, "video_uid": uid, "status": "completed",
                "created_at": created, "files": {}, "bucket": self.store.bucket,
            })
        listed = s3.list_job_manifests(self.store)
        self.assertEqual([m["job_id"] for m in listed], ["2b2b2b2b", "1a1a1a1a"])

    def test_a_manifest_that_points_at_another_job_is_ignored(self):
        uid = s3.make_video_uid()
        forged = {"schema": s3.MANIFEST_SCHEMA, "job_id": JOB, "video_uid": uid, "status": "completed",
                  "created_at": "2026-01-01", "bucket": self.store.bucket,
                  "files": {"summary.txt": {"key": f"jobs/9f9f9f9f/{uid}/summary.txt"}}}
        self.store.put_json(s3.manifest_key(JOB, uid), forged)
        self.assertIsNone(s3.load_job_manifest(self.store, JOB))

    def test_history_listing_is_cached_and_refreshed_by_a_publish(self):
        folder = _job_folder(Path(self.tmp.name))
        self.assertEqual(s3.list_job_manifests(self.store), [])
        with mock.patch.object(self.store.client, "list_objects_v2", wraps=self.store.client.list_objects_v2) as listing:
            s3.list_job_manifests(self.store)  # a poll within the cache window
            s3.list_job_manifests(self.store)
        self.assertEqual(listing.call_count, 0, "repeat polls must not list the bucket again")
        s3.publish_job(self.store, folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
                       created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
                       results={"status": "completed"})
        self.assertEqual([m["job_id"] for m in s3.list_job_manifests(self.store)], [JOB])

    def test_manifest_is_cached_briefly_and_refreshed_after_a_publish(self):
        self.assertIsNone(s3.load_job_manifest(self.store, JOB))  # cached as "no job"
        folder = _job_folder(Path(self.tmp.name))
        s3.publish_job(self.store, folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
                       created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
                       results={"status": "completed"})
        self.assertIsNotNone(s3.load_job_manifest(self.store, JOB), "publishing must clear the cached miss")


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fake_s3.new_store()
        self.folder = _job_folder(Path(self.tmp.name))
        self.manifest = s3.publish_job(
            self.store, self.folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
            created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00",
            results={"status": "completed"})

    def test_link_is_for_the_exact_key_and_lasts_one_hour(self):
        link = s3.file_link(self.store, self.manifest, "summary.txt", attachment=True)
        expected = f"jobs/{JOB}/{self.manifest['video_uid']}/summary.txt"
        self.assertEqual(self.store.client.presigned[-1]["params"]["Key"], expected)
        self.assertEqual(self.store.client.presigned[-1]["expires"], s3.HIGHLIGHT_URL_EXPIRES)
        self.assertEqual(link["expires_in"], 3600)
        self.assertEqual(fake_s3.key_from_link(link["url"]), expected)

    def test_attachment_and_inline_set_the_disposition(self):
        s3.file_link(self.store, self.manifest, "summary.txt", attachment=True)
        self.assertTrue(self.store.client.presigned[-1]["params"]["ResponseContentDisposition"].startswith("attachment"))
        s3.file_link(self.store, self.manifest, "summary.txt", attachment=False)
        self.assertTrue(self.store.client.presigned[-1]["params"]["ResponseContentDisposition"].startswith("inline"))

    def test_each_request_gets_a_new_link_and_nothing_is_saved(self):
        s3.file_link(self.store, self.manifest, "summary.txt")
        s3.file_link(self.store, self.manifest, "summary.txt")
        self.assertEqual(len(self.store.client.presigned), 2)
        stored = json.dumps(fake_s3.manifest(self.store, JOB))
        self.assertNotIn("X-Amz", stored)

    def test_a_file_the_job_does_not_have_is_not_in_s3(self):
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.file_link(self.store, self.manifest, "highlights/hi/highlight_hi.mp4")
        self.assertEqual(ctx.exception.code, "not_in_s3")

    def test_a_manifest_from_another_bucket_is_refused(self):
        other = dict(self.manifest, bucket="someone-elses-bucket")
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.file_link(self.store, other, "summary.txt")
        self.assertEqual(ctx.exception.code, "invalid_record")

    def test_a_missing_object_gets_a_clear_code(self):
        del self.store.client.objects[f"jobs/{JOB}/{self.manifest['video_uid']}/summary.txt"]
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.file_link(self.store, self.manifest, "summary.txt")
        self.assertEqual(ctx.exception.code, "object_missing")

    def test_access_denied_is_reported_by_code_only(self):
        with mock.patch.object(self.store, "head", side_effect=fake_s3.FakeClientError("AccessDenied")):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.file_link(self.store, self.manifest, "summary.txt")
        self.assertEqual(ctx.exception.code, "s3_access")
        self.assertIn("AccessDenied", str(ctx.exception))
        self.assertNotIn("amazonaws", str(ctx.exception))

    def test_presign_failure_is_reported(self):
        self.store.client.fail_presign = True
        with self.assertRaises(s3.HighlightStoreError) as ctx:
            s3.file_link(self.store, self.manifest, "summary.txt")
        self.assertEqual(ctx.exception.code, "presign_failed")

    def test_short_lived_temporary_credentials_are_refused(self):
        with mock.patch.object(s3.VideoStore, "signing_credentials_problem", return_value="credentials_expire_early"):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.file_link(self.store, self.manifest, "summary.txt")
        self.assertEqual(ctx.exception.code, "credentials_expire_early")
        self.assertEqual(self.store.client.presigned, [])

    def test_error_messages_never_carry_a_link_or_a_token(self):
        for exc in (s3.HighlightStoreError("s3_access", "S3 refused"), fake_s3.FakeClientError("AccessDenied")):
            mapped = s3.as_store_error(exc)
            self.assertNotIn(TOKEN, str(mapped))
            self.assertNotIn(SECRET_URL, str(mapped))


class EnsureReadyTests(unittest.TestCase):
    def test_ready_when_the_bucket_answers(self):
        s3.ensure_ready(fake_s3.new_store())

    def test_missing_profile_is_reported_as_credentials_missing(self):
        store = fake_s3.new_store()
        with mock.patch.object(store, "head_bucket", side_effect=fake_s3.FakeClientError("ProfileNotFound")):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.ensure_ready(store)
        self.assertEqual(ctx.exception.code, "credentials_missing")

    def test_access_denied_is_reported_as_access(self):
        store = fake_s3.new_store()
        with mock.patch.object(store, "head_bucket", side_effect=fake_s3.FakeClientError("AccessDenied")):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.ensure_ready(store)
        self.assertEqual(ctx.exception.code, "s3_access")

    def test_network_failure_is_reported_as_unavailable(self):
        store = fake_s3.new_store()
        with mock.patch.object(store, "head_bucket", side_effect=ConnectionError("no route")):
            with self.assertRaises(s3.HighlightStoreError) as ctx:
                s3.ensure_ready(store)
        self.assertEqual(ctx.exception.code, "s3_unavailable")


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fake_s3.new_store()
        folder = _job_folder(Path(self.tmp.name))
        self.manifest = s3.publish_job(
            self.store, folder, job_id=JOB, video_uid=s3.make_video_uid(), source_name="clip.mp4",
            created_at="2026-01-01T00:00:00", completed_at="2026-01-01T00:05:00", results={"status": "completed"})

    def test_delete_removes_every_file_of_the_job(self):
        removed = s3.delete_job(self.store, JOB)
        self.assertGreater(removed, 0)
        self.assertEqual([k for k in self.store.client.objects if k.startswith(f"jobs/{JOB}/")], [])
        self.assertIsNone(s3.load_job_manifest(self.store, JOB))

    def test_a_partly_failed_delete_keeps_the_manifest_so_the_job_stays_visible(self):
        with mock.patch.object(self.store, "delete_keys", side_effect=s3.HighlightStoreError("s3_access", "no")):
            with self.assertRaises(s3.HighlightStoreError):
                s3.delete_job(self.store, JOB)
        self.assertIsNotNone(s3.load_job_manifest(self.store, JOB), "the manifest must survive a failed delete")

    def test_deleting_an_unknown_job_removes_nothing(self):
        self.assertEqual(s3.delete_job(self.store, "ffffffff"), 0)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app = app_module

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = fake_s3.new_store()
        patches = [
            mock.patch.object(self.app, "OUTPUT_DIR", Path(self.tmp.name) / "output"),
            mock.patch.object(self.app, "jobs", {}),
            mock.patch.object(self.app, "_s3_store", return_value=self.store),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = self.app.app.test_client()
        self.folder = _job_folder(Path(self.tmp.name))
        self.manifest = fake_s3.publish_local_job(self.app, self.store, self.folder, JOB)

    def _key(self, rel):
        return f"jobs/{JOB}/{self.manifest['video_uid']}/{rel}"

    def test_history_lists_the_stored_job_with_its_highlight(self):
        listed = self.client.get("/api/history").get_json()["jobs"]
        self.assertEqual([j["job_id"] for j in listed], [JOB])
        self.assertEqual(listed[0]["status"], "completed")
        self.assertTrue(listed[0]["has_summary"])
        self.assertTrue(listed[0]["has_highlight_video"])

    def test_history_shows_storage_problems_instead_of_local_jobs(self):
        with mock.patch.object(self.app.s3, "list_job_manifests",
                               side_effect=s3.HighlightStoreError("s3_access", "S3 refused")):
            response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "s3_access")

    def test_results_are_read_from_storage_when_the_job_is_not_in_memory(self):
        response = self.client.get(f"/api/results/{JOB}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], "A short summary.")

    def test_summary_download_is_a_redirect_to_a_fresh_stored_link(self):
        response = self.client.get(f"/download/summary/{JOB}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fake_s3.key_from_link(response.headers["Location"]), self._key("summary.txt"))
        self.assertTrue(self.store.client.presigned[-1]["params"]["ResponseContentDisposition"].startswith("attachment"))

    def test_a_translation_that_was_never_stored_is_not_found(self):
        self.assertEqual(self.client.get(f"/download/summary/{JOB}/hi").status_code, 404)

    def test_stream_is_inline_and_points_at_the_stored_highlight(self):
        response = self.client.get(f"/stream/highlight/{JOB}/en")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fake_s3.key_from_link(response.headers["Location"]), self._key("highlights/en/highlight_en.mp4"))
        self.assertTrue(self.store.client.presigned[-1]["params"]["ResponseContentDisposition"].startswith("inline"))

    def test_a_local_file_is_never_served_when_storage_has_no_copy(self):
        (self.folder / "highlights" / "hi").mkdir(parents=True)
        (self.folder / "highlights" / "hi" / "highlight_hi.mp4").write_bytes(b"local only")
        response = self.client.get(f"/stream/highlight/{JOB}/hi")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"local only", response.data)

    def test_highlight_link_endpoint_returns_json_and_never_caches(self):
        response = self.client.get(f"/api/highlight-url/{JOB}/en")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(fake_s3.key_from_link(response.get_json()["url"]), self._key("highlights/en/highlight_en.mp4"))

    def test_invalid_job_ids_are_refused_before_any_storage_call(self):
        with mock.patch.object(self.app.s3, "load_job_manifest") as load:
            response = self.client.get("/api/results/ZZZZZZZZ")
        self.assertEqual(response.status_code, 404)
        load.assert_not_called()

    def test_summarize_is_refused_when_storage_is_not_ready_and_starts_nothing(self):
        error = s3.HighlightStoreError("credentials_missing", s3.S3_MESSAGES["credentials_missing"])
        with mock.patch.object(self.app.s3, "ensure_ready", side_effect=error), \
                mock.patch.object(self.app.threading, "Thread") as thread:
            response = self.client.post("/api/summarize", data={"url": "https://example.test/v"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "credentials_missing")
        thread.assert_not_called()
        self.assertEqual(self.app.jobs, {})

    def test_delete_removes_the_job_from_storage_and_history(self):
        response = self.client.delete(f"/api/job/{JOB}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/history").get_json()["jobs"], [])

    def test_a_running_job_cannot_be_deleted(self):
        running = self.app.JobProgress("1f1f1f1f", "clip.mp4")
        self.app.jobs["1f1f1f1f"] = running
        response = self.client.delete("/api/job/1f1f1f1f")
        self.assertEqual(response.status_code, 409)

    def test_retry_downloads_the_job_renders_the_language_and_uploads_only_what_changed(self):
        def fake_retry(job_dir, languages, model=None, compression=None):
            target = Path(job_dir) / "highlights" / "hi" / "highlight_hi.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"new translated highlight")
            return {}

        with mock.patch("src.pipeline_runtime.load_pipeline_runtime", return_value="local"), \
                mock.patch.object(self.app, "retry_highlight_languages", side_effect=fake_retry), \
                mock.patch.object(self.store, "upload_file", wraps=self.store.upload_file) as upload:
            response = self.client.post(f"/api/highlight/retry/{JOB}", json={"languages": ["hi"]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        uploaded = [c.args[1] for c in upload.call_args_list]
        self.assertIn(self._key("highlights/hi/highlight_hi.mp4"), uploaded)
        self.assertNotIn(self._key("source.mp4"), uploaded, "the unchanged source must not be sent again")
        self.assertNotIn(self._key("highlights/en/highlight_en.mp4"), uploaded)
        manifest = fake_s3.manifest(self.store, JOB)
        self.assertIn("highlights/hi/highlight_hi.mp4", manifest["files"])
        self.assertIn("highlights/en/highlight_en.mp4", manifest["files"])


if __name__ == "__main__":
    unittest.main()
