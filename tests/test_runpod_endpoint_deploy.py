"""Regression tests: serverless deploy reuses existing endpoints, never recreates them on repeated saves,
and reports real RunPod create errors. The RunPod API is replaced by an in-memory fake, so nothing is sent.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import compute_deploy, runpod_serverless  # noqa: E402

IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.9"
NAME = "video-summarizer-RTX A5000"
OFFER = {"name": "RTX A5000", "data_center_id": "CA-MTL-1", "serverless_pool": "AMPERE_24"}


class FakeRunpod:
    """Minimal stand-in for runpod_serverless._request covering the calls the deploy path makes."""

    def __init__(self, endpoints=None, create_status=201, honor_patch=True):
        self.endpoints = {e["id"]: e for e in (endpoints or [])}
        self.create_status = create_status
        self.honor_patch = honor_patch
        self.posts = []

    def __call__(self, method, path, *, body=None, timeout=120):
        if method == "GET" and path == "/serverless":
            return 200, {"endpoints": list(self.endpoints.values()), "pagination": {}}
        if method == "GET" and path.startswith("/serverless/"):
            eid = path.rsplit("/", 1)[-1]
            if eid in self.endpoints:
                return 200, self.endpoints[eid]
            return 404, {"detail": "endpoint not found"}
        if method == "PATCH" and path.startswith("/serverless/"):
            # Capacity-only updates, as the lifecycle code sends them. Other changes are not expected here.
            eid = path.rsplit("/", 1)[-1]
            if eid not in self.endpoints:
                return 404, {"detail": "endpoint not found"}
            if set(body) != {"workers"}:
                raise AssertionError(f"unexpected PATCH body keys: {sorted(body)}")
            if self.honor_patch:
                self.endpoints[eid]["workers"] = dict(body["workers"])
            return 200, self.endpoints[eid]
        if method == "POST" and path == "/serverless":
            self.posts.append(body)
            if self.create_status in (200, 201):
                ep = {"id": "new123", "name": body["name"], "image": body["image"],
                      "workers": {"min": 0, "max": 3, "idleTimeout": 5}}
                self.endpoints["new123"] = ep
                return self.create_status, ep
            return self.create_status, {
                "detail": "endpoint template names must be unique. check that you do not already have an endpoint with a similar template."
            }
        raise AssertionError(f"unexpected RunPod call: {method} {path}")


def _existing(eid="ep1", name=NAME, image=IMAGE, max_workers=3):
    return {"id": eid, "name": name, "image": image, "requestUrls": {},
            "workers": {"min": 0, "max": max_workers, "idleTimeout": 5}}


class DeployTestBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        # Lease files go to a temporary folder, never the project's data/ folder.
        import tempfile

        lease_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lease_tmp.cleanup)
        lease_patch = mock.patch("src.endpoint_capacity.LEASE_DIR", Path(lease_tmp.name))
        lease_patch.start()
        self.addCleanup(lease_patch.stop)
        os.environ.update({
            "RUNPOD_SERVERLESS_IMAGE": IMAGE,
            "RUNPOD_NETWORK_VOLUME_ID": "vol1",
            "RUNPOD_API_KEY": "test-only-not-a-secret",
        })
        os.environ.pop("RUNPOD_SERVERLESS_NAME", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def deploy(self, fake, saved):
        with mock.patch.object(runpod_serverless, "_request", side_effect=fake), mock.patch.object(
            compute_deploy, "load_saved_selection", return_value=saved
        ):
            return compute_deploy._deploy_runpod_serverless(dict(OFFER))


class ReuseExistingEndpointTests(DeployTestBase):
    def test_reuses_endpoint_saved_by_id_without_creating(self):
        fake = FakeRunpod(endpoints=[_existing("ep1")])
        result = self.deploy(fake, {"serverless_endpoint_id": "ep1"})
        self.assertEqual(result["status"], "deployed")
        self.assertTrue(result["reused"])
        self.assertEqual(result["endpoint_id"], "ep1")
        self.assertEqual(fake.posts, [], "no endpoint may be created when one is already saved")

    def test_reuses_endpoint_found_by_name_when_id_missing(self):
        fake = FakeRunpod(endpoints=[_existing("ep1")])
        result = self.deploy(fake, {})
        self.assertTrue(result["reused"])
        self.assertEqual(result["endpoint_id"], "ep1")
        self.assertEqual(fake.posts, [])

    def test_reuse_reports_image_mismatch_and_does_not_change_it(self):
        fake = FakeRunpod(endpoints=[_existing("ep1", image="sanjayuk80603/video-summarizer-worker:v1.0.6")])
        result = self.deploy(fake, {"serverless_endpoint_id": "ep1"})
        self.assertTrue(result["reused"])
        self.assertIn("not changed automatically", result["message"])
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.endpoints["ep1"]["image"], "sanjayuk80603/video-summarizer-worker:v1.0.6")


class CreateNewEndpointTests(DeployTestBase):
    def test_creates_with_deterministic_name_when_nothing_exists(self):
        fake = FakeRunpod(endpoints=[])
        result = self.deploy(fake, {})
        self.assertEqual(result["status"], "deployed")
        self.assertFalse(result["reused"])
        self.assertEqual(result["endpoint_id"], "new123")
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0]["name"], NAME)

    def test_creation_that_is_not_verified_at_zero_reports_failure(self):
        # RunPod accepts the capacity change but max stays 3. The deploy must not report success.
        fake = FakeRunpod(endpoints=[], honor_patch=False)
        result = self.deploy(fake, {})
        self.assertEqual(result["status"], "failed")
        self.assertIn("new123", result["message"])
        self.assertIn("expected 0", result["message"])
        self.assertEqual(result["endpoint_id"], "", "an unverified endpoint is not saved as the active endpoint")

    def test_creation_sets_capacity_to_zero_and_reads_it_back(self):
        fake = FakeRunpod(endpoints=[])
        result = self.deploy(fake, {})
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(fake.posts[0]["workers"]["max"], 0, "the create request itself must start at max 0")
        self.assertEqual(fake.endpoints["new123"]["workers"]["max"], 0)

    def test_duplicate_name_error_is_reported_with_status_and_detail(self):
        fake = FakeRunpod(endpoints=[], create_status=400)
        result = self.deploy(fake, {})
        self.assertEqual(result["status"], "failed")
        self.assertIn("HTTP 400", result["message"])
        self.assertIn("must be unique", result["message"])
        self.assertEqual(result["endpoint_id"], "")


class RepeatedDeployTests(DeployTestBase):
    def test_second_attempt_reuses_what_the_first_created(self):
        fake = FakeRunpod(endpoints=[])
        first = self.deploy(fake, {})
        self.assertEqual(first["endpoint_id"], "new123")
        # The save flow keeps the id it learned from the first deploy.
        saved_after_first = {"serverless_endpoint_id": first["endpoint_id"]}
        second = self.deploy(fake, saved_after_first)
        self.assertTrue(second["reused"])
        self.assertEqual(second["endpoint_id"], "new123")
        self.assertEqual(len(fake.posts), 1, "repeated deploy must not create a second endpoint")


class UniqueNameTests(unittest.TestCase):
    def test_free_name_is_used_as_is(self):
        self.assertEqual(runpod_serverless.unique_endpoint_name("video-summarizer-RTX A5000", set()), NAME)

    def test_taken_name_gets_timestamp_suffix_within_limit(self):
        name = runpod_serverless.unique_endpoint_name(NAME, {NAME})
        self.assertTrue(name.startswith(NAME + "-"))
        self.assertLessEqual(len(name), 63)
        self.assertNotEqual(name, NAME)


class SavedEndpointIdTests(unittest.TestCase):
    def test_explicit_field_wins(self):
        saved = {"serverless_endpoint_id": "explicit", "deploy": {"endpoint_id": "old"}}
        self.assertEqual(compute_deploy.saved_serverless_endpoint_id(saved), "explicit")

    def test_falls_back_to_last_deploy_record(self):
        self.assertEqual(compute_deploy.saved_serverless_endpoint_id({"deploy": {"endpoint_id": "ep9"}}), "ep9")

    def test_empty_when_nothing_known(self):
        self.assertEqual(compute_deploy.saved_serverless_endpoint_id({"deploy": {"endpoint_id": ""}}), "")
        self.assertEqual(compute_deploy.saved_serverless_endpoint_id(None), "")


class JobEndpointLookupTests(unittest.TestCase):
    def test_job_uses_saved_endpoint_even_after_failed_deploy_cleared_deploy_record(self):
        saved = {"serverless_endpoint_id": "ep1", "deploy": {"status": "failed", "endpoint_id": ""}}
        with mock.patch("src.compute_deploy.load_saved_selection", return_value=saved):
            self.assertEqual(runpod_serverless.load_endpoint_id_from_selection(), "ep1")

    def test_job_raises_when_no_endpoint_is_known(self):
        with mock.patch("src.compute_deploy.load_saved_selection", return_value={"deploy": {}}):
            with self.assertRaises(RuntimeError):
                runpod_serverless.load_endpoint_id_from_selection()


if __name__ == "__main__":
    unittest.main()
