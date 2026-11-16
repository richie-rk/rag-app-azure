"""`index_item_orchestrator` through the orchestration harness (`14-ingestion.md` sections 8.12,
6.1 and 4.7): one activity through the retry wrapper, the purge repeated until done (at most 20
calls), then the outcome, and nothing after a lost claim."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from docchat_core.ingestion.models import ItemOperation
from docchat_ingestion.orchestrators import index_item_orchestrator as orchestrator
from docchat_testing.durable import OrchestrationHarness

ITEM_ID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
RUN_ID = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
OK: dict[str, Any] = {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}


def _input(operation: ItemOperation, max_attempts: int = 3) -> dict[str, Any]:
    return {
        "itemId": str(ITEM_ID),
        "claimNumber": 2,
        "operation": operation.value,
        "runId": str(RUN_ID),
        "traceparent": None,
        "tracestate": None,
        "retry": {"maxAttempts": max_attempts, "firstDelaySeconds": 15, "maxDelaySeconds": 480},
        "settings": {
            "activityTimeBudgetSeconds": 1200,
            "maxPagesPerDocument": 2000,
            "pagewisePagesPerExtract": 40,
            "maxParseSecondsPerDocument": 7200,
            "embeddingBatchMaxInputs": 64,
            "embeddingBatchMaxTokens": 60000,
            "embeddingConcurrency": 2,
            "indexBatchMaxDocuments": 100,
            "indexBatchMaxBytes": 8000000,
            "tableSummaryConcurrency": 4,
            "tableSummaryMaxInputTokens": 6000,
            "tableSummaryMaxOutputTokens": 300,
        },
    }


def _failure(code: str, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "errorCode": code, "retryable": retryable, "retryAfterSeconds": None}


class Outcomes:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.envelopes: list[dict[str, Any]] = []

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.recorded.append(payload)
        return OK

    def envelope(self, result: dict[str, Any]) -> Any:
        def activity(payload: dict[str, Any]) -> dict[str, Any]:
            self.envelopes.append(payload)
            return result

        return activity


async def _run(
    operation: ItemOperation, scripted: list[dict[str, Any]], max_attempts: int = 3
) -> tuple[OrchestrationHarness, Outcomes, str]:
    harness = OrchestrationHarness()
    outcomes = Outcomes()
    harness.register_orchestrator(orchestrator.NAME, orchestrator.index_item_orchestrator)
    harness.register_activity(orchestrator.RECORD_OUTCOME, outcomes.record)
    activity, _ = orchestrator.ACTIVITIES[operation]
    harness.script_activity(activity, *scripted)
    instance = await harness.start(
        orchestrator.NAME,
        _input(operation, max_attempts),
        instance_id=f"docchat-item-{ITEM_ID}-2",
    )
    await harness.run_to_completion(instance)
    return harness, outcomes, instance


@pytest.mark.parametrize(
    ("operation", "activity", "stage"),
    [
        (ItemOperation.CREATE_PHYSICAL_INDEX, "create_search_index", "create_index"),
        (ItemOperation.DELETE_PHYSICAL_INDEX, "delete_search_index", "delete_index"),
    ],
)
async def test_one_activity_then_a_succeeded_outcome(
    operation: ItemOperation, activity: str, stage: str
) -> None:
    harness, outcomes, instance = await _run(operation, [{**OK, "activated": True}])

    assert harness.status(instance).value == "Completed"
    assert harness.activity_starts(activity) == 1
    assert outcomes.recorded == [
        {
            "itemId": str(ITEM_ID),
            "claimNumber": 2,
            "state": "succeeded",
            "code": None,
            "stage": None,
            "warnings": [],
            "usage": {
                "embeddingTokens": 0,
                "summaryInputTokens": 0,
                "summaryOutputTokens": 0,
                "docIntelPages": 0,
            },
        }
    ]
    del stage


async def test_a_failure_records_its_code_and_stage_after_the_retries() -> None:
    harness, outcomes, _ = await _run(
        ItemOperation.CREATE_PHYSICAL_INDEX,
        [_failure("search_unavailable", True)] * 3,
    )

    assert harness.activity_starts("create_search_index") == 3
    (recorded,) = outcomes.recorded
    assert (recorded["state"], recorded["code"], recorded["stage"]) == (
        "failed",
        "search_unavailable",
        "create_index",
    )


async def test_a_non_retryable_failure_is_recorded_at_once() -> None:
    harness, outcomes, _ = await _run(
        ItemOperation.CREATE_PHYSICAL_INDEX, [_failure("index_schema_conflict", False)]
    )

    assert harness.activity_starts("create_search_index") == 1
    assert outcomes.recorded[0]["code"] == "index_schema_conflict"


async def test_a_lost_claim_records_nothing() -> None:
    harness, outcomes, instance = await _run(
        ItemOperation.DELETE_PHYSICAL_INDEX, [_failure("lease_lost", False)]
    )

    assert harness.status(instance).value == "Completed"
    assert outcomes.recorded == []


async def test_the_purge_repeats_until_done() -> None:
    not_done = {**OK, "done": False, "deletedBlobs": 256}
    harness, outcomes, _ = await _run(
        ItemOperation.PURGE_INDEX_BLOBS,
        [not_done, not_done, {**OK, "done": True, "deletedBlobs": 3}],
    )

    assert harness.activity_starts("purge_index_blobs") == 3
    assert outcomes.recorded[0]["state"] == "succeeded"


async def test_a_21st_purge_call_is_never_made() -> None:
    not_done = {**OK, "done": False, "deletedBlobs": 256}
    harness, outcomes, _ = await _run(ItemOperation.PURGE_INDEX_BLOBS, [not_done] * 25)

    assert harness.activity_starts("purge_index_blobs") == 20
    (recorded,) = outcomes.recorded
    assert (recorded["state"], recorded["code"], recorded["stage"]) == (
        "failed",
        "activity_timeout",
        "purge_blobs",
    )


async def test_the_activity_receives_the_item_envelope() -> None:
    harness = OrchestrationHarness()
    outcomes = Outcomes()
    harness.register_orchestrator(orchestrator.NAME, orchestrator.index_item_orchestrator)
    harness.register_activity(orchestrator.RECORD_OUTCOME, outcomes.record)
    harness.register_activity("create_search_index", outcomes.envelope({**OK, "activated": False}))
    instance = await harness.start(
        orchestrator.NAME, _input(ItemOperation.CREATE_PHYSICAL_INDEX), instance_id="i"
    )
    await harness.run_to_completion(instance)

    (envelope,) = outcomes.envelopes
    assert set(envelope) == {
        "itemId",
        "claimNumber",
        "runId",
        "traceparent",
        "tracestate",
        "settings",
    }


@pytest.mark.parametrize(
    "given",
    [
        {"itemId": "cnry-input-0a1b2c3d"},
        _input(ItemOperation.INGEST),
    ],
)
async def test_an_input_that_is_no_index_operation_fails_without_its_values(
    given: dict[str, Any],
) -> None:
    harness = OrchestrationHarness()
    harness.register_orchestrator(orchestrator.NAME, orchestrator.index_item_orchestrator)
    instance = await harness.start(orchestrator.NAME, given, instance_id="i")
    await harness.run_until_idle()

    assert harness.status(instance).value == "Failed"
    assert "cnry-" not in str(harness.output(instance))
