from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.http.problems import DependencyUnavailable
from docchat_api.http.ratelimit import (
    BurstLimiter,
    TooManyRequests,
    WriteRateLimited,
    WriteRateLimiter,
    estimate,
    retry_after,
)
from docchat_core.auth import Principal
from docchat_testing.clock import FakeClock

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
OTHER = UUID("0b0b0000-0000-4000-8000-000000000002")


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


def _principal(oid: UUID = USER) -> Principal:
    return Principal(oid=oid, tid=uuid4(), azp=uuid4(), client="web", roles=frozenset({"User"}))


# --- T-016: the burst limit -------------------------------------------------------------------


def test_t016_the_41st_request_of_one_instant_is_refused(captured: Captured) -> None:
    limiter = BurstLimiter(FakeClock())
    for _ in range(40):
        limiter.check(USER)

    with pytest.raises(TooManyRequests) as caught:
        limiter.check(USER)

    assert caught.value.retry_after_seconds == 1
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "api"),): 1}


def test_t016_100_requests_in_one_second_get_at_least_50_refusals() -> None:
    clock = FakeClock()
    limiter = BurstLimiter(clock)
    refused = 0
    for _ in range(100):
        try:
            limiter.check(USER)
        except TooManyRequests:
            refused += 1
        clock.advance(0.01)

    assert refused >= 50


def test_t016_another_user_is_unaffected() -> None:
    limiter = BurstLimiter(FakeClock())
    for _ in range(40):
        limiter.check(USER)

    limiter.check(OTHER)
    with pytest.raises(TooManyRequests):
        limiter.check(USER)


def test_t016_after_one_idle_second_a_refused_user_gets_10_more() -> None:
    clock = FakeClock()
    limiter = BurstLimiter(clock)
    for _ in range(40):
        limiter.check(USER)
    with pytest.raises(TooManyRequests):
        limiter.check(USER)

    clock.advance(1)
    for _ in range(10):
        limiter.check(USER)
    with pytest.raises(TooManyRequests):
        limiter.check(USER)


def test_the_bucket_never_holds_more_than_40() -> None:
    clock = FakeClock()
    limiter = BurstLimiter(clock)
    limiter.check(USER)
    clock.advance(3600)
    for _ in range(40):
        limiter.check(USER)

    with pytest.raises(TooManyRequests):
        limiter.check(USER)


def test_t016_a_50000_user_map_evicts_the_least_recently_used() -> None:
    limiter = BurstLimiter(FakeClock(), max_users=50_000)
    first = UUID(int=1)
    limiter.check(first)
    for number in range(2, 50_001):
        limiter.check(UUID(int=number))
    limiter.check(first)

    limiter.check(UUID(int=50_001))

    assert len(limiter) == 50_000
    assert first in limiter
    assert UUID(int=2) not in limiter


# --- The write window: estimate and wait ---------------------------------------------------


def test_the_estimate_weights_last_minute_by_the_window_it_still_covers() -> None:
    assert estimate(current=10, previous=60, fraction=0.5) == 41


def test_retry_after_is_the_first_second_the_estimate_allows() -> None:
    # At second 30 of a minute with 120 in the previous minute and none in this one, the
    # estimate is 1 + 120 * 0.5 = 61; with a limit of 60 one more second brings it under.
    now = 60 * 1000 + 30

    assert retry_after(now, current=0, previous=120, limit=60) == 1
    # 60 this minute: the next minute starts with them as last minute's full weight, so the
    # first allowed second is the one after it.
    assert retry_after(now, current=60, previous=0, limit=60) == 31
    assert retry_after(60 * 1000 + 0.0, current=0, previous=0, limit=0) == 120


# --- T-017, T-019: the write window over a fake usage container ------------------------------


def _error(cls: type[Exception], **kwargs: Any) -> Exception:
    # The Cosmos DB exception constructors are untyped; the call through a class object isn't.
    return cls(**kwargs)


class FakeWindows:
    """The `usage` container's point operations over a dict, with injectable failures."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.etags = 0
        self.fail_with: BaseException | None = None
        self.conflicts = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _etag(self) -> str:
        self.etags += 1
        return f'"{self.etags}"'

    async def read_item(self, item: str, partition_key: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("read", kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        stored = self.items.get((partition_key, item))
        if stored is None:
            raise _error(CosmosResourceNotFoundError, message="missing")
        return stored

    async def patch_item(
        self, item: str, partition_key: str, patch_operations: list[dict[str, Any]], **kwargs: Any
    ) -> Mapping[str, Any]:
        self.calls.append(("patch", kwargs))
        if self.conflicts:
            self.conflicts -= 1
            raise _error(CosmosAccessConditionFailedError, message="etag")
        stored = self.items[(partition_key, item)]
        assert kwargs["etag"] == stored["_etag"]
        stored["count"] += patch_operations[0]["value"]
        stored["_etag"] = self._etag()
        return stored

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("create", kwargs))
        if self.conflicts:
            self.conflicts -= 1
            raise _error(CosmosResourceExistsError, message="exists")
        key = (body["userId"], body["id"])
        self.items[key] = body | {"_etag": self._etag()}
        return self.items[key]


async def test_t017_the_window_allows_request_120_and_refuses_121(captured: Captured) -> None:
    windows = FakeWindows()
    limiter = WriteRateLimiter(windows, clock=FakeClock(datetime(2026, 10, 30, 12, 0, tzinfo=UTC)))
    for _ in range(120):
        await limiter.check(_principal(), 120)

    with pytest.raises(WriteRateLimited) as caught:
        await limiter.check(_principal(), 120)

    minute = math.floor(datetime(2026, 10, 30, 12, 0, tzinfo=UTC).timestamp() / 60)
    stored = windows.items[(str(USER), f"wrate:{minute}")]
    assert stored["count"] == 120
    assert stored["ttl"] == 180
    assert stored["type"] == "write_rate_window"
    assert 1 <= (caught.value.retry_after_seconds or 0) <= 120
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "write"),): 1}
    outcomes = [
        span.attributes["docchat.ratelimit.outcome"]
        for span in captured.finished_spans("docchat.ratelimit.write_check")
        if span.attributes is not None
    ]
    assert outcomes.count("allowed") == 120
    assert outcomes[-1] == "limited"


async def test_t017_five_conflicts_in_a_row_are_write_rate_limited() -> None:
    windows = FakeWindows()
    windows.conflicts = 5
    limiter = WriteRateLimiter(windows, clock=FakeClock())

    with pytest.raises(WriteRateLimited) as caught:
        await limiter.check(_principal(), 120)

    assert caught.value.retry_after_seconds == 1


async def test_a_conflict_is_retried_with_a_fresh_read() -> None:
    windows = FakeWindows()
    windows.conflicts = 2
    limiter = WriteRateLimiter(windows, clock=FakeClock())

    await limiter.check(_principal(), 120)

    assert [kind for kind, _ in windows.calls].count("create") == 3
    assert [kind for kind, _ in windows.calls].count("read") == 6


async def test_the_calls_carry_their_timeouts_and_the_etag_condition() -> None:
    windows = FakeWindows()
    limiter = WriteRateLimiter(windows, clock=FakeClock())

    await limiter.check(_principal(), 120)
    await limiter.check(_principal(), 120)

    reads = [kwargs for kind, kwargs in windows.calls if kind == "read"]
    (patch,) = [kwargs for kind, kwargs in windows.calls if kind == "patch"]
    (create,) = [kwargs for kind, kwargs in windows.calls if kind == "create"]
    assert {kwargs["timeout"] for kwargs in reads} == {2}
    assert patch["timeout"] == create["timeout"] == 3
    assert patch["match_condition"].name == "IfNotModified"


@pytest.mark.parametrize(
    "failure",
    [
        ServiceRequestError("unreachable"),
        _error(CosmosHttpResponseError, status_code=429, message="throttled"),
        _error(CosmosHttpResponseError, status_code=503, message="unavailable"),
        TimeoutError(),
    ],
    ids=["connection", "429", "503", "timeout"],
)
async def test_t019_an_unreachable_store_refuses_with_dependency_unavailable(
    failure: BaseException, captured: Captured
) -> None:
    windows = FakeWindows()
    windows.fail_with = failure
    limiter = WriteRateLimiter(windows, clock=FakeClock())

    with pytest.raises(DependencyUnavailable) as caught:
        await limiter.check(_principal(), 120)

    assert caught.value.retry_after_seconds == 5
    assert captured.points("docchat.ratelimit.rejections") == {}
    (span,) = captured.finished_spans("docchat.ratelimit.write_check")
    assert span.attributes is not None
    assert span.attributes["docchat.ratelimit.outcome"] == "unavailable"


async def test_the_previous_minute_counts_by_its_remaining_part() -> None:
    windows = FakeWindows()
    start = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(start)
    limiter = WriteRateLimiter(windows, clock=clock)
    for _ in range(10):
        await limiter.check(_principal(), 10)
    clock.advance(60 + 30)

    for _ in range(5):
        await limiter.check(_principal(), 10)
    with pytest.raises(WriteRateLimited):
        await limiter.check(_principal(), 10)
