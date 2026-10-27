"""Cross-process locking tests. These run on macOS and Windows.

A second Python process holds the endpoint lock; this process must be blocked until it is released. This is the
single-machine guarantee the lease design relies on (two app processes sharing one project folder).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import endpoint_capacity as ec  # noqa: E402
from src import runpod_serverless as rs  # noqa: E402

EP = "4jxzzc08sr5z0w"
HOLDER = (
    "import sys, time\n"
    "from filelock import FileLock\n"
    "lock = FileLock(sys.argv[1], timeout=10)\n"
    "lock.acquire()\n"
    "print('locked', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
    "lock.release()\n"
)


class CrossProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lease_dir = Path(self.tmp.name) / "leases with spaces"
        self.lock_path = self.lease_dir / f"{ec._safe_name(EP)}.lock"
        # RunPod is never called from these tests.
        for target, name, kwargs in [
            (rs, "patch_serverless_endpoint", {"return_value": {}}),
            (rs, "get_serverless_endpoint", {"return_value": ({"workers": {"max": 0}}, None)}),
        ]:
            p = mock.patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def _start_holder(self, seconds):
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.lock_path), str(seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()

        self.addCleanup(cleanup)
        line = proc.stdout.readline().strip()  # do not read stderr here: that would wait for the holder to exit
        if line != "locked":
            self.fail(f"holder did not acquire the lock; output={line!r}")
        return proc

    def test_second_process_is_blocked_while_the_lock_is_held(self):
        holder = self._start_holder(seconds=4)
        with mock.patch.object(ec, "LOCK_TIMEOUT_SEC", 0.5):
            with self.assertRaises(ec.CapacityLockError):
                ec.acquire_lease(EP, "blocked", lease_dir=self.lease_dir)
        holder.wait(timeout=30)

    def test_lock_is_available_again_after_the_other_process_releases(self):
        holder = self._start_holder(seconds=1)
        holder.wait(timeout=30)
        token = ec.acquire_lease(EP, "after release", lease_dir=self.lease_dir)
        self.assertEqual(ec.active_lease_count(EP, lease_dir=self.lease_dir), 1)
        ec.release_lease(EP, token, lambda _m: None, lease_dir=self.lease_dir)


if __name__ == "__main__":
    unittest.main()
