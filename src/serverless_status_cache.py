"""Short-lived cache for the read-only endpoint status (settings + health).

- A fully successful snapshot is reused for LIVE_STATUS_TTL_SEC seconds.
- A failed read is never cached, so the next request asks RunPod again.
- Concurrent requests for the same endpoint share one in-flight refresh.
- Only read-only provider calls are made here. Mutations never go through this module.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

LIVE_STATUS_TTL_SEC = 5.0
FLIGHT_WAIT_SEC = 30.0

Fetch = Callable[[str], dict[str, Any]]


class _Flight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[dict[str, Any]] = None


_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_inflight: dict[str, _Flight] = {}


def _failed(message: str) -> dict[str, Any]:
    return {"config": None, "config_error": message, "health": None, "health_error": message,
            "fetched_at": None, "cached": False}


def get_live_snapshot(
    endpoint_id: str,
    *,
    fetch: Fetch,
    ttl_sec: float = LIVE_STATUS_TTL_SEC,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return {config, config_error, health, health_error, fetched_at, cached} for one endpoint."""
    now = clock()
    with _lock:
        hit = _cache.get(endpoint_id)
        if hit is not None and now - hit[0] < ttl_sec:
            return dict(hit[1], cached=True)
        flight = _inflight.get(endpoint_id)
        owner = flight is None
        if owner:
            flight = _Flight()
            _inflight[endpoint_id] = flight

    if not owner:
        if not flight.event.wait(FLIGHT_WAIT_SEC) or flight.result is None:
            return _failed("status refresh is still running; try again")
        return dict(flight.result, cached=False)

    try:
        result = fetch(endpoint_id)
    except Exception as exc:  # noqa: BLE001 — reported as unavailable, never as healthy
        result = _failed(f"status refresh failed ({type(exc).__name__})")
    flight.result = result
    try:
        if result.get("config_error") is None and result.get("health_error") is None:
            with _lock:
                _cache[endpoint_id] = (clock(), result)
    finally:
        flight.event.set()
        with _lock:
            _inflight.pop(endpoint_id, None)
    return dict(result, cached=False)


def invalidate(endpoint_id: str) -> None:
    """Drop the cached snapshot, e.g. after a deploy, stop, or delete changed the endpoint."""
    with _lock:
        _cache.pop(endpoint_id, None)


def invalidate_all() -> None:
    """Drop every cached snapshot. Called after any route that changes RunPod resources."""
    with _lock:
        _cache.clear()
