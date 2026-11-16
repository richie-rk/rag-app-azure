"""`call_with_retry` through the orchestration harness (`14-ingestion.md` section 4.7; T-148)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from typing import Any

import pytest

from docchat_core.ingestion.models import RetrySettings
from docchat_ingestion.orchestrators.retry import (
    MAX_WAIT_SECONDS,
    OrchestrationContext,
    call_with_retry,
    host_failure,
    wait_seconds,
)
from docchat_testing.durable import OrchestrationHarness

RETRY = RetrySettings(max_attempts=7, first_delay_seconds=15, max_delay_seconds=480)
ACTIVITY = "embed_chunks"


def _throttled(retry_after: int | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "errorCode": "embedding_throttled",
        "retryable": True,
        "retryAfterSeconds": retry_after,
    }


def _failed(code: str) -> dict[str, Any]:
    return {"ok": False, "errorCode": code, "retryable": False, "retryAfterSeconds": None}


OK: dict[str, Any] = {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}


def _harness(max_attempts: int | None = None) -> OrchestrationHarness:
    harness = OrchestrationHarness()

    def orchestrator(context: OrchestrationContext) -> Generator[Any, Any, Any]:
        result = yield from call_with_retry(
            context, ACTIVITY, {"itemId": "x"}, RETRY, max_attempts=max_attempts
        )
        return result

    harness.register_orchestrator("retry_probe", orchestrator)
    return harness


def _waits(harness: OrchestrationHarness, instance: str) -> list[float]:
    events = harness.history(instance)
    waits: list[float] = []
    for index, event in enumerate(events):
        if event.kind == "TimerCreated":
            scheduled = next(
                e.timestamp for e in reversed(events[:index]) if e.kind == "OrchestratorStarted"
            )
            fire_at: datetime = event.payload
            waits.append((fire_at - scheduled).total_seconds())
    return waits


async def test_t148_backoff_waits_double_from_15_to_480_seconds() -> None:
    harness = _harness()
    harness.script_activity(ACTIVITY, *[_throttled()] * 7)
    instance = await harness.start("retry_probe")

    await harness.run_to_completion(instance)

    assert harness.activity_starts(ACTIVITY) == 7
    assert _waits(harness, instance) == [15, 30, 60, 120, 240, 480]
    assert harness.output(instance)["errorCode"] == "embedding_throttled"


async def test_t148_retry_after_is_honoured_and_capped_at_540_seconds() -> None:
    harness = _harness(max_attempts=3)
    harness.script_activity(ACTIVITY, _throttled(500), _throttled(2000), OK)
    instance = await harness.start("retry_probe")

    await harness.run_to_completion(instance)

    assert _waits(harness, instance) == [500, 540]
    assert harness.output(instance) == OK


async def test_t148_a_host_reported_failure_is_retried_as_internal_error() -> None:
    harness = _harness()
    harness.fail_activity(ACTIVITY, call=1, error=RuntimeError("cnry-host-0a1b2c3d"))
    harness.script_activity(ACTIVITY, OK)
    instance = await harness.start("retry_probe")

    await harness.run_to_completion(instance)

    assert harness.activity_starts(ACTIVITY) == 2
    assert harness.output(instance) == OK


async def test_t148_a_non_retryable_code_stops_at_once() -> None:
    harness = _harness()
    harness.script_activity(ACTIVITY, _failed("embedding_failed"))
    instance = await harness.start("retry_probe")

    await harness.run_to_completion(instance)

    assert harness.activity_starts(ACTIVITY) == 1
    assert harness.output(instance)["errorCode"] == "embedding_failed"


async def test_lease_lost_is_never_retried() -> None:
    harness = _harness()
    harness.script_activity(ACTIVITY, _failed("lease_lost"))
    instance = await harness.start("retry_probe")

    await harness.run_to_completion(instance)

    assert harness.activity_starts(ACTIVITY) == 1


def test_t148_no_wait_exceeds_540_seconds() -> None:
    wide = RetrySettings(max_attempts=10, first_delay_seconds=300, max_delay_seconds=540)
    for attempt in range(1, 10):
        for retry_after in (None, 0, 100, 539, 541, 100_000):
            assert wait_seconds(attempt, wide, retry_after) <= MAX_WAIT_SECONDS


def test_attempts_count_from_one() -> None:
    with pytest.raises(ValueError, match="count from 1"):
        wait_seconds(0, RETRY, None)


def test_a_host_failure_is_a_retryable_internal_error() -> None:
    assert host_failure() == {
        "ok": False,
        "errorCode": "internal_error",
        "retryable": True,
        "retryAfterSeconds": None,
    }
