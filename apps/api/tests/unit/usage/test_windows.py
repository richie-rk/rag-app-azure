from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import pytest

from docchat_api.usage.container import UsageStore
from docchat_api.usage.windows import (
    CHAT_WINDOW,
    DOWNLOAD_WINDOW,
    RateLimited,
    allows,
    count_request,
    window_retry_after,
)
from docchat_core.cosmos.containers import USAGE
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
MINUTE = int(START.timestamp()) // 60


class Captured(Protocol):
    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


# --- T-216: the sliding-window decision and its wait --------------------------------------------


@pytest.mark.parametrize(
    ("previous", "current", "fraction", "limit", "allowed"),
    [
        (0, 0, 0.0, 3, True),
        (0, 2, 0.9, 3, True),  # exactly at the limit: (2 + 1) + 0 = 3
        (0, 3, 0.9, 3, False),
        (2, 1, 0.5, 3, True),  # (1 + 1) + 2 * 0.5 = 3, at the limit
        (2, 1, 0.4, 3, False),  # (1 + 1) + 2 * 0.6 = 3.2
        (4, 0, 0.5, 3, True),
        (4, 0, 0.25, 3, False),
        (9, 0, 0.0, 10, True),  # 1 + 9 = 10
        (10, 0, 0.0, 10, False),
    ],
)
def test_t216_the_decision_matches_the_cases(
    previous: int, current: int, fraction: float, limit: int, allowed: bool
) -> None:
    assert allows(previous=previous, current=current, fraction=fraction, limit=limit) is allowed


@pytest.mark.parametrize(
    ("second", "previous", "current", "limit", "wait"),
    [
        (24, 2, 1, 3, 6),  # same minute: needs f >= 0.5, at second 30
        (0, 4, 0, 3, 30),  # same minute: needs f >= 0.5
        (10, 0, 4, 4, 65),  # next minute: 4 becomes the previous count; needs f >= 0.25
        (59, 0, 1, 1, 61),  # the request weighs fully all next minute: wait for minute m + 2
        (50, 0, 3, 3, 30),  # next minute: 1 + 3 * (1 - f) <= 3 from 20 s into it
        (0, 0, 120, 1, 120),  # from minute m + 2 on, both counts are 0
    ],
)
def test_t216_retry_after_is_the_first_second_the_window_allows(
    second: int, previous: int, current: int, limit: int, wait: int
) -> None:
    now = MINUTE * 60 + second

    assert window_retry_after(now, previous=previous, current=current, limit=limit) == wait


def test_t216_retry_after_never_leaves_its_range() -> None:
    for previous, current in [(0, 0), (500, 500), (3, 0), (0, 3)]:
        wait = window_retry_after(MINUTE * 60 + 7.5, previous=previous, current=current, limit=3)
        assert 1 <= wait <= 120


# --- Counting over a fake usage container -------------------------------------------------------


async def test_the_first_request_creates_the_minute_and_later_ones_patch_it(
    container: FakeContainer, clock: FakeClock
) -> None:
    store = UsageStore(container)
    for _ in range(3):
        await count_request(store, USER, CHAT_WINDOW, 3, clock)

    stored = container.get(str(USER), f"rate:{MINUTE}")
    assert stored is not None
    assert (stored["count"], stored["ttl"], stored["type"]) == (3, 180, "rate_window")
    assert len(container.calls_of("create_item")) == 1
    patches = container.calls_of("patch_item")
    assert len(patches) == 2
    assert all(call.kwargs["match_condition"].name == "IfNotModified" for call in patches)


async def test_a_refused_request_is_not_counted(
    container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    store = UsageStore(container)
    for _ in range(3):
        await count_request(store, USER, CHAT_WINDOW, 3, clock)
    clock.advance(30)

    with pytest.raises(RateLimited) as caught:
        await count_request(store, USER, CHAT_WINDOW, 3, clock)

    stored = container.get(str(USER), f"rate:{MINUTE}")
    assert stored is not None
    assert stored["count"] == 3
    assert (
        caught.value.retry_after_seconds == 50
    )  # 20 s into the next minute, where the 3 weigh (1 - f)
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "rate"),): 1}


async def test_the_previous_minute_weighs_by_the_part_it_still_covers(
    container: FakeContainer, clock: FakeClock
) -> None:
    store = UsageStore(container)
    for _ in range(4):
        await count_request(store, USER, CHAT_WINDOW, 4, clock)
    clock.advance(60 + 30)

    # 0 + 1 + 4 * 0.5 = 3 and then 4; the third is refused.
    await count_request(store, USER, CHAT_WINDOW, 4, clock)
    await count_request(store, USER, CHAT_WINDOW, 4, clock)
    with pytest.raises(RateLimited):
        await count_request(store, USER, CHAT_WINDOW, 4, clock)


async def test_a_conflict_decides_again_on_fresh_counts(
    container: FakeContainer, clock: FakeClock
) -> None:
    store = UsageStore(container)
    container.fail("create_item", status=409, times=2)

    await count_request(store, USER, CHAT_WINDOW, 3, clock)

    assert len(container.calls_of("create_item")) == 3
    assert len(container.calls_of("read_item")) == 6


async def test_five_conflicts_refuse_with_one_second(
    container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    container.fail("create_item", status=409, times=5)

    with pytest.raises(RateLimited) as caught:
        await count_request(UsageStore(container), USER, CHAT_WINDOW, 3, clock)

    assert caught.value.retry_after_seconds == 1
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "rate"),): 1}


async def test_the_download_window_uses_its_own_documents(
    container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    store = UsageStore(container)
    await count_request(store, USER, DOWNLOAD_WINDOW, 1, clock)

    with pytest.raises(RateLimited):
        await count_request(store, USER, DOWNLOAD_WINDOW, 1, clock)

    stored = container.get(str(USER), f"dlrate:{MINUTE}")
    assert stored is not None
    assert stored["type"] == "download_rate_window"
    assert container.get(str(USER), f"rate:{MINUTE}") is None
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "download_rate"),): 1}
