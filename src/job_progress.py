"""Measured progress reporting for the job that is running in this thread.

The application sets a reporter for the job before running the local pipeline. Pipeline code calls report_progress()
or report_stage(); with no reporter set they do nothing. Reporting never raises, so a progress problem cannot fail a
processing job.
"""

from __future__ import annotations

import contextvars
from typing import Callable, Optional

# reporter(kind, value, step, indeterminate): kind is "progress" (fraction 0..1 of the current measured range)
# or "stage" (a new stage with its starting fraction and whether progress inside it is measurable).
Reporter = Callable[[str, float, Optional[str], bool], None]

_reporter: contextvars.ContextVar[Optional[Reporter]] = contextvars.ContextVar("job_progress_reporter", default=None)


def set_reporter(reporter: Optional[Reporter]) -> contextvars.Token:
    return _reporter.set(reporter)


def reset_reporter(token: contextvars.Token) -> None:
    _reporter.reset(token)


def report_progress(fraction: float) -> None:
    """Measured progress inside the current stage, as a fraction from 0 to 1."""
    reporter = _reporter.get()
    if reporter is None:
        return
    try:
        reporter("progress", float(fraction), None, False)
    except Exception:  # noqa: BLE001 — progress must never fail the job
        pass


def report_stage(step: str, fraction: float, indeterminate: bool = False) -> None:
    """Start a stage. indeterminate=True means no measurable progress inside it."""
    reporter = _reporter.get()
    if reporter is None:
        return
    try:
        reporter("stage", float(fraction), step, indeterminate)
    except Exception:  # noqa: BLE001
        pass
