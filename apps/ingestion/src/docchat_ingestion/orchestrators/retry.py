"""`call_with_retry`, the item sub-orchestrations' retry wrapper (`14-ingestion.md` sections 4.3,
4.4 and 4.7; ADR 0050), and the orchestration context the orchestrators use.

Durable's own retry (`call_activity_with_retry`, `df.RetryOptions`) is used nowhere: it waits at
constant intervals with no cap and no `Retry-After`, and retries only raised exceptions, whose text
would enter history. Instead an activity returns its failure as a typed result, and this generator
waits on a durable timer, so a long wait holds no worker: exponential from
`retry.firstDelaySeconds`, capped at `retry.maxDelaySeconds`, at least the service's `Retry-After`,
and never more than 540 seconds, because each attempt extends the item's lease only to its
deadline plus 10 minutes.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from docchat_core.ingestion.models import ActivityResult, RetrySettings

MAX_WAIT_SECONDS: Final = 540
INTERNAL_ERROR: Final = "internal_error"

type ActivityOutput = dict[str, Any]


class DurableTask(Protocol):
    """A task an orchestrator yields: an activity, a timer, an event or a sub-orchestration."""

    @property
    def is_completed(self) -> bool: ...


class OrchestrationContext(Protocol):
    """The `DurableOrchestrationContext` members the ingestion orchestrators use."""

    @property
    def current_utc_datetime(self) -> datetime: ...

    @property
    def instance_id(self) -> str: ...

    def get_input(self) -> Any: ...

    def call_activity(self, name: str, input_: Any = None) -> Any: ...

    def call_sub_orchestrator(
        self, name: str, input_: Any = None, instance_id: str | None = None
    ) -> Any: ...

    def create_timer(self, fire_at: datetime) -> Any: ...

    def wait_for_external_event(self, name: str) -> Any: ...

    def task_any(self, activities: Iterable[Any]) -> Any: ...

    def continue_as_new(self, input_: Any) -> None: ...

    def set_custom_status(self, status: Any) -> None: ...


def wait_seconds(attempt: int, retry: RetrySettings, retry_after_seconds: int | None) -> int:
    """The wait before attempt `attempt + 1`: `min(max(min(first * 2^(attempt - 1), max),
    Retry-After), 540)`, a missing `Retry-After` counted as 0."""
    if attempt < 1:
        raise ValueError("attempts count from 1")
    backoff: int = min(retry.first_delay_seconds * 2 ** (attempt - 1), retry.max_delay_seconds)
    return min(max(backoff, retry_after_seconds or 0), MAX_WAIT_SECONDS)


def host_failure() -> ActivityOutput:
    """What a task the host reports as failed counts as: a retryable `internal_error`."""
    return ActivityResult.failure(INTERNAL_ERROR).model_dump(mode="json")


def call_with_retry(
    context: OrchestrationContext,
    name: str,
    input_: Mapping[str, Any],
    retry: RetrySettings,
    *,
    max_attempts: int | None = None,
) -> Generator[Any, Any, ActivityOutput]:
    """Call an activity until it succeeds, fails with a code that isn't retried, or uses its
    attempts (`retry.maxAttempts`, or `max_attempts` for the clean-up calls that have their own
    count); returns the last result. Use with `yield from`."""
    attempts = retry.max_attempts if max_attempts is None else max_attempts
    attempt = 1
    while True:
        try:
            output = yield context.call_activity(name, dict(input_))
        except Exception:  # noqa: BLE001 - a host-reported failure; its text is never kept
            output = host_failure()
        result: ActivityOutput = output if isinstance(output, dict) else host_failure()
        if result.get("ok") is True or result.get("retryable") is not True or attempt >= attempts:
            return result
        retry_after = result.get("retryAfterSeconds")
        wait = wait_seconds(attempt, retry, retry_after if isinstance(retry_after, int) else None)
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=wait))
        attempt += 1
