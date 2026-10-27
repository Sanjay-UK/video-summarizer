"""Mapping RunPod serverless provider data to user-facing live states.

Inputs mirror the documented provider responses: settings (workers.max) and health (jobs.inQueue, jobs.inProgress,
workers.idle, workers.running). Unknown or failed reads must never map to a healthy state.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import serverless_live_status as ls  # noqa: E402

CHECKED = "2026-01-01T00:00:00Z"


def _health(idle=0, running=0, in_queue=0, in_progress=0):
    return {
        "jobs": {"completed": 0, "failed": 0, "inProgress": in_progress, "inQueue": in_queue, "retried": 0},
        "workers": {"idle": idle, "running": running},
    }


def _map(max_workers=3, health=None, health_error=None, config_error=None, configured=True):
    config = {"workers": {"min": 0, "max": max_workers, "idleTimeout": 5}} if max_workers is not None else {}
    return ls.map_live_status(
        endpoint_configured=configured,
        config=None if config_error else config,
        config_error=config_error,
        health=health,
        health_error=health_error,
        checked_at=CHECKED,
    )


class ProviderMappingTests(unittest.TestCase):
    def test_no_endpoint_is_not_deployed(self):
        self.assertEqual(_map(configured=False)["key"], ls.NOT_DEPLOYED)

    def test_idle_needs_both_reads_and_zero_counts(self):
        result = _map(health=_health())
        self.assertEqual(result["key"], ls.IDLE)
        self.assertEqual(result["label"], "Idle — no workers running")
        self.assertEqual(result["tone"], "gray")
        self.assertIn("Zero workers is normal", result["explanation"])

    def test_zero_workers_alone_is_not_an_error(self):
        self.assertEqual(_map(health=_health())["tone"], "gray")

    def test_reported_running_worker_is_running_and_is_not_proof_of_processing(self):
        result = _map(health=_health(running=1))
        self.assertEqual(result["key"], ls.RUNNING)
        self.assertEqual(result["label"], "Running")
        self.assertEqual(result["tone"], "green")
        self.assertIn("does not by itself show that a video is being processed", result["explanation"])

    def test_idle_only_is_not_labelled_running_or_idle(self):
        result = _map(health=_health(idle=2))
        self.assertEqual(result["key"], ls.WORKERS_IDLE_ONLY)
        self.assertEqual(result["label"], "Workers reported idle")
        self.assertIn("What 'idle' covers is not documented", result["explanation"])

    def test_queued_with_no_worker_and_capacity_available_is_waiting_and_marked_as_inference(self):
        result = _map(max_workers=3, health=_health(in_queue=1))
        self.assertEqual(result["key"], ls.WAITING_CAPACITY)
        self.assertEqual(result["label"], "Waiting for worker capacity")
        self.assertEqual(result["tone"], "blue")
        self.assertIn("inference", result["explanation"])

    def test_queued_while_max_workers_is_zero_explains_possible_block(self):
        result = _map(max_workers=0, health=_health(in_queue=1))
        self.assertEqual(result["key"], ls.QUEUED_MAX_ZERO)
        self.assertEqual(result["tone"], "amber")
        self.assertIn("may be blocked until worker capacity is enabled", result["explanation"])
        self.assertIn("inference", result["explanation"])

    def test_queue_with_a_reported_running_worker_is_running_not_waiting(self):
        self.assertEqual(_map(health=_health(running=1, in_queue=2))["key"], ls.RUNNING)

    def test_settings_read_failure_is_unavailable_even_if_health_looks_empty(self):
        result = _map(config_error="timeout", health=_health())
        self.assertEqual(result["key"], ls.UNAVAILABLE)
        self.assertNotEqual(result["key"], ls.IDLE)

    def test_health_read_failure_is_unavailable_not_idle(self):
        result = _map(health=None, health_error="health check failed (URLError)")
        self.assertEqual(result["key"], ls.UNAVAILABLE)
        self.assertEqual(result["tone"], "amber")
        self.assertIn("does not mean the service is down", result["explanation"])

    def test_missing_or_malformed_health_fields_are_unavailable(self):
        for bad in ({}, {"workers": {"idle": 0}}, {"workers": "x", "jobs": {}},
                    {"workers": {"idle": -1, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
                    {"workers": {"idle": True, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}}):
            with self.subTest(health=bad):
                self.assertEqual(_map(health=bad)["key"], ls.UNAVAILABLE)

    def test_unknown_settings_never_map_to_idle(self):
        result = ls.map_live_status(endpoint_configured=True, config={"workers": {"min": 0}}, config_error=None,
                                    health=_health(), health_error=None, checked_at=CHECKED)
        self.assertEqual(result["key"], ls.UNAVAILABLE)

    def test_unsupported_states_are_not_exposed(self):
        """'Starting', 'Stopping', and 'Attention required' need evidence the API does not provide."""
        labels = set(ls._LABELS.values())
        for forbidden in ("Starting", "Stopping", "Attention required"):
            self.assertFalse(any(forbidden in label for label in labels), forbidden)
        keys = {_map(health=_health(idle=i, running=r, in_queue=q), max_workers=m)["key"]
                for i in (0, 1, 2) for r in (0, 1, 2) for q in (0, 1) for m in (0, 3)}
        self.assertTrue(keys <= {ls.NOT_DEPLOYED, ls.UNAVAILABLE, ls.IDLE, ls.RUNNING,
                                 ls.WORKERS_IDLE_ONLY, ls.WAITING_CAPACITY, ls.QUEUED_MAX_ZERO})

    def test_counts_are_kept_separate_and_never_summed(self):
        result = _map(health=_health(idle=1, running=2, in_queue=3))
        self.assertEqual(result["workers"], {"idle": 1, "running": 2})
        self.assertEqual(result["jobs"], {"inQueue": 3, "inProgress": 0})
        self.assertNotIn("3 worker", result["explanation"], "the two counts must not be added together")
        self.assertEqual(result["max_workers"], 3)
        self.assertEqual(result["checked_at"], CHECKED)


class ProviderCallTests(unittest.TestCase):
    def test_health_check_is_a_read_only_get_to_the_health_path(self):
        from src import runpod_serverless as rs

        calls = []

        def fake(method, endpoint_id, subpath, *, body=None, timeout=120):
            calls.append((method, endpoint_id, subpath, body))
            return 200, _health()

        with mock.patch.object(rs, "_inference_request", side_effect=fake):
            payload, err = rs.get_serverless_health("EP123", timeout=5)
        self.assertIsNone(err)
        self.assertEqual(payload["workers"]["idle"], 0)
        self.assertEqual(calls, [("GET", "EP123", "/health", None)])

    def test_health_http_error_and_exceptions_are_reported_not_raised(self):
        from src import runpod_serverless as rs

        with mock.patch.object(rs, "_inference_request", return_value=(503, {"detail": "down"})):
            payload, err = rs.get_serverless_health("EP123")
        self.assertIsNone(payload)
        self.assertIn("HTTP 503", err)
        with mock.patch.object(rs, "_inference_request", side_effect=OSError("boom")):
            payload, err = rs.get_serverless_health("EP123")
        self.assertIsNone(payload)
        self.assertIn("OSError", err)


if __name__ == "__main__":
    unittest.main()
