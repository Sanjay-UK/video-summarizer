"""The deploy-status builder uses read-only provider data and never reports a healthy state from a failed read.

Provider calls are replaced with fakes; no RunPod request is made. The fakes also record every call so the test can
show that status reads are GET-only.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import runpod_serverless as rs  # noqa: E402

try:
    import app as app_module  # noqa: E402
except ImportError:  # the worker image does not ship the Flask app
    app_module = None


def _out():
    return {"hardware": {"price_per_hour": 0.27}, "deploy_record_status": "deployed"}


def _deploy():
    return {"endpoint_id": "EP123", "status": "deployed"}


class LiveStatusBuilderTests(unittest.TestCase):
    def setUp(self):
        from src import serverless_status_cache as cache

        cache.invalidate_all()
        self.addCleanup(cache.invalidate_all)

    def _build(self, config, config_err, health, health_err):
        from src import compute_status as cs

        calls = []

        def fake_config(endpoint_id, *, timeout=None):
            calls.append(("config", endpoint_id))
            return config, config_err

        def fake_health(endpoint_id, *, timeout=10.0):
            calls.append(("health", endpoint_id))
            return health, health_err

        out = _out()
        with mock.patch.object(rs, "get_serverless_endpoint", side_effect=fake_config), \
                mock.patch.object(rs, "get_serverless_health", side_effect=fake_health):
            cs._serverless_deploy_status(out, _deploy(), {}, refresh_live=True)
        return out, calls

    def test_idle_endpoint_with_confirmed_reads(self):
        out, calls = self._build({"workers": {"min": 0, "max": 3, "idleTimeout": 5}}, None,
                                 {"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None)
        self.assertEqual(out["live"]["key"], "idle")
        self.assertEqual(out["phase_label"], "Idle — no workers running")
        self.assertEqual(calls, [("config", "EP123"), ("health", "EP123")])

    def test_queued_job_with_capacity_available_reports_waiting_as_inference(self):
        out, _ = self._build({"workers": {"min": 0, "max": 3}}, None,
                             {"jobs": {"inQueue": 2, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None)
        self.assertEqual(out["live"]["key"], "waiting_for_capacity")
        self.assertEqual(out["phase_label"], "Waiting for worker capacity")
        self.assertIn("inference", out["live"]["explanation"])

    def test_queued_job_with_max_zero_explains_possible_block(self):
        out, _ = self._build({"workers": {"min": 0, "max": 0}}, None,
                             {"jobs": {"inQueue": 1, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None)
        self.assertEqual(out["live"]["key"], "queued_max_zero")
        self.assertIn("may be blocked", out["live"]["explanation"])

    def test_two_status_reads_within_the_ttl_make_one_provider_read_each(self):
        from src import compute_status as cs

        config_calls = []
        health_calls = []

        def fake_config(endpoint_id, *, timeout=None):
            config_calls.append(endpoint_id)
            return {"workers": {"min": 0, "max": 3}}, None

        def fake_health(endpoint_id, *, timeout=10.0):
            health_calls.append(endpoint_id)
            return {"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None

        with mock.patch.object(rs, "get_serverless_endpoint", side_effect=fake_config), \
                mock.patch.object(rs, "get_serverless_health", side_effect=fake_health):
            for _ in range(3):
                cs._serverless_deploy_status(_out(), _deploy(), {}, refresh_live=True)
        self.assertEqual(config_calls, ["EP123"])
        self.assertEqual(health_calls, ["EP123"])

    def test_failed_read_is_not_reused_as_healthy_on_the_next_request(self):
        from src import compute_status as cs

        results = []
        with mock.patch.object(rs, "get_serverless_endpoint", return_value=(None, "timeout")), \
                mock.patch.object(rs, "get_serverless_health", return_value=(None, None)):
            results.append(cs._serverless_deploy_status(_out(), _deploy(), {}, refresh_live=True)["live"]["key"])
        with mock.patch.object(rs, "get_serverless_endpoint", return_value=({"workers": {"min": 0, "max": 3}}, None)), \
                mock.patch.object(rs, "get_serverless_health", return_value=({"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None)):
            results.append(cs._serverless_deploy_status(_out(), _deploy(), {}, refresh_live=True)["live"]["key"])
        self.assertEqual(results, ["unavailable", "idle"])

    def test_failed_settings_read_is_unavailable_and_skips_the_health_read(self):
        out, calls = self._build(None, "timeout talking to RunPod", None, None)
        self.assertEqual(out["live"]["key"], "unavailable")
        self.assertNotEqual(out.get("phase"), "stopped")
        self.assertEqual(calls, [("config", "EP123")])

    def test_failed_health_read_is_unavailable_not_idle(self):
        out, _ = self._build({"workers": {"min": 0, "max": 3}}, None, None, "health check returned HTTP 503")
        self.assertEqual(out["live"]["key"], "unavailable")
        self.assertIsNot(out["live"]["key"], "idle")

    def test_zero_max_workers_with_confirmed_idle_health_is_idle_not_an_error(self):
        out, _ = self._build({"workers": {"min": 0, "max": 0}}, None,
                             {"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}, None)
        self.assertEqual(out["live"]["key"], "idle")
        self.assertEqual(out["phase"], "stopped")

    def test_status_reads_are_get_only(self):
        seen = []

        def fake_inference(method, endpoint_id, subpath, *, body=None, timeout=120):
            seen.append(method)
            return 200, {"jobs": {"inQueue": 0, "inProgress": 0}, "workers": {"idle": 0, "running": 0}}

        with mock.patch.object(rs, "_inference_request", side_effect=fake_inference):
            rs.get_serverless_health("EP123")
        self.assertEqual(seen, ["GET"])

    @unittest.skipIf(app_module is None, "app.py is not part of the worker image")
    def test_status_route_accepts_only_get(self):
        rules = [r for r in app_module.app.url_map.iter_rules() if r.rule == "/api/compute-deploy-status"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(set(rules[0].methods) - {"HEAD", "OPTIONS"}, {"GET"})


if __name__ == "__main__":
    unittest.main()
