"""When the status cache is cleared, and that it never shows a failed or expired read as current.

Routes that change RunPod resources must clear the cache only after a confirmed success. Failed calls leave the
cache alone; its 5-second TTL bounds how old a snapshot can be. No RunPod call is made: operations are patched.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import serverless_status_cache as cache  # noqa: E402

try:
    import app as app_module  # noqa: E402
except ImportError:  # the worker image does not ship the Flask app
    app_module = None

OK_SNAPSHOT = {"config": {"workers": {"min": 0, "max": 3}}, "config_error": None,
               "health": {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
               "health_error": None, "fetched_at": "2026-01-01T00:00:00Z"}


def _fresh_snapshot(endpoint_id):
    return dict(OK_SNAPSHOT)


class CacheIsolationTests(unittest.TestCase):
    def setUp(self):
        cache._cache.clear()
        cache._inflight.clear()
        self.clock_value = [100.0]
        self.clock = lambda: self.clock_value[0]

    def test_a_failure_for_one_endpoint_does_not_touch_another_endpoints_snapshot(self):
        cache.get_live_snapshot("EP1", fetch=_fresh_snapshot, clock=self.clock)
        cache.get_live_snapshot("EP2", fetch=lambda e: dict(OK_SNAPSHOT, config=None, config_error="timeout"),
                                clock=self.clock)
        second = cache.get_live_snapshot("EP1", fetch=lambda e: self.fail("EP1 should still be cached"),
                                         clock=self.clock)
        self.assertTrue(second["cached"])
        self.assertIsNone(second["config_error"])

    def test_an_expired_success_is_not_shown_when_the_next_read_fails(self):
        cache.get_live_snapshot("EP1", fetch=_fresh_snapshot, clock=self.clock)
        self.clock_value[0] += 6.0  # past the TTL
        failed = cache.get_live_snapshot(
            "EP1", fetch=lambda e: dict(OK_SNAPSHOT, config=None, config_error="timeout"), clock=self.clock)
        self.assertEqual(failed["config_error"], "timeout", "the old success must not be returned as current")
        self.assertFalse(failed["cached"])

    def test_a_failed_snapshot_is_never_stored(self):
        cache.get_live_snapshot("EP1", fetch=lambda e: dict(OK_SNAPSHOT, health=None, health_error="HTTP 503"),
                                clock=self.clock)
        self.assertNotIn("EP1", cache._cache)

    def fail(self, message):
        raise AssertionError(message)


@unittest.skipIf(app_module is None, "app.py is not part of the worker image")
class RouteInvalidationTests(unittest.TestCase):
    def setUp(self):
        cache._cache.clear()
        cache._inflight.clear()
        cache._cache["EP-CACHED"] = (0.0, dict(OK_SNAPSHOT))
        self.client = app_module.app.test_client()

    def _cached(self):
        return "EP-CACHED" in cache._cache

    def test_stop_clears_the_cache_only_after_success(self):
        with mock.patch.object(app_module, "stop_saved_runpod_pod", return_value={"ok": False, "error": "x"}), \
                mock.patch.object(app_module, "get_compute_deploy_status", return_value={}):
            self.client.post("/api/compute-deploy-stop")
        self.assertTrue(self._cached(), "a failed stop must not clear the cache")
        with mock.patch.object(app_module, "stop_saved_runpod_pod", return_value={"ok": True}), \
                mock.patch.object(app_module, "get_compute_deploy_status", return_value={}):
            self.client.post("/api/compute-deploy-stop")
        self.assertFalse(self._cached(), "a confirmed stop must clear the cache")

    def test_delete_volume_clears_the_cache_only_after_success(self):
        with mock.patch.object(app_module, "delete_saved_network_volume", return_value={"ok": False, "error": "x"}), \
                mock.patch.object(app_module, "get_compute_deploy_status", return_value={}):
            self.client.post("/api/compute-network-volume-delete", json={"confirm": "DELETE"})
        self.assertTrue(self._cached())
        with mock.patch.object(app_module, "delete_saved_network_volume", return_value={"ok": True}), \
                mock.patch.object(app_module, "get_compute_deploy_status", return_value={}):
            self.client.post("/api/compute-network-volume-delete", json={"confirm": "DELETE"})
        self.assertFalse(self._cached())

    def test_deploy_save_clears_the_cache_only_after_success(self):
        body = {"selection_id": "sl-1", "offer": None}
        with mock.patch.object(app_module, "save_selection_and_deploy", return_value={"ok": False, "error": "x"}), \
                mock.patch("src.compute_deploy.load_compute_mode", return_value="pod"):
            self.client.post("/api/gpu-selection", json=body)
        self.assertTrue(self._cached(), "a failed save must not clear the cache")
        with mock.patch.object(app_module, "save_selection_and_deploy", return_value={"ok": True, "deploy": {}}), \
                mock.patch.object(app_module, "get_compute_deploy_status", return_value={}), \
                mock.patch("src.compute_deploy.load_compute_mode", return_value="pod"):
            self.client.post("/api/gpu-selection", json=body)
        self.assertFalse(self._cached())


if __name__ == "__main__":
    unittest.main()
