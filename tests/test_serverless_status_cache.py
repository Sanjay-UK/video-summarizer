"""The status cache: 5-second TTL, shared in-flight refresh, no caching of failed reads, and invalidation.

A fake clock and fake fetch are injected, so no provider call is made and expiry is tested without waiting.
"""

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import serverless_status_cache as cache  # noqa: E402


def _ok(endpoint_id):
    return {"config": {"workers": {"min": 0, "max": 3}}, "config_error": None,
            "health": {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
            "health_error": None, "fetched_at": "2026-01-01T00:00:00Z"}


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class CacheTestBase(unittest.TestCase):
    def setUp(self):
        cache._cache.clear()
        cache._inflight.clear()
        self.clock = FakeClock()
        self.calls = []

    def fetch(self, endpoint_id, result=None):
        self.calls.append(endpoint_id)
        return dict(result if result is not None else _ok(endpoint_id))

    def get(self, endpoint_id="EP1", fetch=None):
        return cache.get_live_snapshot(endpoint_id, fetch=fetch or self.fetch, clock=self.clock)


class TtlTests(CacheTestBase):
    def test_second_request_within_the_ttl_reuses_the_snapshot(self):
        first = self.get()
        self.clock.t += 4.9
        second = self.get()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    def test_snapshot_expires_after_five_seconds(self):
        self.get()
        self.clock.t += 5.0
        self.get()
        self.assertEqual(len(self.calls), 2)

    def test_invalidate_forces_the_next_request_to_ask_again(self):
        self.get()
        cache.invalidate("EP1")
        self.get()
        self.assertEqual(len(self.calls), 2)

    def test_invalidate_all_clears_every_endpoint(self):
        self.get("EP1")
        self.get("EP2")
        cache.invalidate_all()
        self.get("EP1")
        self.get("EP2")
        self.assertEqual(sorted(self.calls), ["EP1", "EP1", "EP2", "EP2"])

    def test_endpoints_are_cached_separately(self):
        self.get("EP1")
        self.get("EP2")
        self.assertEqual(self.calls, ["EP1", "EP2"])


class FailureNotCachedTests(CacheTestBase):
    def test_settings_failure_is_not_cached_so_the_next_request_retries(self):
        failing = {"config": None, "config_error": "timeout", "health": None, "health_error": None,
                   "fetched_at": None}
        self.get(fetch=lambda e: self.fetch(e, failing))
        self.get(fetch=lambda e: self.fetch(e, failing))
        self.assertEqual(len(self.calls), 2)

    def test_health_failure_is_not_cached(self):
        failing = dict(_ok("EP1"), health=None, health_error="health check returned HTTP 503")
        result = self.get(fetch=lambda e: self.fetch(e, failing))
        self.get(fetch=lambda e: self.fetch(e, failing))
        self.assertEqual(result["health_error"], "health check returned HTTP 503")
        self.assertEqual(len(self.calls), 2)

    def test_a_success_after_a_failure_is_cached_normally(self):
        failing = dict(_ok("EP1"), health=None, health_error="HTTP 503")
        self.get(fetch=lambda e: self.fetch(e, failing))
        self.get()  # healthy result this time
        self.get()  # served from cache
        self.assertEqual(len(self.calls), 2)

    def test_exception_in_the_fetch_is_reported_as_unavailable_not_healthy(self):
        def boom(endpoint_id):
            raise OSError("network down")

        result = self.get(fetch=boom)
        self.assertIsNone(result["config"])
        self.assertIn("OSError", result["config_error"])
        self.assertIsNone(result["health"])
        self.get(fetch=boom)
        self.assertEqual(len(cache._cache), 0, "an exception must not populate the cache")


class ConcurrentRefreshTests(CacheTestBase):
    def test_concurrent_requests_share_one_provider_refresh(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        def slow_fetch(endpoint_id):
            self.calls.append(endpoint_id)
            entered.set()
            release.wait(5)
            return _ok(endpoint_id)

        def worker():
            results.append(self.get(fetch=slow_fetch))

        owner = threading.Thread(target=worker)
        owner.start()
        self.assertTrue(entered.wait(5), "the first refresh should start")
        waiters = [threading.Thread(target=worker) for _ in range(4)]
        for t in waiters:
            t.start()
        release.set()
        owner.join(10)
        for t in waiters:
            t.join(10)
        self.assertEqual(len(self.calls), 1, "only one provider refresh for concurrent requests")
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["config_error"] is None for r in results))

    def test_concurrent_waiters_see_the_failure_but_it_is_not_cached(self):
        entered = threading.Event()
        release = threading.Event()
        failing = {"config": None, "config_error": "timeout", "health": None, "health_error": None, "fetched_at": None}

        def slow_failing(endpoint_id):
            self.calls.append(endpoint_id)
            entered.set()
            release.wait(5)
            return dict(failing)

        results = []
        owner = threading.Thread(target=lambda: results.append(self.get(fetch=slow_failing)))
        owner.start()
        entered.wait(5)
        waiter = threading.Thread(target=lambda: results.append(self.get(fetch=slow_failing)))
        waiter.start()
        release.set()
        owner.join(10)
        waiter.join(10)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r["config_error"] == "timeout" for r in results))
        self.get(fetch=slow_failing)
        self.assertEqual(len(self.calls), 2, "after the shared failure, the next request asks again")


if __name__ == "__main__":
    unittest.main()
