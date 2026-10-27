"""Worker-capacity lifecycle for the RunPod serverless endpoint.

Rule: the endpoint is kept at max_workers=0 whenever this app has no valid (unexpired) job lease on it. A job
holds a lease from submission until its poll ends, whether it succeeds, fails, or times out.

Coordination:
- Leases are one JSON object per endpoint in the project S3 bucket: endpoint-leases/<endpoint>.json.
  Every machine that uses the endpoint reads and writes the same object, so each one sees every other machine's leases.
- A lock object, endpoint-leases/<endpoint>.lock, is created with If-None-Match, so only one machine holds it at a
  time. Every lease read or write, and every capacity change, runs under that lock. A lock older than
  LOCK_STALE_SEC is treated as left by a crashed process and is broken.
- A lease expires LEASE_TTL_SEC after it was acquired. Expiry is age-based only. No process-liveness check is
  made, because signal-based liveness checks can terminate a process on Windows.
- Every capacity change is read back from RunPod. A change that cannot be verified is reported as a failure.
- A lease object that cannot be read is never treated as empty. Capacity is then left as it is.
- LEASE_DIR, when set, uses a local folder with a FileLock instead of S3. Tests use it. Production leaves it None.

Limitations (see SETUP.md):
- Jobs started outside this app (RunPod console, other tools) hold no lease and are not tracked.
- A job longer than LEASE_TTL_SEC would lose its lease. The app caps polling at 2 h, so 4 h leaves headroom.
- Lock expiry compares this machine's clock with the time written by the other machine. Clocks must agree to
  within a few seconds (NTP). LOCK_STALE_SEC is far larger than that.
- Every machine must run this code. An older copy still lowers capacity from its local files and can still cut
  off a job running on the other machine.
"""

from __future__ import annotations
from src.log_safety import safe_exc
from src.s3_video_store import error_code

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from filelock import FileLock, Timeout

# None: leases live in the project S3 bucket, shared by every machine. A folder path: local files (tests only).
LEASE_DIR: Optional[Path] = None
LEASE_PREFIX = "endpoint-leases/"
LEASE_TTL_SEC = 4 * 3600  # longer than the 2 h poll cap, with headroom
# Capacity calls run while the lock is held. Each change makes up to two calls (PATCH, then GET read-back),
# so the worst-case hold is 2 x this value. That must stay below LOCK_TIMEOUT_SEC; see the timeout tests.
CAPACITY_REQUEST_TIMEOUT_SEC = 20.0
RECOVERY_INTERVAL_SEC = 300.0  # background stale-lease recovery, while the app is running
LOCK_TIMEOUT_SEC = 60.0  # how long to wait for another machine's lock before giving up
LOCK_STALE_SEC = 120.0  # a lock older than this was left by a crash and may be broken
LOCK_POLL_SEC = 1.0
# S3 answers a conditional create on an existing key with 412, or 409 when two writers race.
_LOCK_HELD_CODES = ("PreconditionFailed", "ConditionalRequestConflict")

LogFn = Callable[[str], None]
_log = logging.getLogger(__name__)


class CapacityLockError(RuntimeError):
    """The endpoint capacity lock could not be taken within LOCK_TIMEOUT_SEC."""


@dataclass
class CapacityResult:
    ok: bool
    lowered: bool
    message: str


def _safe_name(endpoint_id: str) -> str:
    return "".join(ch for ch in endpoint_id if ch.isalnum() or ch in "-_") or "endpoint"


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


_S3_STORE: Any = None


def _default_s3_store() -> Any:
    """The project's S3 store (VideoStore). Built once per process; the boto3 client is created lazily."""
    global _S3_STORE
    if _S3_STORE is None:
        from src.s3_video_store import highlight_store

        _S3_STORE = highlight_store()
    return _S3_STORE


class _FileStore:
    """Leases in a local JSON file, guarded by a FileLock. Used only when LEASE_DIR or lease_dir is set."""

    def __init__(self, endpoint_id: str, base: Path):
        name = _safe_name(endpoint_id)
        self.path = base / f"{name}.json"
        self.lock_path = base / f"{name}.lock"

    @contextmanager
    def locked(self, log: Optional[LogFn] = None) -> Iterator[None]:
        with _locked(self.lock_path):
            yield

    def read(self) -> list[dict]:
        return _read(self.path)

    def write(self, leases: list[dict]) -> None:
        _write(self.path, leases)


class _S3Store:
    """Leases in the project S3 bucket. Every machine sees the same objects, so the lock is shared too.

    The lock is a small object created with If-None-Match. A holder that is still running after LOCK_STALE_SEC
    loses it to the next writer. Holding the lock covers the whole read-modify-write, and the lowering check.
    """

    def __init__(self, endpoint_id: str):
        name = _safe_name(endpoint_id)
        self.key = f"{LEASE_PREFIX}{name}.json"
        self.lock_key = f"{LEASE_PREFIX}{name}.lock"

    @contextmanager
    def locked(self, log: Optional[LogFn] = None) -> Iterator[None]:
        token = self._take_lock()
        try:
            yield
        finally:
            self._drop_lock(token, log or (lambda m: _log.warning("%s", m)))

    def _take_lock(self) -> str:
        store = _default_s3_store()
        token = uuid.uuid4().hex
        body = json.dumps({"token": token, "acquired_at": time.time()}).encode("utf-8")
        deadline = time.monotonic() + LOCK_TIMEOUT_SEC
        while True:
            try:
                store.client.put_object(
                    Bucket=store.bucket,
                    Key=self.lock_key,
                    Body=body,
                    IfNoneMatch="*",
                    ContentType="application/json",
                    ServerSideEncryption="AES256",
                )
                return token
            except Exception as exc:  # noqa: BLE001 — only "lock already held" is retried; anything else is raised
                if error_code(exc) not in _LOCK_HELD_CODES:
                    raise
            self._break_if_stale()
            if time.monotonic() >= deadline:
                raise CapacityLockError(
                    f"another machine holds the endpoint lock ({self.lock_key}); waited up to {LOCK_TIMEOUT_SEC:g}s"
                )
            time.sleep(LOCK_POLL_SEC)

    def _break_if_stale(self) -> None:
        store = _default_s3_store()
        try:
            obj = store.client.get_object(Bucket=store.bucket, Key=self.lock_key)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) == "NoSuchKey":
                return  # released since the failed create; the next attempt will take it
            raise
        try:
            acquired = float(json.loads(obj["Body"].read().decode("utf-8")).get("acquired_at", 0))
        except (ValueError, TypeError, AttributeError):
            acquired = 0.0  # unreadable lock: treat as stale
        if time.time() - acquired < LOCK_STALE_SEC:
            return
        try:
            # Conditional delete: if the lock changed since it was read, it is not ours to break.
            store.client.delete_object(Bucket=store.bucket, Key=self.lock_key, IfMatch=obj.get("ETag"))
        except Exception:  # noqa: BLE001 — changed or removed meanwhile; the next attempt re-reads it
            return

    def _drop_lock(self, token: str, log: LogFn) -> None:
        store = _default_s3_store()
        try:
            obj = store.client.get_object(Bucket=store.bucket, Key=self.lock_key)
            held = json.loads(obj["Body"].read().decode("utf-8")).get("token")
            if held != token:
                log("WARNING: the endpoint lock was taken over while this job held it. Check the endpoint.")
                return
            store.client.delete_object(Bucket=store.bucket, Key=self.lock_key, IfMatch=obj.get("ETag"))
        except Exception as exc:  # noqa: BLE001 — never mask the job's own exception
            log(
                f"WARNING: could not release the endpoint lock ({safe_exc(exc)}); "
                f"it is broken automatically after {LOCK_STALE_SEC:g}s."
            )

    def read(self) -> list[dict]:
        store = _default_s3_store()
        try:
            obj = store.client.get_object(Bucket=store.bucket, Key=self.key)
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) == "NoSuchKey":
                return []
            raise
        # A corrupt object raises here on purpose. Treating it as empty could lower capacity under a running job.
        return list(json.loads(obj["Body"].read().decode("utf-8")))

    def write(self, leases: list[dict]) -> None:
        store = _default_s3_store()
        store.client.put_object(
            Bucket=store.bucket,
            Key=self.key,
            Body=json.dumps(leases, indent=2).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )


def _store(endpoint_id: str, lease_dir: Optional[Path]) -> Any:
    base = Path(lease_dir) if lease_dir else LEASE_DIR
    if base:
        return _FileStore(endpoint_id, Path(base))
    return _S3Store(endpoint_id)


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
        return CapacityResult(False, False, f"capacity update failed ({safe_exc(exc)})")
    try:
        ep, err = rs.get_serverless_endpoint(endpoint_id, timeout=CAPACITY_REQUEST_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001
        return CapacityResult(False, False, f"could not read endpoint back ({safe_exc(exc)})")
    if err or not ep:
        return CapacityResult(False, False, f"could not read endpoint back ({err or 'no data'})")
    max_workers = (ep.get("workers") or {}).get("max")
    if max_workers != 0:
        return CapacityResult(False, False, f"endpoint max_workers is {max_workers} after the update, expected 0")
    return CapacityResult(True, True, "max workers 0 (verified)")


def active_lease_count(endpoint_id: str, *, lease_dir: Optional[Path] = None) -> int:
    store = _store(endpoint_id, lease_dir)
    with store.locked():
        return len(_valid(store.read(), time.time()))


def acquire_lease(endpoint_id: str, label: str, *, lease_dir: Optional[Path] = None) -> str:
    store = _store(endpoint_id, lease_dir)
    token = uuid.uuid4().hex
    with store.locked():
        leases = _valid(store.read(), time.time())
        leases.append({"token": token, "label": label, "acquired_at": time.time()})
        store.write(leases)
    return token


def release_lease(
    endpoint_id: str, token: str, log: LogFn, *, lease_dir: Optional[Path] = None
) -> int:
    """Remove this job's lease, and lower capacity if none remain. Best effort: never raises.

    A failure here is logged and the lease expires on its own. It must never replace the job's own error.
    Returns the number of valid leases left, or -1 if the release itself failed.
    """
    try:
        store = _store(endpoint_id, lease_dir)
        with store.locked(log):
            leases = [l for l in _valid(store.read(), time.time()) if l.get("token") != token]
            store.write(leases)
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
                f"WARNING: lease release failed ({safe_exc(exc)}); "
                f"the lease expires after {LEASE_TTL_SEC // 3600} h."
            )
        except Exception:  # noqa: BLE001 — logging itself must not raise either
            pass
        return -1


def lower_if_idle(endpoint_id: str, log: LogFn, *, lease_dir: Optional[Path] = None) -> CapacityResult:
    """Lower to 0 when no valid lease exists, and verify. Used after creating or reusing an endpoint."""
    try:
        store = _store(endpoint_id, lease_dir)
        with store.locked(log):
            if _valid(store.read(), time.time()):
                return CapacityResult(True, False, "jobs active; capacity left as is")
            result = _set_capacity_zero(endpoint_id)
            log(result.message if result.ok else f"WARNING: {result.message}")
            return result
    except Exception as exc:  # noqa: BLE001
        return CapacityResult(False, False, f"capacity check failed ({safe_exc(exc)})")


def startup_recovery(endpoint_id: str, log: LogFn, *, lease_dir: Optional[Path] = None) -> CapacityResult:
    """At app start: drop expired leases under the lock. With no valid lease left, lower to 0 and verify.

    Failures are returned, not raised, so startup can report them without hiding other startup errors.
    """
    try:
        store = _store(endpoint_id, lease_dir)
        with store.locked(log):
            leases = _valid(store.read(), time.time())
            store.write(leases)
            if leases:
                return CapacityResult(True, False, f"{len(leases)} valid job lease(s); capacity left as is")
            result = _set_capacity_zero(endpoint_id)
            log(result.message if result.ok else f"WARNING: startup recovery failed: {result.message}")
            return result
    except Exception as exc:  # noqa: BLE001
        msg = f"startup recovery failed ({safe_exc(exc)})"
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
                log(f"[capacity] background recovery could not run ({safe_exc(exc)})")

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
        log(f"[capacity] startup check could not run ({safe_exc(exc)})")
        return None
