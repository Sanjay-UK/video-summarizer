"""Per-job Google circuit breaker: first 429 opens the circuit for the rest of the job."""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field


@dataclass
class GoogleJobCircuitBreaker:
    """When tripped, Google Translate must not be called again for this job."""

    tripped: bool = False
    reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def trip(self, reason: str) -> None:
        with self._lock:
            self.tripped = True
            if reason:
                self.reason = reason

    @property
    def open(self) -> bool:
        with self._lock:
            return self.tripped


_active_breaker: contextvars.ContextVar[GoogleJobCircuitBreaker | None] = contextvars.ContextVar(
    "google_job_circuit_breaker",
    default=None,
)


def get_google_circuit_breaker() -> GoogleJobCircuitBreaker | None:
    return _active_breaker.get()


def set_google_circuit_breaker(breaker: GoogleJobCircuitBreaker | None):
    return _active_breaker.set(breaker)


def reset_google_circuit_breaker(token) -> None:
    _active_breaker.reset(token)
