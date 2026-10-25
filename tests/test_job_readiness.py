"""Regression tests: job submission waits for a paused endpoint to accept work, with bounded retries.

RunPod is faked and time is simulated, so no request is sent and nothing sleeps.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import runpod_serverless as rs  # noqa: E402

EP = "4jxzzc08sr5z0w"
JOB = {"input": {"job_type": "heavy_phase"}}


class FakeClock:
    """Each simulated sleep advances time, so timeouts are reached deterministically."""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ReadinessTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.logs = []
        self.live = {"id": EP, "workers": {"min": 0, "max": 0, "idleTimeout": 5}}
        self.patches = []
        self.get = self._patch("get_serverless_endpoint", side_effect=lambda eid: (dict(self.live), None))
        self.patch_ep = self._patch("patch_serverless_endpoint", side_effect=self._apply_patch)
        self.submit = self._patch("submit_job", side_effect=AssertionError("submit not scripted"))

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _patch(self, name, **kwargs):
        p = mock.patch.object(rs, name, **kwargs)
        self.patches.append(p)
        return p.start()

    def _apply_patch(self, endpoint_id, body):
        if "workers" in body:
            self.live["workers"] = dict(body["workers"])
        return {"id": endpoint_id}

    def run_submit(self, timeout_sec=30.0):
        return rs.submit_job_when_ready(
            EP, JOB, log=self.logs.append, timeout_sec=timeout_sec, retry_sec=10.0,
            sleep=self.clock.sleep, clock=self.clock.clock,
        )

    def paused(self):
        return rs.EndpointPausedError("RunPod /run rejected: endpoint paused (HTTP 409): paused")

    def disabled_patches(self):
        return [c for c in self.patch_ep.call_args_list if c.args[1]["workers"]["max"] == 0]


class SuccessfulSubmissionTests(ReadinessTestBase):
    def test_paused_endpoint_is_enabled_then_job_submitted_once(self):
        self.submit.side_effect = ["job-1"]
        job_id = self.run_submit()
        self.assertEqual(job_id, "job-1")
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.patch_ep.call_count, 1, "exactly one capacity change: enable")
        self.assertEqual(self.live["workers"]["max"], 3)
        self.assertEqual(self.disabled_patches(), [], "a successful submit keeps capacity on")

    def test_already_enabled_endpoint_is_not_patched(self):
        self.live["workers"] = {"min": 0, "max": 3, "idleTimeout": 5}
        self.submit.side_effect = ["job-2"]
        self.assertEqual(self.run_submit(), "job-2")
        self.assertEqual(self.patch_ep.call_count, 0)


class DelayedReadinessTests(ReadinessTestBase):
    def test_retries_while_paused_then_succeeds(self):
        self.submit.side_effect = [self.paused(), self.paused(), self.paused(), "job-3"]
        job_id = self.run_submit(timeout_sec=240.0)
        self.assertEqual(job_id, "job-3")
        self.assertEqual(self.submit.call_count, 4)
        self.assertEqual(self.clock.now, 30.0, "three 10 s waits before acceptance")
        self.assertTrue(any("attempt 3" in line for line in self.logs))
        self.assertEqual(self.disabled_patches(), [])


class TimeoutTests(ReadinessTestBase):
    def test_times_out_with_clear_error_and_restores_capacity(self):
        self.submit.side_effect = lambda *_a, **_k: (_ for _ in ()).throw(self.paused())
        with self.assertRaises(rs.EndpointNotReadyError) as ctx:
            self.run_submit(timeout_sec=30.0)
        message = str(ctx.exception)
        self.assertIn("still paused", message)
        self.assertIn("no job was accepted", message)
        self.assertLessEqual(self.submit.call_count, 4, "retries are bounded by the timeout")
        # The submit step never lowers capacity itself. The job lease does, and only when no job remains.
        self.assertEqual(self.disabled_patches(), [], "submit must not lower capacity while a lease may hold it")
        self.assertEqual(self.live["workers"]["max"], 3)


class UnexpectedErrorTests(ReadinessTestBase):
    def test_unexpected_api_error_is_not_retried_and_restores_capacity(self):
        self.submit.side_effect = [RuntimeError("RunPod /run failed (HTTP 500): internal")]
        with self.assertRaises(RuntimeError) as ctx:
            self.run_submit()
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(self.submit.call_count, 1, "non-409 errors are raised at once")
        self.assertEqual(self.disabled_patches(), [], "submit must not lower capacity; the job lease does")

    def test_network_error_is_not_retried_so_no_duplicate_can_be_sent(self):
        self.submit.side_effect = [TimeoutError("read timed out")]
        with self.assertRaises(TimeoutError):
            self.run_submit()
        self.assertEqual(self.submit.call_count, 1)

    def test_unexpected_error_leaves_pre_enabled_endpoint_alone(self):
        self.live["workers"] = {"min": 0, "max": 3, "idleTimeout": 5}
        self.submit.side_effect = [RuntimeError("RunPod /run failed (HTTP 500): internal")]
        with self.assertRaises(RuntimeError):
            self.run_submit()
        self.assertEqual(self.patch_ep.call_count, 0, "capacity we did not change is not touched")



class SubmitErrorClassificationTests(unittest.TestCase):
    def _submit_with(self, status, payload):
        with mock.patch.object(rs, "_inference_request", return_value=(status, payload)):
            return rs.submit_job(EP, JOB)

    def test_409_paused_is_classified_as_retryable(self):
        with self.assertRaises(rs.EndpointPausedError):
            self._submit_with(409, {"detail": "Endpoint is paused (max_workers=0)."})

    def test_other_409_is_not_retryable(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._submit_with(409, {"detail": "job already exists"})
        self.assertNotIsInstance(ctx.exception, rs.EndpointPausedError)

    def test_success_returns_job_id(self):
        self.assertEqual(self._submit_with(200, {"id": "abc"}), "abc")


if __name__ == "__main__":
    unittest.main()
