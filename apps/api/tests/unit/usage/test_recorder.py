from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from docchat_api.usage.recorder import Recorder, counter_document, counter_patch
from docchat_api.usage.tasks import PendingTasks
from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage
from docchat_core.cosmos.usage import UsageDayCounter, UsageMonthCounter
from docchat_core.models import format_utc
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
START = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)


class Captured(Protocol):
    metrics: InMemoryMetricReader

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


def histogram_points(captured: Captured, name: str) -> list[dict[str, object]]:
    """The attribute sets of a histogram's data points (`points` reads sums only)."""
    data = captured.metrics.get_metrics_data()
    if data is None:
        return []
    return [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _record(**overrides: Any) -> ModelCallUsage:
    values: dict[str, Any] = {
        "call": ModelCall.ANSWER,
        "deployment": "chat",
        "model": "gpt-5.6-sol",
        "prompt_tokens": 1_000,
        "completion_tokens": 300,
        "reasoning_tokens": 100,
        "cached_tokens": 200,
        "cache_write_tokens": 0,
        "total_tokens": 1_300,
        "estimated": False,
        "estimated_cost_micros": 5_950,
        "duration_ms": 1_200,
        "outcome": CallOutcome.OK,
    }
    return ModelCallUsage.model_validate(values | overrides)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


@pytest.fixture
def recorder(container: FakeContainer, clock: FakeClock) -> Recorder:
    return Recorder(container, clock=clock, tasks=PendingTasks(), sleep=clock.sleep)


def test_a_patch_has_at_most_10_operations() -> None:
    estimated_unpriced = _record(estimated=True, estimated_cost_micros=None)

    operations = counter_patch(estimated_unpriced, START)

    assert len(operations) == 10
    assert {"op": "incr", "path": "/unpricedCalls", "value": 1} in operations
    assert {"op": "incr", "path": "/estimatedCalls", "value": 1} in operations
    assert operations[-1] == {"op": "set", "path": "/updatedAt", "value": format_utc(START)}
    priced = counter_patch(_record(), START)
    assert {"op": "incr", "path": "/estimatedCostMicros", "value": 5_950} in priced
    assert len(priced) == 9


def test_a_new_counter_starts_from_the_record() -> None:
    document = counter_document(UsageMonthCounter, USER, _record(), START)

    assert document["id"] == "month:2026-10"
    assert document["period"] == "2026-10"
    assert document["ttl"] == 34_560_000
    assert (document["totalTokens"], document["calls"], document["estimatedCostMicros"]) == (
        1_300,
        1,
        5_950,
    )
    assert counter_document(UsageDayCounter, USER, _record(), START)["ttl"] == 3_456_000


async def test_the_first_record_creates_both_counters_and_the_next_patches_them(
    recorder: Recorder, container: FakeContainer
) -> None:
    await recorder.record(USER, _record())
    await recorder.record(USER, _record(estimated=True, estimated_cost_micros=None))

    day = container.get(str(USER), "day:2026-10-31")
    month = container.get(str(USER), "month:2026-10")
    assert day is not None
    assert month is not None
    for counter in (day, month):
        assert counter["promptTokens"] == 2_000
        assert counter["completionTokens"] == 600
        assert counter["reasoningTokens"] == 200
        assert counter["cachedTokens"] == 400
        assert counter["totalTokens"] == 2_600
        assert counter["calls"] == 2
        assert counter["estimatedCostMicros"] == 5_950
        assert counter["unpricedCalls"] == 1
        assert counter["estimatedCalls"] == 1
    assert day["type"] == "usage_day"
    assert {call.kwargs["timeout"] for call in container.calls} == {3}


async def test_a_counter_created_meanwhile_is_patched_once_more(
    recorder: Recorder, container: FakeContainer
) -> None:
    rival = counter_document(UsageDayCounter, USER, _record(), START)

    def rival_creates_it() -> None:
        container.put(rival)

    container.before("create_item", rival_creates_it, item_id="day:2026-10-31")

    await recorder.record(USER, _record())

    day = container.get(str(USER), "day:2026-10-31")
    assert day is not None
    assert day["calls"] == 2
    # Both counters missing, then the day counter once more after its create's 409.
    assert len(container.calls_of("patch_item")) == 3


async def test_throttling_is_retried_0_2_then_0_4_s_apart(
    recorder: Recorder, container: FakeContainer, clock: FakeClock
) -> None:
    container.put(counter_document(UsageDayCounter, USER, _record(), START))
    container.put(counter_document(UsageMonthCounter, USER, _record(), START))
    container.fail("patch_item", status=429, item_id="day:2026-10-31")
    container.fail("patch_item", status=449, item_id="day:2026-10-31")
    start = clock.monotonic()

    await recorder.record(USER, _record())

    day = container.get(str(USER), "day:2026-10-31")
    assert day is not None
    assert day["calls"] == 2
    assert clock.monotonic() - start == pytest.approx(0.6)


async def test_a_write_that_cant_be_retried_logs_one_failure(
    recorder: Recorder, container: FakeContainer, captured: Captured
) -> None:
    container.fail("patch_item", error=ServiceRequestError("down"), times=2)
    session, turn = uuid4(), uuid4()

    await recorder.record(USER, _record(), session_id=session, turn_id=turn)

    (failed,) = captured.records("usage_record_failed")
    assert failed.levelno == logging.ERROR
    assert failed.__dict__["docchat.deployment"] == "chat"
    assert failed.__dict__["docchat.session_id"] == str(session)
    assert failed.__dict__["docchat.turn_id"] == str(turn)
    assert captured.points("docchat.usage.record_failures") == {(): 1}


async def test_t223_a_counter_patch_that_never_returns_logs_usage_record_failed(
    recorder: Recorder, container: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    container.put(counter_document(UsageMonthCounter, USER, _record(), START))
    container.fail("patch_item", hang=True, item_id="day:2026-10-31", times=3)
    start = clock.monotonic()

    await recorder.record(USER, _record())

    assert clock.monotonic() - start == pytest.approx(3 * 3 + 0.2 + 0.4)
    assert len(captured.records("usage_record_failed")) == 1
    month = container.get(str(USER), "month:2026-10")
    assert month is not None
    assert month["calls"] == 2


async def test_each_record_emits_its_metrics(recorder: Recorder, captured: Captured) -> None:
    await recorder.record(USER, _record(cache_write_tokens=0))

    tokens = captured.points("docchat.openai.tokens")
    answer = (("call", "answer"), ("deployment", "chat"))
    assert tokens[(*answer, ("kind", "prompt"))] == 1_000
    assert tokens[(*answer, ("kind", "reasoning"))] == 100
    assert not any(("kind", "cache_write") in key for key in tokens)
    assert captured.points("docchat.openai.estimated_cost") == {
        (("deployment", "chat"), ("model", "gpt-5.6-sol")): 5_950
    }
    assert histogram_points(captured, "docchat.openai.duration") == [
        {"deployment": "chat", "call": "answer", "outcome": "ok"}
    ]


async def test_cancelling_the_awaiter_never_cancels_the_writes(
    recorder: Recorder, container: FakeContainer
) -> None:
    gate = asyncio.Event()
    container.before("patch_item", gate.wait, times=2)
    tasks = PendingTasks()
    recorder = Recorder(container, clock=FakeClock(START), tasks=tasks, sleep=asyncio.sleep)

    waiter = asyncio.ensure_future(recorder.record(USER, _record()))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    await tasks.drain()

    assert container.get(str(USER), "day:2026-10-31") is not None
