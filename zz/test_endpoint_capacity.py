"""Regression tests for the endpoint-capacity lease lifecycle (src/endpoint_capacity.py).

RunPod is faked: capacity changes update an in-memory endpoint, and reads return it, so read-back
verification is exercised. Every lease file lives in a temporary folder. Runs on macOS and Windows.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from filelock import FileLock, Timeout  # noqa: E402

from src import endpoint_capacity as ec  # noqa: E402
from src import runpod_serverless as rs  # noqa: E402

EP = "4jxzzc08sr5z0w"


class FakeEndpoint:
    """Holds max_workers. Every PATCH is recorded; set honor_patch=False to simulate a change RunPod ignores."""

    def __init__(self, max_workers=3, honor_patch=True, fail_patch=None, fail_get=None):
        self.max = max_workers
        self.honor_patch = honor_patch
        self.fail_patch = fail_patch
        self.fail_get = fail_get
        self.patches = []
        self.timeouts = []

    def patch(self, endpoint_id, body, timeout=None):
        self.timeouts.append(timeout)
        if self.fail_patch:
            raise self.fail_patch
        self.patches.append(body["workers"]["max"])
        if self.honor_patch:
            self.max = body["workers"]["max"]
        return {"id": endpoint_id}

    def get(self, endpoint_id, timeout=None):
        self.timeouts.append(timeout)
        if self.fail_get:
            raise self.fail_get
        return {"id": endpoint_id, "workers": {"min": 0, "max": self.max, "idleTimeout": 5}}, None


class CapacityTestBase(unittest.TestCase):
    max_start = 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lease_dir = Path(self.tmp.name)
        self.logs = []
        self.fake = FakeEndpoint(max_workers=self.max_start)
        for target, name, kwargs in [
            (ec, "LEASE_DIR", {"new": self.lease_dir}),
            (rs, "patch_serverless_endpoint", {"side_effect": self.fake.patch}),
            (rs, "get_serverless_endpoint", {"side_effect": self.fake.get}),
        ]:
            p = mock.patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def lease_file(self):
        return self.lease_dir / f"{ec._safe_name(EP)}.json"

    def leases_on_disk(self):
        path = self.lease_file()
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def write_leases(self, leases):
        self.lease_file().write_text(json.dumps(leases), encoding="utf-8")

    def zero_patches(self):
        return [m for m in self.fake.patches if m == 0]


class ReleaseSafetyTests(CapacityTestBase):
    max_start = 0

    def test_success_lowers_once_after_the_job_and_verifies(self):
        with ec.job_lease(EP, "heavy", self.logs.append, lease_dir=self.lease_dir):
            self.assertEqual(self.zero_patches(), [], "no lowering while the job is active")
        self.assertEqual(self.leases_on_disk(), [])
        self.assertEqual(self.fake.max, 0)
        self.assertTrue(any("verified" in line for line in self.logs))

    def test_release_failure_does_not_mask_the_job_error(self):
        # Regression: a failing lease write during release used to replace the job's own ValueError.
        real_write = ec._write
        calls = {"n": 0}

        def flaky_write(path, leases):
            calls["n"] += 1
            if calls["n"] == 2:  # the release write, after the job has raised
                raise OSError("disk full")
            return real_write(path, leases)

        with mock.patch.object(ec, "_write", side_effect=flaky_write):
            with self.assertRaises(ValueError) as ctx:
                with ec.job_lease(EP, "heavy", self.logs.append, lease_dir=self.lease_dir):
                    raise ValueError("the real job error")
        self.assertEqual(str(ctx.exception), "the real job error")
        self.assertTrue(any("lease release failed" in line for line in self.logs))

    def test_capacity_failure_during_release_does_not_mask_the_job_error(self):
        self.fake.fail_patch = RuntimeError("network down")
        with self.assertRaises(ValueError) as ctx:
            with ec.job_lease(EP, "heavy", self.logs.append, lease_dir=self.lease_dir):
                raise ValueError("the real job error")
        self.assertEqual(str(ctx.exception), "the real job error")
        self.assertTrue(any("could not restore max workers to 0" in line for line in self.logs))
        self.assertEqual(self.leases_on_disk(), [], "the lease is released even when the capacity change fails")

    def test_timeout_releases_and_lowers(self):
        with mock.patch.object(rs, "submit_job_when_ready", side_effect=rs.EndpointNotReadyError("still paused")):
            with self.assertRaises(rs.EndpointNotReadyError):
                rs.submit_and_wait(EP, {"input": {}}, label="heavy", log=self.logs.append)
        self.assertEqual(self.leases_on_disk(), [])
        self.assertEqual(self.fake.max, 0)

    def test_submit_and_wait_success_lowers_after_completion(self):
        with mock.patch.object(rs, "submit_job_when_ready", return_value="job-1"), mock.patch.object(
            rs, "poll_job_until_done", return_value={"status": "COMPLETED", "output": {"ok": True}}
        ):
            result = rs.submit_and_wait(EP, {"input": {}}, label="warmup", log=self.logs.append)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.fake.max, 0)


class OverlapTests(CapacityTestBase):
    max_start = 0

    def test_capacity_stays_up_until_the_last_job_releases(self):
        a = ec.acquire_lease(EP, "heavy", lease_dir=self.lease_dir)
        b = ec.acquire_lease(EP, "highlight", lease_dir=self.lease_dir)
        self.assertEqual(ec.release_lease(EP, a, self.logs.append, lease_dir=self.lease_dir), 1)
        self.assertEqual(self.zero_patches(), [])
        self.assertEqual(ec.release_lease(EP, b, self.logs.append, lease_dir=self.lease_dir), 0)
        self.assertEqual(len(self.zero_patches()), 1)

    def test_nested_jobs_lower_only_at_the_outer_exit(self):
        with ec.job_lease(EP, "outer", self.logs.append, lease_dir=self.lease_dir):
            with ec.job_lease(EP, "inner", self.logs.append, lease_dir=self.lease_dir):
                pass
            self.assertEqual(self.zero_patches(), [])
        self.assertEqual(len(self.zero_patches()), 1)

    def test_concurrent_jobs_never_see_capacity_lowered_while_leases_exist(self):
        errors = []
        seen_during_zero = []

        def checked_patch(endpoint_id, body, timeout=None):
            if body["workers"]["max"] == 0:
                seen_during_zero.append(self.leases_on_disk())
            return self.fake.patch(endpoint_id, body, timeout)

        p = mock.patch.object(rs, "patch_serverless_endpoint", side_effect=checked_patch)
        p.start()
        self.addCleanup(p.stop)
        barrier = threading.Barrier(6)

        def worker(i):
            try:
                barrier.wait()
                with ec.job_lease(EP, f"job{i}", lambda _m: None, lease_dir=self.lease_dir):
                    time.sleep(0.01)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(self.leases_on_disk(), [])
        self.assertGreaterEqual(len(seen_during_zero), 1)
        for leases in seen_during_zero:
            self.assertEqual(leases, [], "capacity was lowered while another job held a lease")


class ExpiryTests(CapacityTestBase):
    max_start = 0

    def test_fresh_lease_is_kept(self):
        self.write_leases([{"token": "t", "label": "x", "acquired_at": time.time()}])
        self.assertEqual(ec.active_lease_count(EP, lease_dir=self.lease_dir), 1)

    def test_lease_older_than_threshold_is_expired(self):
        old = time.time() - ec.LEASE_TTL_SEC - 1
        self.write_leases([{"token": "t", "label": "x", "acquired_at": old}])
        self.assertEqual(ec.active_lease_count(EP, lease_dir=self.lease_dir), 0)

    def test_lease_just_inside_threshold_is_kept(self):
        recent = time.time() - ec.LEASE_TTL_SEC + 60
        self.write_leases([{"token": "t", "label": "x", "acquired_at": recent}])
        self.assertEqual(ec.active_lease_count(EP, lease_dir=self.lease_dir), 1)

    def test_malformed_lease_is_ignored(self):
        self.write_leases([{"token": "t", "label": "x", "acquired_at": "not-a-number"}])
        self.assertEqual(ec.active_lease_count(EP, lease_dir=self.lease_dir), 0)

    def test_no_process_liveness_check_is_used(self):
        source = Path(ec.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.kill", source, "os.kill(pid, 0) terminates processes on Windows")
        self.assertNotIn('"pid"', source, "lease records must not depend on process IDs")
        self.assertNotIn("getpid", source, "lease records must not depend on process IDs")


class LowerIfIdleTests(CapacityTestBase):
    def test_lowers_when_idle_and_verifies(self):
        self.fake.max = 3
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertTrue(result.ok)
        self.assertTrue(result.lowered)
        self.assertEqual(self.fake.max, 0)

    def test_does_not_lower_while_a_valid_lease_exists(self):
        self.fake.max = 3
        token = ec.acquire_lease(EP, "running", lease_dir=self.lease_dir)
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertTrue(result.ok)
        self.assertFalse(result.lowered)
        self.assertEqual(self.fake.max, 3)
        ec.release_lease(EP, token, self.logs.append, lease_dir=self.lease_dir)

    def test_read_back_mismatch_is_reported_as_failure(self):
        self.fake.honor_patch = False  # RunPod accepts the call but max stays 3
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertFalse(result.ok)
        self.assertIn("expected 0", result.message)

    def test_patch_error_is_reported_not_raised(self):
        self.fake.fail_patch = RuntimeError("HTTP 500")
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.message)

    def test_unreadable_endpoint_is_reported_not_raised(self):
        self.fake.fail_get = ConnectionError("no route")
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertFalse(result.ok)
        self.assertIn("could not read endpoint back", result.message)


class StartupRecoveryTests(CapacityTestBase):
    def test_expired_leases_are_pruned_and_capacity_lowered(self):
        self.fake.max = 3
        self.write_leases([{"token": "t", "label": "x", "acquired_at": time.time() - ec.LEASE_TTL_SEC - 5}])
        result = ec.startup_recovery(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertTrue(result.ok)
        self.assertEqual(self.leases_on_disk(), [])
        self.assertEqual(self.fake.max, 0)

    def test_valid_lease_blocks_recovery_lowering(self):
        self.fake.max = 3
        self.write_leases([{"token": "t", "label": "x", "acquired_at": time.time()}])
        result = ec.startup_recovery(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertTrue(result.ok)
        self.assertFalse(result.lowered)
        self.assertEqual(self.fake.max, 3)

    def test_unreachable_runpod_is_reported_clearly(self):
        self.fake.fail_get = ConnectionError("no route")
        result = ec.startup_recovery(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertFalse(result.ok)
        self.assertTrue(any("WARNING" in line for line in self.logs))

    def test_startup_check_never_raises_even_when_storage_is_broken(self):
        out = []
        with mock.patch("src.compute_deploy.load_saved_selection", side_effect=OSError("disk gone")):
            result = ec.startup_check(out.append)
        self.assertIsNone(result)
        self.assertTrue(any("could not run" in line for line in out))

    def test_startup_check_is_a_no_op_without_a_saved_endpoint(self):
        with mock.patch("src.compute_deploy.load_saved_selection", return_value={"deploy": {}}):
            self.assertIsNone(ec.startup_check(lambda _m: None))


class LockingTests(CapacityTestBase):
    def test_lock_is_taken_through_filelock_on_every_platform(self):
        source = Path(ec.__file__).read_text(encoding="utf-8")
        self.assertIn("from filelock import FileLock", source)
        self.assertNotIn("fcntl", source, "the no-op Windows fallback has been removed")

    def test_second_holder_times_out_with_a_clear_error(self):
        lock_path = self.lease_dir / f"{ec._safe_name(EP)}.lock"
        holder = FileLock(str(lock_path), timeout=5)
        holder.acquire()
        try:
            with mock.patch.object(ec, "LOCK_TIMEOUT_SEC", 0.2):
                with self.assertRaises(ec.CapacityLockError):
                    ec.acquire_lease(EP, "blocked", lease_dir=self.lease_dir)
        finally:
            holder.release()

    def test_lock_path_with_spaces_and_unicode_works(self):
        base = self.lease_dir / "dir with spaces ünïcode"
        token = ec.acquire_lease(EP, "x", lease_dir=base)
        self.assertEqual(ec.release_lease(EP, token, self.logs.append, lease_dir=base), 0)


class BackgroundRecoveryTests(CapacityTestBase):
    def _saved(self):
        return mock.patch("src.compute_deploy.load_saved_selection", return_value={"serverless_endpoint_id": EP})

    def test_cycle_lowers_capacity_when_idle(self):
        self.fake.max = 3
        with self._saved():
            result = ec.recovery_cycle(self.logs.append)
        self.assertTrue(result.ok)
        self.assertEqual(self.fake.max, 0)
        self.assertTrue(any("background recovery" in line for line in self.logs))

    def test_cycle_never_lowers_while_a_job_lease_is_valid(self):
        self.fake.max = 3
        token = ec.acquire_lease(EP, "running", lease_dir=self.lease_dir)
        with self._saved(), mock.patch.object(ec, "LEASE_DIR", self.lease_dir):
            result = ec.recovery_cycle(self.logs.append)
        self.assertTrue(result.ok)
        self.assertFalse(result.lowered)
        self.assertEqual(self.fake.max, 3, "capacity must stay up while a job is active")
        ec.release_lease(EP, token, self.logs.append, lease_dir=self.lease_dir)

    def test_cycle_does_nothing_without_a_saved_endpoint(self):
        with mock.patch("src.compute_deploy.load_saved_selection", return_value={"deploy": {}}):
            self.assertIsNone(ec.recovery_cycle(self.logs.append))

    def test_background_loop_runs_and_stops(self):
        self.fake.max = 3
        with self._saved():
            thread = ec.start_background_recovery(self.logs.append, interval_sec=0.02)
            deadline = time.time() + 5
            while self.fake.max != 0 and time.time() < deadline:
                time.sleep(0.02)
            thread.stop_event.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.fake.max, 0, "the background loop should lower idle capacity")

    def test_background_loop_survives_a_failing_cycle(self):
        errors = []
        with mock.patch.object(ec, "recovery_cycle", side_effect=RuntimeError("boom")):
            thread = ec.start_background_recovery(errors.append, interval_sec=0.02)
            deadline = time.time() + 5
            while not errors and time.time() < deadline:
                time.sleep(0.02)
            thread.stop_event.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any("could not run" in line for line in errors))


class SlowCapacityRequestTests(CapacityTestBase):
    max_start = 0

    def test_capacity_calls_use_the_short_request_timeout(self):
        ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertTrue(self.fake.timeouts)
        self.assertTrue(all(t == ec.CAPACITY_REQUEST_TIMEOUT_SEC for t in self.fake.timeouts))

    def test_worst_case_hold_is_shorter_than_the_lock_wait(self):
        # Two calls (PATCH and GET) per capacity change, each bounded by the request timeout.
        self.assertLess(2 * ec.CAPACITY_REQUEST_TIMEOUT_SEC, ec.LOCK_TIMEOUT_SEC)

    def test_slow_capacity_call_fails_clearly_and_releases_the_lock(self):
        import socket

        self.fake.fail_patch = socket.timeout("timed out")
        result = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertFalse(result.ok)
        self.assertIn("capacity update failed", result.message)
        # The lock must be free afterwards: a second caller gets through without a lock timeout.
        second = ec.lower_if_idle(EP, self.logs.append, lease_dir=self.lease_dir)
        self.assertIn("capacity", second.message)

    def test_release_with_slow_capacity_call_does_not_block_other_jobs(self):
        import socket

        self.fake.fail_patch = socket.timeout("timed out")
        token = ec.acquire_lease(EP, "slow", lease_dir=self.lease_dir)
        ec.release_lease(EP, token, self.logs.append, lease_dir=self.lease_dir)
        self.fake.fail_patch = None
        new_token = ec.acquire_lease(EP, "next", lease_dir=self.lease_dir)  # must not raise CapacityLockError
        ec.release_lease(EP, new_token, self.logs.append, lease_dir=self.lease_dir)


if __name__ == "__main__":
    unittest.main()
