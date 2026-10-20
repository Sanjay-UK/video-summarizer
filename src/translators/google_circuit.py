"""Per-job Google circuit breaker: first 429 opens the circuit for the rest of the job."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field


@dataclass
class GoogleJobCircuitBreaker:
    """When tripped, Google Translate must not be called again for this job."""

    tripped: bool = False
    reason: str = ""

    def trip(self, reason: str) -> None:
        self.tripped = True
        if reason:
            self.reason = reason

    @property
    def open(self) -> bool:
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
