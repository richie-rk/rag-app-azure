"""Day and month token quotas: the P4 check and the background gate `check_token_quota`
(`12-chat-pipeline.md` sections 3.4 and 3.6; `13-conversations-and-memory.md` section 6.4 rule 5).

The counters `day:{YYYY-MM-DD}` and `month:{YYYY-MM}` of the current UTC date are point-read
concurrently, a missing item counting 0. A quota is reached when the counter's `totalTokens` is at
or above `quota.tokens_per_day` or `quota.tokens_per_month`; the wait is the time to the next UTC
midnight or to the first instant of the next UTC month, rounded up, the larger of the two when both
are reached. A turn that starts under quota finishes even when it crosses it, so the overshoot is
bounded by one turn and its after-response calls.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from docchat_api.usage.container import UsageStore
from docchat_core.cosmos.usage import day_counter_id, month_counter_id
from docchat_core.errors import DocChatProblem
from docchat_core.opsconfig import QuotaOps

DECEMBER: Final = 12

type QuotaPeriod = Literal["day", "month"]


class TokenQuotaExceeded(DocChatProblem):
    """`429 token_quota_exceeded`, `retryAfterSeconds` the time to the period's end (1 to
    2,678,400)."""

    code = "token_quota_exceeded"


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaCheck:
    """The background gate's answer. `exceeded` is `"month"` when the month quota is reached,
    otherwise `"day"` when the day quota is; `retry_after_seconds` is `None` when allowed."""

    allowed: bool
    exceeded: QuotaPeriod | None
    retry_after_seconds: int | None


def _whole_seconds_until(now: datetime, end: datetime) -> int:
    return max(1, math.ceil((end - now).total_seconds()))


def seconds_to_next_day(now: datetime) -> int:
    """Seconds until the next UTC midnight, rounded up: 1 to 86,400."""
    utc = now.astimezone(UTC)
    midnight = datetime(utc.year, utc.month, utc.day, tzinfo=UTC) + timedelta(days=1)
    return _whole_seconds_until(utc, midnight)


def seconds_to_next_month(now: datetime) -> int:
    """Seconds until the first instant of the next UTC month, rounded up: 1 to 2,678,400."""
    utc = now.astimezone(UTC)
    year, month = (utc.year + 1, 1) if utc.month == DECEMBER else (utc.year, utc.month + 1)
    return _whole_seconds_until(utc, datetime(year, month, 1, tzinfo=UTC))


def quota_retry_after(now: datetime, *, day: bool, month: bool) -> int:
    """The wait of a refused request: the larger period end among the quotas reached."""
    waits = [seconds_to_next_day(now)] if day else []
    if month:
        waits.append(seconds_to_next_month(now))
    if not waits:
        raise ValueError("no quota is reached")
    return max(waits)


def _total(item: Mapping[str, Any] | None) -> int:
    # Read raw rather than through the counter model: the month total may outgrow the model's
    # integer range long before it can outgrow the largest monthly quota.
    if item is None:
        return 0
    value = item.get("totalTokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def evaluate_quota(
    store: UsageStore, oid: UUID, quota: QuotaOps, now: datetime
) -> QuotaCheck:
    """Read both counters and decide. Raises `UsageStoreUnavailable` when they can't be read."""
    today = now.astimezone(UTC).date()
    day_item, month_item = await store.read_pair(
        day_counter_id(today), month_counter_id(today), str(oid)
    )
    day = _total(day_item) >= quota.tokens_per_day
    month = _total(month_item) >= quota.tokens_per_month
    if not (day or month):
        return QuotaCheck(allowed=True, exceeded=None, retry_after_seconds=None)
    return QuotaCheck(
        allowed=False,
        exceeded="month" if month else "day",
        retry_after_seconds=quota_retry_after(now, day=day, month=month),
    )
