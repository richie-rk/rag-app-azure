"""The per-user limits and the counters against the Cosmos DB emulator (T-217, T-218, T-221's
background gate, T-222; `12-chat-pipeline.md` sections 3 and 15.6).

Every test uses a fresh user, whose `oid` is its partition, and a `FakeClock` held at the start of
a minute, so the windows and the lease expiries are decided in virtual time while the emulator
applies the ETag conditions, the `409`s and the patch `incr`s.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from azure.cosmos.aio import DatabaseProxy

from docchat_api.usage import (
    RateLimited,
    TooManyStreams,
    UsageContainer,
    UsageService,
)
from docchat_api.usage.windows import CONFLICT_RETRY_AFTER_SECONDS
from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage
from docchat_core.opsconfig import ChatOps, OpsConfig, QuotaOps, RateLimitOps
from docchat_testing.clock import FakeClock

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
MINUTE = int(START.timestamp()) // 60
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


def _ratelimit(requests: int = 10, streams: int = 3) -> RateLimitOps:
    return RateLimitOps.model_validate(
        {
            "ratelimit.chat_requests_per_minute": requests,
            "ratelimit.max_concurrent_streams": streams,
            "ratelimit.citation_downloads_per_minute": 30,
            "ratelimit.max_concurrent_downloads": 2,
        }
    )


class StaticOps:
    async def snapshot(self) -> OpsConfig:
        return OpsConfig.model_validate({"quota": QUOTA})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def usage_container(cosmos_database: DatabaseProxy) -> UsageContainer:
    return cast("UsageContainer", cosmos_database.get_container_client(USAGE.name))


def _service(container: UsageContainer, clock: FakeClock) -> UsageService:
    return UsageService(container, ops=StaticOps(), clock=clock)


async def test_t217_20_concurrent_requests_across_two_instances_allow_exactly_10(
    usage_container: UsageContainer, clock: FakeClock
) -> None:
    user = uuid4()
    instances = [_service(usage_container, clock), _service(usage_container, clock)]
    ratelimit = _ratelimit(requests=10)

    async def request(index: int) -> bool:
        # A request that lost five ETag races in a row is refused with 1 s; like the web client,
        # it is sent again (the clock doesn't move, so it stays in the same minute).
        while True:
            try:
                await instances[index % 2].check_chat_rate(user, ratelimit)
            except RateLimited as refused:
                if refused.retry_after_seconds != CONFLICT_RETRY_AFTER_SECONDS:
                    return False
            else:
                return True

    allowed = await asyncio.gather(*(request(index) for index in range(20)))

    assert sum(allowed) == 10
    window = await usage_container.read_item(f"rate:{MINUTE}", str(user), timeout=2)
    assert window["count"] == 10
    for _ in range(3):
        with pytest.raises(RateLimited):
            await instances[0].check_chat_rate(user, ratelimit)
    window = await usage_container.read_item(f"rate:{MINUTE}", str(user), timeout=2)
    assert window["count"] == 10


async def test_t218_3_slots_give_exactly_3_of_10_concurrent_leases(
    usage_container: UsageContainer, clock: FakeClock
) -> None:
    user = uuid4()
    service = _service(usage_container, clock)
    ratelimit = _ratelimit(streams=3)

    async def acquire() -> Any:
        try:
            return await service.acquire_stream_lease(
                user, session_id=uuid4(), turn_id=uuid4(), ratelimit=ratelimit, chat=CHAT
            )
        except TooManyStreams:
            return None

    leases = [lease for lease in await asyncio.gather(*(acquire() for _ in range(10))) if lease]

    assert sorted(lease.slot for lease in leases) == [1, 2, 3]
    await leases[0].release()
    again = await acquire()
    assert again is not None
    assert again.slot == leases[0].slot
    await service.aclose()


async def test_t218_an_expired_lease_is_taken_over_and_an_old_etag_never_deletes_it(
    usage_container: UsageContainer, clock: FakeClock
) -> None:
    user = uuid4()
    service = _service(usage_container, clock)
    ratelimit = _ratelimit(streams=1)
    old = await service.acquire_stream_lease(
        user, session_id=uuid4(), turn_id=uuid4(), ratelimit=ratelimit, chat=CHAT
    )
    clock.advance(211)

    new = await service.acquire_stream_lease(
        user, session_id=uuid4(), turn_id=uuid4(), ratelimit=ratelimit, chat=CHAT
    )
    await old.release()

    stored = await usage_container.read_item("lease:1", str(user), timeout=2)
    assert stored["_etag"] == new.etag
    await new.release()
    await service.aclose()


def _record(total: int = 100) -> ModelCallUsage:
    return ModelCallUsage(
        call=ModelCall.ANSWER,
        deployment="chat",
        model="gpt-5.6-sol",
        prompt_tokens=total - 10,
        completion_tokens=10,
        reasoning_tokens=2,
        cached_tokens=5,
        cache_write_tokens=0,
        total_tokens=total,
        estimated=False,
        estimated_cost_micros=7,
        duration_ms=50,
        outcome=CallOutcome.OK,
    )


async def test_t222_50_concurrent_records_give_exact_totals(
    usage_container: UsageContainer, clock: FakeClock
) -> None:
    user = uuid4()
    service = _service(usage_container, clock)

    await asyncio.gather(*(service.record(user, _record()) for _ in range(50)))

    for item_id in ("day:2026-10-30", "month:2026-10"):
        counter = await usage_container.read_item(item_id, str(user), timeout=2)
        assert counter["totalTokens"] == 5_000
        assert counter["promptTokens"] == 4_500
        assert counter["reasoningTokens"] == 100
        assert counter["cachedTokens"] == 250
        assert counter["calls"] == 50
        assert counter["estimatedCostMicros"] == 350
    await service.aclose()


async def test_t221_the_background_gate_defers_at_the_threshold(
    usage_container: UsageContainer, clock: FakeClock
) -> None:
    user: UUID = uuid4()
    service = _service(usage_container, clock)

    assert (await service.token_quota(user)).allowed
    await service.record(user, _record(total=10_000))
    check = await service.token_quota(user)

    assert (check.allowed, check.exceeded, check.retry_after_seconds) == (False, "day", 43_200)
    await service.aclose()
