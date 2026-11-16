from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceRequestError

from docchat_api.http.problems import DependencyUnavailable
from docchat_api.usage import (
    RateLimited,
    TokenQuotaExceeded,
    TooManyDownloads,
    TooManyStreams,
    UsageService,
    UsageStoreUnavailable,
    check_token_quota,
    record_model_call,
)
from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage
from docchat_core.opsconfig import ChatOps, OpsConfig, QuotaOps, RateLimitOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
MINUTE = int(START.timestamp()) // 60

RATELIMIT = RateLimitOps.model_validate(
    {
        "ratelimit.chat_requests_per_minute": 3,
        "ratelimit.max_concurrent_streams": 2,
        "ratelimit.citation_downloads_per_minute": 30,
        "ratelimit.max_concurrent_downloads": 1,
    }
)
QUOTA = QuotaOps.model_validate({"quota.tokens_per_day": 10_000, "quota.tokens_per_month": 50_000})
CHAT = ChatOps.model_validate(
    {
        "chat.context_token_budget": 40_000,
        "chat.answer_max_completion_tokens": 12_000,
        "chat.small_call_max_completion_tokens": 800,
        "chat.rewrite_history_turns": 4,
        "chat.rewrite_timeout_seconds": 8,
        "chat.max_turn_seconds": 180,
    }
)


class Captured(Protocol):
    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


class StaticOps:
    """An ops source holding one snapshot with the quota area only."""

    def __init__(self, quota: QuotaOps) -> None:
        self.snapshots = 0
        self._config = OpsConfig.model_validate({"quota": quota})

    async def snapshot(self) -> OpsConfig:
        self.snapshots += 1
        return self._config


def _record() -> ModelCallUsage:
    return ModelCallUsage(
        call=ModelCall.SUMMARY,
        deployment="chat-small",
        model="gpt-5.6-terra",
        prompt_tokens=4_000,
        completion_tokens=500,
        reasoning_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        total_tokens=4_500,
        estimated=False,
        estimated_cost_micros=None,
        duration_ms=900,
        outcome=CallOutcome.OK,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


@pytest.fixture
def ops() -> StaticOps:
    return StaticOps(QUOTA)


@pytest.fixture
def service(container: FakeContainer, clock: FakeClock, ops: StaticOps) -> UsageService:
    return UsageService(container, ops=ops, clock=clock, sleep=clock.sleep)


@pytest.fixture
async def installed(service: UsageService) -> AsyncIterator[UsageService]:
    service.install()
    try:
        yield service
    finally:
        await service.aclose()


@pytest.fixture
def no_service() -> Iterator[None]:
    assert UsageService._current is None
    yield
    assert UsageService._current is None


# --- P3, P4 and P5 --------------------------------------------------------------------------


async def test_p3_counts_and_refuses_the_fourth_request(service: UsageService) -> None:
    for _ in range(3):
        await service.check_chat_rate(USER, RATELIMIT)

    with pytest.raises(RateLimited):
        await service.check_chat_rate(USER, RATELIMIT)


async def test_t223_a_rate_window_read_that_never_returns_is_503_after_2_x_2_s(
    service: UsageService, container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    container.fail("read_item", hang=True, item_id=f"rate:{MINUTE}", times=2)
    start = clock.monotonic()

    with pytest.raises(DependencyUnavailable) as caught:
        await service.check_chat_rate(USER, RATELIMIT)

    assert caught.value.retry_after_seconds == 5
    assert clock.monotonic() - start == 4
    assert captured.points("docchat.ratelimit.rejections") == {}


async def test_p4_refuses_a_reached_quota_with_its_wait(
    service: UsageService, container: FakeContainer, captured: Captured
) -> None:
    container.put({"id": "day:2026-10-30", "userId": str(USER), "totalTokens": 10_000})

    with pytest.raises(TokenQuotaExceeded) as caught:
        await service.check_quota(USER, QUOTA)

    assert caught.value.retry_after_seconds == 43_200
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "quota_day"),): 1}


async def test_p4_names_the_month_when_both_are_reached(
    service: UsageService, container: FakeContainer, captured: Captured
) -> None:
    container.put({"id": "day:2026-10-30", "userId": str(USER), "totalTokens": 10_000})
    container.put({"id": "month:2026-10", "userId": str(USER), "totalTokens": 50_000})

    with pytest.raises(TokenQuotaExceeded) as caught:
        await service.check_quota(USER, QUOTA)

    assert caught.value.retry_after_seconds == 43_200 + 86_400
    assert captured.points("docchat.ratelimit.rejections") == {(("reason", "quota_month"),): 1}


async def test_p4_under_quota_passes(service: UsageService) -> None:
    await service.check_quota(USER, QUOTA)


async def test_p4_fails_closed(service: UsageService, container: FakeContainer) -> None:
    container.fail("read_item", error=ServiceRequestError("down"), times=2)

    with pytest.raises(DependencyUnavailable):
        await service.check_quota(USER, QUOTA)


async def test_p5_holds_a_lease_for_the_turn_cap_plus_30_s(
    service: UsageService, container: FakeContainer
) -> None:
    session, turn = uuid4(), uuid4()
    lease = await service.acquire_stream_lease(
        USER, session_id=session, turn_id=turn, ratelimit=RATELIMIT, chat=CHAT
    )
    await service.acquire_stream_lease(
        USER, session_id=uuid4(), turn_id=uuid4(), ratelimit=RATELIMIT, chat=CHAT
    )

    with pytest.raises(TooManyStreams):
        await service.acquire_stream_lease(
            USER, session_id=uuid4(), turn_id=uuid4(), ratelimit=RATELIMIT, chat=CHAT
        )
    stored = container.get(str(USER), "lease:1")
    assert stored is not None
    assert (stored["ttl"], stored["sessionId"], stored["turnId"]) == (210, str(session), str(turn))
    await lease.release()
    assert container.get(str(USER), "lease:1") is None


async def test_p5_fails_closed(service: UsageService, container: FakeContainer) -> None:
    container.fail("create_item", hang=True, times=2)

    with pytest.raises(DependencyUnavailable):
        await service.acquire_stream_lease(
            USER, session_id=uuid4(), turn_id=uuid4(), ratelimit=RATELIMIT, chat=CHAT
        )


async def test_the_download_limits(service: UsageService, container: FakeContainer) -> None:
    await service.check_download_rate(USER, RATELIMIT)
    lease = await service.acquire_download_lease(
        USER, session_id=None, turn_id=None, ratelimit=RATELIMIT
    )

    with pytest.raises(TooManyDownloads):
        await service.acquire_download_lease(
            USER, session_id=uuid4(), turn_id=uuid4(), ratelimit=RATELIMIT
        )
    window = container.get(str(USER), f"dlrate:{MINUTE}")
    assert window is not None
    assert window["count"] == 1
    assert lease.item_id == "dllease:1"


async def test_the_download_limits_fail_closed(
    service: UsageService, container: FakeContainer
) -> None:
    container.fail("read_item", error=ServiceRequestError("down"))
    container.fail("create_item", error=ServiceRequestError("down"))

    with pytest.raises(DependencyUnavailable):
        await service.check_download_rate(USER, RATELIMIT)
    with pytest.raises(DependencyUnavailable):
        await service.acquire_download_lease(
            USER, session_id=None, turn_id=None, ratelimit=RATELIMIT
        )


# --- The module functions and the process's service --------------------------------------------


async def test_check_token_quota_reads_the_current_snapshot(
    installed: UsageService, container: FakeContainer, ops: StaticOps
) -> None:
    allowed = await check_token_quota(USER)
    container.put({"id": "month:2026-10", "userId": str(USER), "totalTokens": 50_000})
    refused = await check_token_quota(USER)

    assert allowed.allowed
    assert (refused.allowed, refused.exceeded) == (False, "month")
    assert refused.retry_after_seconds == 43_200 + 86_400
    assert ops.snapshots == 2


async def test_t223_check_token_quota_raises_usage_store_unavailable(
    installed: UsageService, container: FakeContainer
) -> None:
    container.fail("read_item", hang=True, item_id="day:2026-10-30", times=2)

    with pytest.raises(UsageStoreUnavailable):
        await check_token_quota(USER)


async def test_record_model_call_reaches_both_counters(
    installed: UsageService, container: FakeContainer
) -> None:
    written = record_model_call(USER, _record())
    await written

    day = container.get(str(USER), "day:2026-10-30")
    month = container.get(str(USER), "month:2026-10")
    assert day is not None
    assert month is not None
    assert (day["totalTokens"], month["unpricedCalls"]) == (4_500, 1)


async def test_shutdown_waits_for_running_writes(
    service: UsageService, container: FakeContainer
) -> None:
    gate = asyncio.Event()
    container.before("patch_item", gate.wait)
    service.install()
    writing = service.record(USER, _record())

    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    gate.set()
    await closing
    await writing

    assert container.get(str(USER), "day:2026-10-30") is not None


async def test_one_service_per_process(service: UsageService, no_service: None) -> None:
    with pytest.raises(RuntimeError, match="no usage service"):
        UsageService.current()
    service.install()
    try:
        assert UsageService.current() is service
        with pytest.raises(RuntimeError, match="already installed"):
            UsageService(FakeContainer.of(USAGE), ops=StaticOps(QUOTA)).install()
    finally:
        await service.aclose()
