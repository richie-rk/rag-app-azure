from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from docchat_api.usage.container import UsageStore
from docchat_api.usage.quotas import (
    QuotaCheck,
    evaluate_quota,
    quota_retry_after,
    seconds_to_next_day,
    seconds_to_next_month,
)
from docchat_core.cosmos.containers import USAGE
from docchat_core.opsconfig import QuotaOps
from docchat_testing.fakes.cosmos import FakeContainer

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
QUOTA = QuotaOps.model_validate({"quota.tokens_per_day": 10_000, "quota.tokens_per_month": 50_000})


# --- T-220: the quota's wait --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "seconds"),
    [
        (datetime(2026, 10, 30, 23, 59, 59, tzinfo=UTC), 1),
        (datetime(2026, 10, 30, 23, 59, 59, 500_000, tzinfo=UTC), 1),
        (datetime(2026, 10, 30, 0, 0, tzinfo=UTC), 86_400),
        (datetime(2026, 10, 30, 12, 0, 0, 1_000, tzinfo=UTC), 43_200),
        (datetime(2028, 2, 28, 23, 0, tzinfo=UTC), 3_600),
    ],
)
def test_t220_the_wait_to_the_next_utc_midnight(now: datetime, seconds: int) -> None:
    assert seconds_to_next_day(now) == seconds


@pytest.mark.parametrize(
    ("now", "seconds"),
    [
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 31 * 86_400),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), 1),
        (datetime(2028, 2, 1, 0, 0, tzinfo=UTC), 29 * 86_400),  # a leap February
        (datetime(2028, 2, 29, 12, 0, tzinfo=UTC), 43_200),
        (datetime(2027, 2, 28, 0, 0, tzinfo=UTC), 86_400),
        (datetime(2026, 4, 30, 18, 0, tzinfo=UTC), 21_600),
    ],
)
def test_t220_the_wait_to_the_next_utc_month(now: datetime, seconds: int) -> None:
    assert seconds_to_next_month(now) == seconds


def test_t220_both_quotas_wait_for_the_later_end() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    assert quota_retry_after(now, day=True, month=False) == 43_200
    assert quota_retry_after(now, day=False, month=True) == 43_200 + 86_400
    assert quota_retry_after(now, day=True, month=True) == 43_200 + 86_400
    with pytest.raises(ValueError, match="no quota"):
        quota_retry_after(now, day=False, month=False)


def test_t220_a_non_utc_instant_counts_in_utc() -> None:

    now = datetime(2026, 10, 31, 1, 0, tzinfo=timezone(timedelta(hours=2)))  # 23:00 UTC on the 30th

    assert seconds_to_next_day(now) == 3_600


# --- Reading the counters -----------------------------------------------------------------------


async def _check(container: FakeContainer, now: datetime) -> QuotaCheck:
    return await evaluate_quota(UsageStore(container), USER, QUOTA, now)


async def test_missing_counters_count_zero() -> None:
    container = FakeContainer.of(USAGE)

    check = await _check(container, datetime(2026, 10, 30, 12, 0, tzinfo=UTC))

    assert check == QuotaCheck(allowed=True, exceeded=None, retry_after_seconds=None)
    assert {call.item_id for call in container.calls_of("read_item")} == {
        "day:2026-10-30",
        "month:2026-10",
    }
    assert {call.kwargs["timeout"] for call in container.calls_of("read_item")} == {2}


@pytest.mark.parametrize(
    ("day_total", "month_total", "exceeded", "wait"),
    [
        (9_999, 49_999, None, None),
        (10_000, 0, "day", 43_200),
        (0, 50_000, "month", 43_200 + 86_400),
        (10_000, 50_000, "month", 43_200 + 86_400),
    ],
)
async def test_the_quota_is_reached_at_its_value(
    day_total: int, month_total: int, exceeded: str | None, wait: int | None
) -> None:
    container = FakeContainer.of(USAGE)
    container.put({"id": "day:2026-10-30", "userId": str(USER), "totalTokens": day_total})
    container.put({"id": "month:2026-10", "userId": str(USER), "totalTokens": month_total})

    check = await _check(container, datetime(2026, 10, 30, 12, 0, tzinfo=UTC))

    assert check.allowed is (exceeded is None)
    assert check.exceeded == exceeded
    assert check.retry_after_seconds == wait


async def test_a_month_total_beyond_the_counter_models_range_still_counts() -> None:
    container = FakeContainer.of(USAGE)
    container.put({"id": "month:2026-10", "userId": str(USER), "totalTokens": 3_000_000_000})
    quota = QuotaOps.model_validate(
        {"quota.tokens_per_day": 10_000, "quota.tokens_per_month": 2_500_000_000}
    )

    check = await evaluate_quota(
        UsageStore(container), USER, quota, datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    )

    assert check.exceeded == "month"
