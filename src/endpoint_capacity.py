"""Worker-capacity lifecycle for the RunPod serverless endpoint.

Rule: the endpoint is kept at max_workers=0 whenever this app has no valid (unexpired) job lease on it. A job
holds a lease from submission until its poll ends, whether it succeeds, fails, or times out.

Coordination:
- Leases are JSON records in data/endpoint_leases/<endpoint>.json.
- Every read and write, and every capacity change, runs under one filelock.FileLock on macOS and on Windows.
- A lease expires LEASE_TTL_SEC after it was acquired. Expiry is age-based only. No process-liveness check is
  made, because signal-based liveness checks can terminate a process on Windows.
- Every capacity change is read back from RunPod. A change that cannot be verified is reported as a failure.

Limitations (see SETUP.md):
- Coordination covers one machine that shares this project folder. Two machines using one endpoint cannot see
  each other's leases.
- Jobs started outside this app (RunPod console, other tools) hold no lease and are not tracked.
- A job longer than LEASE_TTL_SEC would lose its lease. The app caps polling at 2 h, so 4 h leaves headroom.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from filelock import FileLock, Timeout

PROJECT_DIR = Path(__file__).resolve().parent.parent
LEASE_DIR = PROJECT_DIR / "data" / "endpoint_leases"
LEASE_TTL_SEC = 4 * 3600  # longer than the 2 h poll cap, with headroom
# Capacity calls run while the lock is held. Each change makes up to two calls (PATCH, then GET read-back),
# so the worst-case hold is 2 x this value. That must stay below LOCK_TIMEOUT_SEC; see the timeout tests.
CAPACITY_REQUEST_TIMEOUT_SEC = 20.0
RECOVERY_INTERVAL_SEC = 300.0  # background stale-lease recovery, while the app is running
LOCK_TIMEOUT_SEC = 60.0

LogFn = Callable[[str], None]


class CapacityLockError(RuntimeError):
    """The endpoint capacity lock could not be taken within LOCK_TIMEOUT_SEC."""


@dataclass
class CapacityResult:
    ok: bool
    lowered: bool
    message: str


def _safe_name(endpoint_id: str) -> str:
    return "".join(ch for ch in endpoint_id if ch.isalnum() or ch in "-_") or "endpoint"


def _paths(endpoint_id: str, lease_dir: Optional[Path]) -> tuple[Path, Path]:
    base = Path(lease_dir) if lease_dir else LEASE_DIR
    name = _safe_name(endpoint_id)
    return base / f"{name}.json", base / f"{name}.lock"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=LOCK_TIMEOUT_SEC)
    try:
        with lock:
            yield
    except Timeout as exc:
        raise CapacityLockError(
            f"another process holds the endpoint capacity lock ({lock_path.name}); "
            f"waited up to {LOCK_TIMEOUT_SEC:g}s"
        ) from exc


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return []  # unreadable file: treated as empty; the next write replaces it


def _write(path: Path, leases: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(leases, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _valid(leases: list[dict], now: float) -> list[dict]:
    """Leases younger than LEASE_TTL_SEC. Expired ones are dropped."""
    out = []
    for lease in leases:
        try:
            acquired = float(lease.get("acquired_at", 0))
        except (TypeError, ValueError):
            continue
        if now - acquired <= LEASE_TTL_SEC:
            out.append(lease)
    return out


def _set_capacity_zero(endpoint_id: str) -> CapacityResult:
    """Set max_workers=0 and read the configuration back. Never raises."""
    from src import runpod_serverless as rs

    try:
        rs.patch_serverless_endpoint(
            endpoint_id,
            {"workers": {"min": 0, "max": 0, "idleTimeout": rs.workers_block()["idleTimeout"]}},
            timeout=CAPACITY_REQUEST_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        return CapacityResult(False, False, f"capacity update failed ({type(exc).__name__}: {exc})")
    try:
        ep, err = rs.get_serverless_endpoint(endpoint_id, timeout=CAPACITY_REQUEST_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001
        return CapacityResult(False, False, f"could not read endpoint back ({type(exc).__name__}: {exc})")
    if err or not ep:
        return CapacityResult(False, False, f"could not read endpoint back ({err or 'no data'})")
    max_workers = (ep.get("workers") or {}).get("max")
    if max_workers != 0:
        return CapacityResult(False, False, f"endpoint max_workers is {max_workers} after the update, expected 0")
    return CapacityResult(True, True, "max workers 0 (verified)")


def active_lease_count(endpoint_id: str, *, lease_dir: Optional[Path] = None) -> int:
    path, lock = _paths(endpoint_id, lease_dir)
    with _locked(lock):
        return len(_valid(_read(path), time.time()))


def acquire_lease(endpoint_id: str, label: str, *, lease_dir: Optional[Path] = None) -> str:
    path, lock = _paths(endpoint_id, lease_dir)
    token = uuid.uuid4().hex
    with _locked(lock):
        leases = _valid(_read(path), time.time())
        leases.append({"token": token, "label": label, "acquired_at": time.time()})
        _write(path, leases)
    return token


def release_lease(
    endpoint_id: str, token: str, log: LogFn, *, lease_dir: Optional[Path] = None
) -> int:
    """Remove this job's lease, and lower capacity if none remain. Best effort: never raises.

    A failure here is logged and the lease expires on its own. It must never replace the job's own error.
    Returns the number of valid leases left, or -1 if the release itself failed.
    """
    try:
        path, lock = _paths(endpoint_id, lease_dir)
        with _locked(lock):
            leases = [l for l in _valid(_read(path), time.time()) if l.get("token") != token]
            _write(path, leases)
            if not leases:
                result = _set_capacity_zero(endpoint_id)
                log(
                    "No active or pending jobs left on this endpoint; " + result.message
                    if result.ok
                    else f"WARNING: could not restore max workers to 0: {result.message}. Check the endpoint."
                )
            return len(leases)
    except Exception as exc:  # noqa: BLE001 — never mask the job's own exception
        try:
            log(
                f"WARNING: lease release failed ({type(exc).__name__}: {exc}); "
                f"the lease expires after {LEASE_TTL_SEC // 3600} h."
            )
        except Exception:  # noqa: BLE001 — logging itself must not raise either
            pass
        return -1


def lower_if_idle(endpoint_id: str, log: LogFn, *, lease_dir: Optional[Path] = None) -> CapacityResult:
    """Lower to 0 when no valid lease exists, and verify. Used after creating or reusing an endpoint."""
    try:
        path, lock = _paths(endpoint_id, lease_dir)
        with _locked(lock):
            if _valid(_read(path), time.time()):
                return CapacityResult(True, False, "jobs active; capacity left as is")
            result = _set_capacity_zero(endpoint_id)
            log(result.message if result.ok else f"WARNING: {result.message}")
            return result
    except Exception as exc:  # noqa: BLE001
        return CapacityResult(False, False, f"capacity check failed ({type(exc).__name__}: {exc})")


def startup_recovery(endpoint_id: str, log: LogFn, *, lease_dir: Optional[Path] = None) -> CapacityResult:
    """At app start: drop expired leases under the lock. With no valid lease left, lower to 0 and verify.

    Failures are returned, not raised, so startup can report them without hiding other startup errors.
    """
    try:
        path, lock = _paths(endpoint_id, lease_dir)
        with _locked(lock):
            leases = _valid(_read(path), time.time())
            _write(path, leases)
            if leases:
                return CapacityResult(True, False, f"{len(leases)} valid job lease(s); capacity left as is")
            result = _set_capacity_zero(endpoint_id)
            log(result.message if result.ok else f"WARNING: startup recovery failed: {result.message}")
            return result
    except Exception as exc:  # noqa: BLE001
        msg = f"startup recovery failed ({type(exc).__name__}: {exc})"
        log(f"WARNING: {msg}")
        return CapacityResult(False, False, msg)


@contextmanager
def job_lease(endpoint_id: str, label: str, log: LogFn, *, lease_dir: Optional[Path] = None) -> Iterator[str]:
    """Hold capacity for one job from submission to completion. Release never replaces the job's own error."""
    token = acquire_lease(endpoint_id, label, lease_dir=lease_dir)
    try:
        yield token
    finally:
        release_lease(endpoint_id, token, log, lease_dir=lease_dir)


def recovery_cycle(log: LogFn = print) -> Optional[CapacityResult]:
    """One background pass for the saved endpoint. Drops expired leases. Lowers capacity only when no valid lease
    exists, which means no job from this app is active or pending. Logs only when it changes something or fails."""
    from src.compute_deploy import load_saved_selection, saved_serverless_endpoint_id

    endpoint_id = saved_serverless_endpoint_id(load_saved_selection())
    if not endpoint_id:
        return None
    result = startup_recovery(endpoint_id, lambda _m: None)
    if result.lowered or not result.ok:
        log(f"[capacity] background recovery for {endpoint_id}: {'OK' if result.ok else 'FAILED'} - {result.message}")
    return result


def start_background_recovery(
    log: LogFn = print, interval_sec: float = RECOVERY_INTERVAL_SEC
) -> threading.Thread:
    """Run recovery_cycle every interval_sec while the app runs.

    Stop it with thread.stop_event.set() and then thread.join(). A cycle that is already running finishes first.

    Limitation: this thread exists only while the app process runs. If the app is stopped or crashes, nothing
    lowers capacity until the app starts again.
    """
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval_sec):
            try:
                recovery_cycle(log)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive; report the failure
                log(f"[capacity] background recovery could not run ({type(exc).__name__}: {exc})")

    thread = threading.Thread(target=loop, name="capacity-recovery", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread


def startup_check(log: LogFn = print) -> Optional[CapacityResult]:
    """Run recovery for the saved serverless endpoint, if one is configured. Never raises."""
    try:
        from src.compute_deploy import load_saved_selection, saved_serverless_endpoint_id

        endpoint_id = saved_serverless_endpoint_id(load_saved_selection())
        if not endpoint_id:
            return None
        result = startup_recovery(endpoint_id, log)
        log(f"[capacity] startup check for {endpoint_id}: {'OK' if result.ok else 'FAILED'} - {result.message}")
        return result
    except Exception as exc:  # noqa: BLE001
        log(f"[capacity] startup check could not run ({type(exc).__name__}: {exc})")
        return None
