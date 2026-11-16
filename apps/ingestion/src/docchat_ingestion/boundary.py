"""The failure boundary of the ingestion app (`14-ingestion.md` sections 4.5 and 4.6; ADR 0144).

`guard(<function name>)` wraps the body of every activity and of the three trigger functions. An
unexpected exception is recorded exactly once, by its qualified type name and the code
`internal_error`, never by its text or arguments, so no library text that can quote a document
reaches the Functions host's record of a failed function or Durable's stored failure details.
`asyncio.CancelledError` is a `BaseException` and passes through, as structured concurrency needs.

An activity then returns `internal_error` (retryable), the timers return and run at their next
tick, and the queue starter alone re-raises, only for its Durable client calls, as
`DispatcherUnavailableError` with no cause attached. `run_activity` adds the activity's deadline
inside the guard: a passed deadline is the retryable `activity_timeout`, not an unexpected failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Final, Literal, Self

from opentelemetry import trace

from docchat_core.ingestion.models import ActivityResult
from docchat_core.telemetry import LOGGER_NAME, log_event, qualified_type_name, record_failure

INTERNAL_ERROR: Final = "internal_error"
ACTIVITY_TIMEOUT: Final = "activity_timeout"

QUEUE_STARTER: Final = "ingestion_queue_starter"
TRIGGERS: Final = frozenset({QUEUE_STARTER, "ingestion_keepalive", "ingestion_maintenance"})
"""The three trigger functions; every other guarded function is an activity."""

type BoundaryEvent = Literal["activity_failed", "job_failed"]

logger: Final = logging.getLogger(LOGGER_NAME)


class DispatcherUnavailableError(Exception):
    """The queue starter's Durable client call failed (task-hub storage unreachable). Raised
    `from None` with a constant message, so the host's record holds no library text and the
    runtime retries the message."""

    def __init__(self) -> None:
        super().__init__("durable client call failed")


def record_unexpected(event: BoundaryEvent, failure: BaseException) -> None:
    """The one record of an unexpected failure: the log record with the type name and the code,
    and the failure on the current span with the code's catalogue message and a frames-only
    stack trace."""
    log_event(
        logger,
        event,
        logging.ERROR,
        **{"error.type": qualified_type_name(type(failure)), "docchat.error_code": INTERNAL_ERROR},
    )
    record_failure(trace.get_current_span(), failure, INTERNAL_ERROR)


class Guard:
    """The async context manager `guard` returns. `failed` tells, after the block, whether it
    recorded and suppressed an exception."""

    def __init__(self, function_name: str) -> None:
        if not function_name:
            raise ValueError("a guard names its function")
        self.function_name = function_name
        self.event: BoundaryEvent = "job_failed" if function_name in TRIGGERS else "activity_failed"
        self.failed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc is None or not isinstance(exc, Exception):
            return False
        if isinstance(exc, DispatcherUnavailableError):
            # Recorded when `escalate` built it; it must reach the host so the message is retried.
            return False
        self._record(exc)
        return True

    def _record(self, failure: Exception) -> None:
        record_unexpected(self.event, failure)
        self.failed = True

    def escalate(self, failure: Exception) -> DispatcherUnavailableError:
        """Record a failed Durable client call of the queue starter once and return the
        first-party error the starter raises `from None`."""
        if self.function_name != QUEUE_STARTER:
            raise ValueError("only the queue starter re-raises a failure")
        self._record(failure)
        return DispatcherUnavailableError()


def guard(function_name: str) -> Guard:
    """The failure boundary around one function's body."""
    return Guard(function_name)


def failure_result(code: str) -> dict[str, Any]:
    """An activity's failed `ActivityResult` as the JSON it returns."""
    return ActivityResult.failure(code).model_dump(mode="json")


async def run_activity[R](
    function_name: str,
    deadline_seconds: float,
    body: Callable[[], Awaitable[R]],
    on_failure: Callable[[str], R],
) -> R:
    """Run an activity's body inside the guard and its deadline: the body's result; on a passed
    deadline `on_failure("activity_timeout")`; on an unexpected exception, recorded once,
    `on_failure("internal_error")`.

    The deadline cancels the body's work at the event loop; whatever the body runs in a thread
    finishes there and is idempotent on a re-run (section 4.6).
    """
    async with guard(function_name):
        timeout = asyncio.timeout(deadline_seconds)
        try:
            async with timeout:
                return await body()
        except TimeoutError:
            if not timeout.expired():
                raise
        return on_failure(ACTIVITY_TIMEOUT)
    return on_failure(INTERNAL_ERROR)
