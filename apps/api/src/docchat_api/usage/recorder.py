"""The recorder: every model call's usage reaches the user's day and month counters
(`12-chat-pipeline.md` sections 3.1, 15.6 and 15.7).

For each `ModelCallUsage` record two concurrent writes go to `day:{YYYY-MM-DD}` and
`month:{YYYY-MM}` of the call's end (UTC): one patch of at most 10 operations (`incr` of the six
token fields and of `calls`, of `estimatedCostMicros` when the cost is known or of `unpricedCalls`
when it isn't, of `estimatedCalls` when the record is estimated, and `set` of `/updatedAt`). A `404`
creates the counter with the record's values as its initial values; a `409` (created meanwhile)
patches once more. Each write takes up to 3 attempts, 0.2 s then 0.4 s apart, on `408`, `429`,
`449`, `503` and a timeout, with 3 s per operation. A record whose writes fail logs
`usage_record_failed` and counts a failure; it never fails the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from http import HTTPStatus
from typing import Any, Final
from uuid import UUID

from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

from docchat_api.usage.container import WRITE_TIMEOUT_SECONDS, UsageContainer, is_timeout, status_of
from docchat_api.usage.tasks import PendingTasks
from docchat_api.usage.telemetry import record_model_metrics, report_record_failed
from docchat_core.cosmos.conversations import ModelCallUsage
from docchat_core.cosmos.usage import (
    UsageDayCounter,
    UsageMonthCounter,
    day_counter_id,
    month_counter_id,
)
from docchat_core.models import format_utc
from docchat_core.models.clock import Clock

type Sleep = Callable[[float], Awaitable[None]]

COUNTER_WRITE_ATTEMPTS: Final = 3
COUNTER_WRITE_WAITS: Final = (0.2, 0.4)
RETRIED_STATUSES: Final = frozenset(
    {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.TOO_MANY_REQUESTS,
        449,  # Cosmos DB's "retry with": a concurrent write to the same item
        HTTPStatus.SERVICE_UNAVAILABLE,
    }
)

_TOKEN_FIELDS: Final = (
    ("promptTokens", "prompt_tokens"),
    ("completionTokens", "completion_tokens"),
    ("reasoningTokens", "reasoning_tokens"),
    ("cachedTokens", "cached_tokens"),
    ("cacheWriteTokens", "cache_write_tokens"),
    ("totalTokens", "total_tokens"),
)


def counter_increments(record: ModelCallUsage) -> dict[str, int]:
    """The counter fields one record adds to, by stored name."""
    increments = {stored: getattr(record, name) for stored, name in _TOKEN_FIELDS}
    increments["calls"] = 1
    if record.estimated_cost_micros is not None:
        increments["estimatedCostMicros"] = record.estimated_cost_micros
    else:
        increments["unpricedCalls"] = 1
    if record.estimated:
        increments["estimatedCalls"] = 1
    return increments


def counter_patch(record: ModelCallUsage, now: datetime) -> list[dict[str, Any]]:
    """The one patch a record makes on a counter: at most 10 operations, the Cosmos DB limit."""
    operations: list[dict[str, Any]] = [
        {"op": "incr", "path": f"/{field}", "value": value}
        for field, value in counter_increments(record).items()
    ]
    operations.append({"op": "set", "path": "/updatedAt", "value": format_utc(now)})
    return operations


def counter_document(
    model: type[UsageDayCounter] | type[UsageMonthCounter],
    oid: UUID,
    record: ModelCallUsage,
    now: datetime,
) -> dict[str, Any]:
    """A counter created by its first record, with the record's values as its initial values."""
    day = now.date()
    period = day.isoformat() if model is UsageDayCounter else f"{day.year:04d}-{day.month:02d}"
    item_id = day_counter_id(day) if model is UsageDayCounter else month_counter_id(day)
    values: dict[str, Any] = {
        "id": item_id,
        "userId": oid,
        "period": period,
        "createdAt": now,
        "updatedAt": now,
        **counter_increments(record),
    }
    return model.model_validate(values).to_item()


def _retryable(exc: BaseException) -> bool:
    return is_timeout(exc) or status_of(exc) in RETRIED_STATUSES


class Recorder:
    """Starts each record's counter writes and emits its metrics."""

    def __init__(
        self, container: UsageContainer, *, clock: Clock, tasks: PendingTasks, sleep: Sleep
    ) -> None:
        self._container = container
        self._clock = clock
        self._tasks = tasks
        self._sleep = sleep

    def record(
        self,
        oid: UUID,
        record: ModelCallUsage,
        *,
        session_id: UUID | None = None,
        turn_id: UUID | None = None,
    ) -> Awaitable[None]:
        """Emit the record's metrics and start its two counter writes as one shielded task; the
        returned awaitable completes when both writes have ended. Cancelling the awaiter never
        cancels the writes."""
        record_model_metrics(record)
        task = self._tasks.spawn(self._write_both(oid, record, session_id, turn_id))
        return asyncio.shield(task)

    async def _write_both(
        self, oid: UUID, record: ModelCallUsage, session_id: UUID | None, turn_id: UUID | None
    ) -> None:
        now = self._clock.now()
        day: date = now.date()
        written = await asyncio.gather(
            self._write(oid, day_counter_id(day), UsageDayCounter, record, now),
            self._write(oid, month_counter_id(day), UsageMonthCounter, record, now),
        )
        if not all(written):
            report_record_failed(record, session_id=session_id, turn_id=turn_id)

    async def _write(
        self,
        oid: UUID,
        item_id: str,
        model: type[UsageDayCounter] | type[UsageMonthCounter],
        record: ModelCallUsage,
        now: datetime,
    ) -> bool:
        """One counter's write; `False` when its attempts are exhausted or it can't be retried."""
        for attempt in range(COUNTER_WRITE_ATTEMPTS):
            if attempt:
                await self._sleep(COUNTER_WRITE_WAITS[attempt - 1])
            try:
                await self._apply(oid, item_id, model, record, now)
            except (AzureError, TimeoutError) as exc:
                if _retryable(exc):
                    continue
                return False
            return True
        return False

    async def _apply(
        self,
        oid: UUID,
        item_id: str,
        model: type[UsageDayCounter] | type[UsageMonthCounter],
        record: ModelCallUsage,
        now: datetime,
    ) -> None:
        partition = str(oid)
        operations = counter_patch(record, now)
        try:
            await self._patch(item_id, partition, operations)
        except CosmosResourceNotFoundError:
            try:
                await self._container.create_item(
                    counter_document(model, oid, record, now), timeout=WRITE_TIMEOUT_SECONDS
                )
            except CosmosResourceExistsError:
                # Created by a concurrent record between the patch and the create.
                await self._patch(item_id, partition, operations)

    async def _patch(self, item_id: str, partition: str, operations: list[dict[str, Any]]) -> None:
        await self._container.patch_item(
            item_id, partition, operations, timeout=WRITE_TIMEOUT_SECONDS
        )
