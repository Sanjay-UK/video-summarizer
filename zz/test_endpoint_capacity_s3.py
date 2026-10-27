"""Tests for the shared (S3) lease store in src/endpoint_capacity.py.

Two machines share one RunPod endpoint. Before this change each machine kept its leases in its own folder, so one
machine could lower max workers to 0 while the other was still running a job. Here both machines use the same
in-memory bucket, which is what production does. The fake enforces S3's conditional writes (If-None-Match on
create, If-Match on delete), so the lock is tested the way S3 behaves. RunPod is faked too. No network, no AWS.
"""

import json
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import endpoint_capacity as ec  # noqa: E402
from src import runpod_serverless as rs  # noqa: E402

EP = "4jxzzc08sr5z0w"
LEASE_KEY = f"{ec.LEASE_PREFIX}{EP}.json"
LOCK_KEY = f"{ec.LEASE_PREFIX}{EP}.lock"


class ClientErr(Exception):
    """Shaped like botocore's ClientError: s3_video_store reads the AWS code from .response."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class ConditionalS3:
    """In-memory bucket with the two conditional writes the lease lock relies on."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._mutex = threading.Lock()
        self.fail_put_for: set[str] = set()  # keys whose writes raise a network error

    def _write(self, key, body):
        self.objects[key] = body
        self.etags[key] = uuid.uuid4().hex

    def put_object(self, Bucket, Key, Body, ContentType="", ServerSideEncryption="", IfNoneMatch=None, IfMatch=None):
        with self._mutex:
            if Key in self.fail_put_for:
                raise ConnectionError("network down")
            if IfNoneMatch == "*" and Key in self.objects:
                raise ClientErr("PreconditionFailed")
            self._write(Key, Body)

    def get_object(self, Bucket, Key):
        with self._mutex:
            if Key not in self.objects:
                raise ClientErr("NoSuchKey")
            import io

            return {"Body": io.BytesIO(self.objects[Key]), "ETag": f'"{self.etags[Key]}"'}

    def delete_object(self, Bucket, Key, IfMatch=None):
        with self._mutex:
            if Key not in self.objects:
                return {}
            if IfMatch is not None and IfMatch != f'"{self.etags[Key]}"':
                raise ClientErr("PreconditionFailed")
            self.objects.pop(Key)
            self.etags.pop(Key)
            return {}

    # helpers for tests
    def put_raw(self, key, body: bytes):
        with self._mutex:
            self._write(key, body)

    def leases(self):
        if LEASE_KEY not in self.objects:
            return []
        return json.loads(self.objects[LEASE_KEY].decode("utf-8"))


class FakeEndpoint:
    """RunPod's max_workers. Records every change so tests can see when capacity was lowered."""

    def __init__(self, max_workers: int):
        self.max = max_workers
        self.patches: list[int] = []
        self.on_patch = None  # optional hook, called with the new max before it is applied

    def patch(self, endpoint_id, body, timeout=None):
        new_max = body["workers"]["max"]
        if self.on_patch:
            self.on_patch(new_max)
        self.patches.append(new_max)
        self.max = new_max
        return {"id": endpoint_id}

    def get(self, endpoint_id, timeout=None):
        return {"id": endpoint_id, "workers": {"min": 0, "max": self.max, "idleTimeout": 5}}, None


class SharedBucketTestBase(unittest.TestCase):
    max_start = 3

    def setUp(self):
        self.s3 = ConditionalS3()
        self.endpoint = FakeEndpoint(self.max_start)
        self.logs: list[str] = []
        patches = [
            mock.patch.object(ec, "_default_s3_store", return_value=SimpleNamespace(client=self.s3, bucket="test-bucket")),
            mock.patch.object(ec, "LOCK_POLL_SEC", 0.001),
            mock.patch.object(rs, "patch_serverless_endpoint", side_effect=self.endpoint.patch),
            mock.patch.object(rs, "get_serverless_endpoint", side_effect=self.endpoint.get),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def log(self, message: str) -> None:
        self.logs.append(message)


class TwoMachineTests(SharedBucketTestBase):
    """The bug from the Windows log: one machine's release lowered capacity under the other machine's job."""

    max_start = 3

    def test_release_on_one_machine_keeps_capacity_while_another_machine_holds_a_lease(self):
        machine_a = ec.acquire_lease(EP, "heavy")  # Windows: job running
        machine_b = ec.acquire_lease(EP, "highlight")  # Mac: job finishes
        remaining = ec.release_lease(EP, machine_b, self.log)
        self.assertEqual(remaining, 1)
        self.assertEqual(self.endpoint.patches, [], "capacity must not be lowered while machine A's job runs")
        self.assertEqual(self.endpoint.max, 3)
        self.assertEqual([l["token"] for l in self.s3.leases()], [machine_a])

        ec.release_lease(EP, machine_a, self.log)
        self.assertEqual(self.endpoint.max, 0, "the last lease lowers capacity")
        self.assertEqual(self.s3.leases(), [])

    def test_startup_check_on_one_machine_does_not_lower_for_a_job_on_the_other(self):
        # Mac starts the app while the Windows job is still running.
        ec.acquire_lease(EP, "heavy")
        result = ec.startup_recovery(EP, self.log)
        self.assertTrue(result.ok)
        self.assertFalse(result.lowered)
        self.assertEqual(self.endpoint.patches, [])
        self.assertEqual(self.endpoint.max, 3)

    def test_lowering_never_happens_while_any_lease_is_held_under_concurrency(self):
        violations: list[int] = []

        def check_before_lowering(new_max):
            # Capacity is lowered only while the lock is held. At that moment no lease may exist in the bucket.
            if new_max == 0 and self.s3.leases():
                violations.append(len(self.s3.leases()))

        self.endpoint.on_patch = check_before_lowering
        errors: list[BaseException] = []

        def machine(label):
            try:
                for _ in range(10):
                    with ec.job_lease(EP, label, self.log):
                        time.sleep(0.002)  # a job holds its lease while other machines start and finish
            except BaseException as exc:  # noqa: BLE001 — collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=machine, args=(f"m{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(violations, [], "capacity was lowered while leases still existed")
        self.assertEqual(self.s3.leases(), [])
        self.assertEqual(self.endpoint.max, 0)
        self.assertNotIn(LOCK_KEY, self.s3.objects, "the lock is released when work finishes")


class LockTests(SharedBucketTestBase):
    max_start = 3

    def test_stale_lock_from_a_crashed_process_is_broken(self):
        stale = json.dumps({"token": "dead", "acquired_at": time.time() - ec.LOCK_STALE_SEC - 60}).encode()
        self.s3.put_raw(LOCK_KEY, stale)
        token = ec.acquire_lease(EP, "heavy")  # must not raise CapacityLockError
        self.assertTrue(token)
        self.assertNotIn(LOCK_KEY, self.s3.objects)

    def test_fresh_lock_held_by_another_machine_is_not_broken(self):
        fresh = json.dumps({"token": "other", "acquired_at": time.time()}).encode()
        self.s3.put_raw(LOCK_KEY, fresh)
        with mock.patch.object(ec, "LOCK_TIMEOUT_SEC", 0.2):
            with self.assertRaises(ec.CapacityLockError):
                ec.acquire_lease(EP, "heavy")
        self.assertIn(LOCK_KEY, self.s3.objects, "another machine's live lock must be left alone")

    def test_lock_is_released_when_the_job_raises(self):
        with self.assertRaises(ValueError):
            with ec.job_lease(EP, "heavy", self.log):
                raise ValueError("job failed")
        self.assertNotIn(LOCK_KEY, self.s3.objects)
        self.assertEqual(self.s3.leases(), [])
        self.assertEqual(self.endpoint.max, 0)

    def test_failed_lock_release_is_logged_and_does_not_mask_the_job_error(self):
        real_delete = self.s3.delete_object

        def broken_delete(Bucket, Key, IfMatch=None):
            if Key == LOCK_KEY:
                raise ConnectionError("network down")
            return real_delete(Bucket, Key, IfMatch=IfMatch)

        with self.assertRaises(ValueError):
            with ec.job_lease(EP, "heavy", self.log):
                self.s3.delete_object = broken_delete  # only the release path fails
                raise ValueError("job failed")
        self.assertTrue(any("could not release the endpoint lock" in line for line in self.logs))

    def test_s3_failure_on_acquire_raises_and_capacity_is_untouched(self):
        self.s3.fail_put_for.add(LOCK_KEY)
        with self.assertRaises(ConnectionError):
            ec.acquire_lease(EP, "heavy")
        self.assertEqual(self.endpoint.patches, [])
        self.assertEqual(self.endpoint.max, 3)


class FailClosedTests(SharedBucketTestBase):
    max_start = 3

    def test_unreadable_lease_object_is_never_treated_as_empty(self):
        self.s3.put_raw(LEASE_KEY, b"not json")
        remaining = ec.release_lease(EP, "any-token", self.log)
        self.assertEqual(remaining, -1)
        self.assertEqual(self.endpoint.patches, [], "capacity must not be lowered when the leases cannot be read")
        self.assertEqual(self.endpoint.max, 3)
        self.assertTrue(any(line.startswith("WARNING: lease release failed") for line in self.logs))

        result = ec.startup_recovery(EP, self.log)
        self.assertFalse(result.ok)
        self.assertEqual(self.endpoint.patches, [])

    def test_expired_leases_on_the_shared_store_are_dropped(self):
        old = [{"token": "old", "label": "heavy", "acquired_at": time.time() - ec.LEASE_TTL_SEC - 60}]
        self.s3.put_raw(LEASE_KEY, json.dumps(old).encode())
        self.assertEqual(ec.active_lease_count(EP), 0)


if __name__ == "__main__":
    unittest.main()
