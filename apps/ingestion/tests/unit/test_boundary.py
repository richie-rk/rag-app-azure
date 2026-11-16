"""The failure boundary (`14-ingestion.md` sections 4.5 and 4.6; ADR 0144; part of T-145): one
record per unexpected failure with its type name and `internal_error`, never its text; a passed
deadline is `activity_timeout`; only the queue starter re-raises, as `DispatcherUnavailableError`
with no cause."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord

from docchat_core.telemetry import TestExporters, start_span
from docchat_ingestion.boundary import (
    ACTIVITY_TIMEOUT,
    INTERNAL_ERROR,
    DispatcherUnavailableError,
    failure_result,
    guard,
    run_activity,
)

CANARY = "cnry-chunk-0a1b2c3d"


def _records(exporters: TestExporters, name: str) -> list[ReadableLogRecord]:
    return [item for item in exporters.logs.get_finished_logs() if item.log_record.body == name]


def _everything(exporters: TestExporters) -> str:
    logs = [
        str(item.log_record.body) + str(item.log_record.attributes)
        for item in exporters.logs.get_finished_logs()
    ]
    spans = [span.to_json() for span in exporters.spans.get_finished_spans()]
    return "\n".join(logs + spans)


async def test_t145_an_activity_failure_is_recorded_once_without_its_text(
    telemetry: TestExporters,
) -> None:
    with start_span("docchat.ingestion.activity", **{"docchat.activity_name": "chunk_document"}):
        async with guard("chunk_document") as boundary:
            raise ValueError(CANARY)

    assert boundary.failed is True
    (record,) = _records(telemetry, "activity_failed")
    assert dict(record.log_record.attributes or {}) == {
        "error.type": "builtins.ValueError",
        "docchat.error_code": INTERNAL_ERROR,
    }
    (span,) = telemetry.spans.get_finished_spans()
    (event,) = span.events
    assert event.name == "exception"
    assert dict(event.attributes or {})["docchat.error_code"] == INTERNAL_ERROR
    assert CANARY not in _everything(telemetry)


@pytest.mark.parametrize("trigger", ["ingestion_keepalive", "ingestion_maintenance"])
async def test_t145_a_timer_failure_is_job_failed_and_returns(
    telemetry: TestExporters, trigger: str
) -> None:
    async with guard(trigger):
        raise RuntimeError(CANARY)

    (record,) = _records(telemetry, "job_failed")
    assert dict(record.log_record.attributes or {})["error.type"] == "builtins.RuntimeError"
    assert CANARY not in _everything(telemetry)


async def test_cancellation_passes_through_unrecorded(telemetry: TestExporters) -> None:
    with pytest.raises(asyncio.CancelledError):
        async with guard("load_dispatch_settings"):
            raise asyncio.CancelledError

    assert _records(telemetry, "activity_failed") == []


async def test_a_clean_block_records_nothing(telemetry: TestExporters) -> None:
    async with guard("load_dispatch_settings") as boundary:
        pass

    assert boundary.failed is False
    assert telemetry.logs.get_finished_logs() == ()


async def _starter_body() -> None:
    def durable_call() -> None:
        raise OSError(CANARY)

    async with guard("ingestion_queue_starter") as boundary:
        try:
            durable_call()
        except OSError as failure:
            raise boundary.escalate(failure) from None


async def test_escalate_records_once_and_raises_without_a_cause(telemetry: TestExporters) -> None:
    with pytest.raises(DispatcherUnavailableError) as raised:
        await _starter_body()

    assert str(raised.value) == "durable client call failed"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert len(_records(telemetry, "job_failed")) == 1
    assert CANARY not in _everything(telemetry)


def test_only_the_queue_starter_escalates() -> None:
    with pytest.raises(ValueError, match="only the queue starter"):
        guard("ingestion_keepalive").escalate(RuntimeError("x"))


def test_a_guard_names_its_function() -> None:
    with pytest.raises(ValueError, match="names its function"):
        guard("")


def test_failure_result_follows_the_catalogue() -> None:
    assert failure_result(INTERNAL_ERROR) == {
        "ok": False,
        "errorCode": INTERNAL_ERROR,
        "retryable": True,
        "retryAfterSeconds": None,
    }


async def test_run_activity_returns_the_body_result() -> None:
    async def body() -> str:
        return "done"

    assert await run_activity("claim_ingestion_items", 1, body, lambda code: code) == "done"


async def test_run_activity_maps_an_exception_to_internal_error(telemetry: TestExporters) -> None:
    async def body() -> str:
        raise KeyError(CANARY)

    assert await run_activity("claim_ingestion_items", 1, body, lambda code: code) == INTERNAL_ERROR
    assert len(_records(telemetry, "activity_failed")) == 1


async def test_run_activity_maps_a_passed_deadline_to_activity_timeout(
    telemetry: TestExporters,
) -> None:
    never = asyncio.Event()

    async def body() -> str:
        await never.wait()
        return "never"

    result = await run_activity("sweep_ingestion_state", 0.01, body, lambda code: code)

    assert result == ACTIVITY_TIMEOUT
    assert _records(telemetry, "activity_failed") == []


async def test_a_timeout_raised_by_the_body_is_an_unexpected_failure(
    telemetry: TestExporters,
) -> None:
    async def body() -> Any:
        raise TimeoutError

    assert await run_activity("sweep_ingestion_state", 5, body, lambda code: code) == INTERNAL_ERROR
