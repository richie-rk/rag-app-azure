"""`document_item_orchestrator` through the orchestration harness (`14-ingestion.md` sections 6.1,
4.7 and 4.8; T-171's crash after Embed, the lost claim, the failure path, the Flip's outcomes,
the clean-up attempts, the 20-call cap, `cleanup_document` and `rerender`).

The activities are scripted here, because these tests prove the orchestrator's own rules;
`tests/unit/activities/test_ingest_steps.py` runs the `ingest` operation with the real Extract,
Enrich and Chunk activities and methods, beside the activity fakes they need."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from azure.durable_functions import OrchestrationRuntimeStatus

from docchat_core.ingestion.methods import PAGEWISE_V1, PagewiseParametersV1
from docchat_core.ingestion.models import ContentType, ItemOperation
from docchat_core.ingestion.names import source_path, work_prefix
from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.activities.documents import BuildPlan, PhysicalIndexPlan
from docchat_ingestion.activities.validate_item import ValidateResult
from docchat_ingestion.orchestrators import document_item_orchestrator as orchestrator
from docchat_testing.clock import FakeClock
from docchat_testing.durable import OrchestrationHarness

ITEM_ID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
RUN_ID = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
START = datetime(2026, 3, 1, 9, tzinfo=UTC)
OK: dict[str, Any] = {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}
COMPLETED = OrchestrationRuntimeStatus.Completed


def failure(code: str, retryable: bool, retry_after: int | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "errorCode": code,
        "retryable": retryable,
        "retryAfterSeconds": retry_after,
    }


def item_input(operation: ItemOperation, max_attempts: int = 3) -> dict[str, Any]:
    return {
        "itemId": str(ITEM_ID),
        "claimNumber": 2,
        "operation": operation.value,
        "runId": str(RUN_ID),
        "traceparent": TRACEPARENT,
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


def build_plan() -> BuildPlan:
    index, document = uuid4(), uuid4()
    return BuildPlan(
        chunk_set_id=uuid4(),
        index_id=index,
        document_id=document,
        version_id=uuid4(),
        version_number=1,
        content_type=ContentType.PDF,
        source_blob_path=source_path(index, document, 1),
        source_sha256="a" * 64,
        conversion=None,
        title="Annual report",
        work_prefix=work_prefix(RUN_ID, document, 1),
        physical_index=PhysicalIndexPlan(
            id=uuid4(),
            name="docchat-0123456789abcdef0123456789abcdef-g1",
            is_active=True,
            schema_version=1,
            dimensions=1024,
            encoding=TokenEncoding.CL100K_BASE,
            max_input_tokens=8192,
            ingestion_deployment="embed-ingest-te3-large-v1",
        ),
        method=PAGEWISE_V1.snapshot(PagewiseParametersV1()),
    )


PLAN = build_plan()
VALIDATED = ValidateResult.build(PLAN).model_dump(mode="json")


def flip(outcome: str, code: str | None = None, retired: int = 1) -> dict[str, Any]:
    return {
        **OK,
        "outcome": outcome,
        "code": code,
        "retiredChunkSetIds": [str(uuid4()) for _ in range(retired)],
        "supersededVersionId": None,
        "buildingItemQueued": False,
    }


DEFAULTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "validate_item": lambda payload: VALIDATED,
    "extract_document": lambda payload: {
        **OK,
        "pageCount": 50,
        "nextPart": {"firstPage": 41, "lastPage": 50} if payload["part"] is None else None,
        "docIntelPages": 0,
        "warnings": [{"code": "converted_pdf_missing", "page": None}]
        if payload["part"] is None
        else [],
    },
    "enrich_document": lambda payload: {
        **OK,
        "done": True,
        "tables": 2,
        "summarised": 1,
        "filtered": 1,
        "discarded": 0,
        "inputTokens": 200,
        "outputTokens": 50,
        "warnings": [{"code": "table_summary_filtered", "page": 3}],
    },
    "chunk_document": lambda payload: {**OK, "chunkCount": 50},
    "embed_chunks": lambda payload: {**OK, "done": True, "embeddingTokens": 1000},
    "index_chunks": lambda payload: {**OK, "indexed": 50},
    "render_pages": lambda payload: {**OK, "pages": 50, "skipped": False, "documentPdf": "built"},
    "flip_chunk_set": lambda payload: flip("flipped"),
    "clean_up_item": lambda payload: {**OK, "deletedChunks": 3, "purgedVersion": False},
    "record_item_outcome": lambda payload: OK,
    "purge_document_artifacts": lambda payload: {
        **OK,
        "done": True,
        "deletedChunks": 1,
        "purgedVersions": 1,
    },
    "rerender_pages": lambda payload: {
        **OK,
        "outcome": "rendered",
        "code": None,
        "pages": 3,
        "documentPdf": "built",
    },
}


class Activities:
    """Every activity of the document path: scripted answers first, then the default, each call's
    input kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, deque[dict[str, Any]]] = {name: deque() for name in DEFAULTS}

    def script(self, name: str, *answers: dict[str, Any]) -> None:
        self.answers[name].extend(answers)

    def function(self, name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def activity(payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, payload))
            if self.answers[name]:
                return self.answers[name].popleft()
            return DEFAULTS[name](payload)

        return activity

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def inputs(self, name: str) -> list[dict[str, Any]]:
        return [payload for called, payload in self.calls if called == name]


def make_harness(clock: FakeClock | None = None) -> tuple[OrchestrationHarness, Activities]:
    harness = OrchestrationHarness(clock or FakeClock(START))
    activities = Activities()
    harness.register_orchestrator(orchestrator.NAME, orchestrator.document_item_orchestrator)
    for name in DEFAULTS:
        harness.register_activity(name, activities.function(name))
    return harness, activities


async def run(
    harness: OrchestrationHarness,
    operation: ItemOperation = ItemOperation.INGEST,
    attempts: int = 3,
) -> str:
    instance = await harness.start(
        orchestrator.NAME,
        item_input(operation, attempts),
        instance_id=f"docchat-item-{ITEM_ID}-2",
    )
    status = await harness.run_to_completion(instance)
    assert status is COMPLETED
    return instance


async def test_the_ingest_steps_in_order_with_usage_and_warnings() -> None:
    harness, activities = make_harness()
    activities.script("embed_chunks", {**OK, "done": False, "embeddingTokens": 600})
    activities.script(
        "enrich_document",
        {
            **OK,
            "done": False,
            "tables": 1,
            "summarised": 1,
            "filtered": 0,
            "discarded": 0,
            "inputTokens": 100,
            "outputTokens": 20,
            "warnings": [],
        },
    )
    await run(harness)
    assert activities.names() == [
        "validate_item",
        "extract_document",
        "extract_document",
        "enrich_document",
        "enrich_document",
        "chunk_document",
        "embed_chunks",
        "embed_chunks",
        "index_chunks",
        "render_pages",
        "flip_chunk_set",
        "clean_up_item",
        "record_item_outcome",
    ]
    first, second = activities.inputs("extract_document")
    assert first["part"] is None
    assert second["part"] == {"firstPage": 41, "lastPage": 50}
    assert first["plan"] == ValidateResult.build(PLAN).plan_fields()
    assert first["itemId"] == str(ITEM_ID)
    assert first["traceparent"] == TRACEPARENT
    (cleanup,) = activities.inputs("clean_up_item")
    assert cleanup["mode"] == "flipped"
    assert cleanup["flip"]["outcome"] == "flipped"
    (outcome,) = activities.inputs("record_item_outcome")
    assert outcome["state"] == "succeeded"
    assert outcome["code"] is None
    assert outcome["claimNumber"] == 2
    assert outcome["runId"] == str(RUN_ID)
    assert outcome["usage"] == {
        "embeddingTokens": 1600,
        "summaryInputTokens": 300,
        "summaryOutputTokens": 70,
        "docIntelPages": 0,
    }
    assert outcome["warnings"] == [
        {"code": "converted_pdf_missing", "page": None},
        {"code": "table_summary_filtered", "page": 3},
    ]


async def test_a_skip_records_the_item_skipped_and_ends() -> None:
    harness, activities = make_harness()
    activities.script(
        "validate_item", ValidateResult.skip("already_served").model_dump(mode="json")
    )
    await run(harness)
    assert activities.names() == ["validate_item", "record_item_outcome"]
    (outcome,) = activities.inputs("record_item_outcome")
    assert (outcome["state"], outcome["code"]) == ("skipped", "already_served")


async def test_a_lost_claim_ends_with_no_outcome() -> None:
    harness, activities = make_harness()
    activities.script("embed_chunks", failure("lease_lost", False))
    await run(harness)
    assert activities.names()[-1] == "embed_chunks"
    assert "record_item_outcome" not in activities.names()


async def test_a_failure_records_then_discards() -> None:
    harness, activities = make_harness()
    activities.script("index_chunks", failure("target_missing", False))
    activities.script(
        "clean_up_item", failure("cleanup_failed", True), failure("cleanup_failed", True)
    )
    await run(harness, attempts=7)
    names = activities.names()
    assert names[names.index("index_chunks") :] == [
        "index_chunks",
        "record_item_outcome",
        "clean_up_item",
        "clean_up_item",
    ]
    (outcome,) = activities.inputs("record_item_outcome")
    assert (outcome["state"], outcome["code"], outcome["stage"]) == (
        "failed",
        "target_missing",
        "index",
    )
    assert all(payload["mode"] == "discard" for payload in activities.inputs("clean_up_item"))
    assert activities.inputs("clean_up_item")[0]["flip"] is None


async def test_a_validate_failure_has_no_build_to_discard() -> None:
    harness, activities = make_harness()
    activities.script("validate_item", *[failure("database_unavailable", True)] * 3)
    await run(harness)
    assert activities.names() == ["validate_item"] * 3 + ["record_item_outcome"]
    (outcome,) = activities.inputs("record_item_outcome")
    assert outcome["stage"] == "validate"


async def test_retries_wait_on_timers_with_retry_after() -> None:
    clock = FakeClock(START)
    harness, activities = make_harness(clock)
    activities.script("embed_chunks", failure("embedding_throttled", True, 100))
    await run(harness)
    assert activities.names().count("embed_chunks") == 2
    assert (clock.now() - START).total_seconds() >= 100


async def test_a_host_reported_failure_is_retried() -> None:
    harness, activities = make_harness()
    harness.fail_activity("chunk_document", call=1, error=RuntimeError("host lost the task"))
    await run(harness)
    assert activities.names().count("chunk_document") >= 1
    (outcome,) = activities.inputs("record_item_outcome")
    assert outcome["state"] == "succeeded"


async def test_twenty_enrich_calls_without_done_fail_the_item() -> None:
    harness, activities = make_harness()
    not_done = {**DEFAULTS["enrich_document"]({}), "done": False, "warnings": []}
    activities.script("enrich_document", *[not_done] * 20)
    await run(harness)
    assert activities.names().count("enrich_document") == 20
    outcome = activities.inputs("record_item_outcome")[0]
    assert (outcome["code"], outcome["stage"]) == ("activity_timeout", "enrich")
    assert activities.inputs("clean_up_item")[0]["mode"] == "discard"


async def test_a_requeued_flip_ends_with_no_outcome() -> None:
    harness, activities = make_harness()
    activities.script("flip_chunk_set", flip("requeued"))
    await run(harness)
    assert activities.names()[-1] == "flip_chunk_set"


async def test_a_skipped_flip_cleans_up_and_records_its_code() -> None:
    harness, activities = make_harness()
    activities.script("flip_chunk_set", flip("skipped", "superseded_by_newer"))
    await run(harness)
    assert activities.names()[-2:] == ["clean_up_item", "record_item_outcome"]
    (outcome,) = activities.inputs("record_item_outcome")
    assert (outcome["state"], outcome["code"]) == ("skipped", "superseded_by_newer")


async def test_cleanup_after_a_flip_makes_three_attempts_then_warns() -> None:
    harness, activities = make_harness()
    activities.script("clean_up_item", *[failure("cleanup_failed", True)] * 3)
    await run(harness, attempts=7)
    assert activities.names().count("clean_up_item") == 3
    (outcome,) = activities.inputs("record_item_outcome")
    assert outcome["state"] == "succeeded"
    assert outcome["warnings"][-1] == {"code": "cleanup_incomplete", "page": None}


async def test_t171_a_crash_after_embed_resumes_at_index() -> None:
    harness, activities = make_harness()
    harness.crash_in_activity("index_chunks", call=1)
    await run(harness)
    for step in ("extract_document", "enrich_document", "chunk_document", "embed_chunks"):
        assert harness.activity_completions(step) == harness.activity_starts(step)
    assert activities.names().count("extract_document") == 2
    assert activities.names().count("enrich_document") == 1
    assert activities.names().count("embed_chunks") == 1
    assert harness.activity_starts("index_chunks") == 2
    assert activities.names().count("flip_chunk_set") == 1


async def test_cleanup_document_purges_until_done() -> None:
    harness, activities = make_harness()
    activities.script(
        "purge_document_artifacts",
        {**OK, "done": False, "deletedChunks": 2, "purgedVersions": 0},
    )
    await run(harness, ItemOperation.CLEANUP_DOCUMENT)
    assert activities.names() == [
        "purge_document_artifacts",
        "purge_document_artifacts",
        "record_item_outcome",
    ]
    assert "documentId" not in activities.inputs("purge_document_artifacts")[0]
    assert activities.inputs("record_item_outcome")[0]["state"] == "succeeded"


async def test_a_failed_cleanup_document() -> None:
    harness, activities = make_harness()
    activities.script("purge_document_artifacts", failure("lease_lost", False))
    await run(harness, ItemOperation.CLEANUP_DOCUMENT)
    assert activities.names() == ["purge_document_artifacts"]
    harness, activities = make_harness()
    activities.script("purge_document_artifacts", *[failure("cleanup_failed", True)] * 3)
    await run(harness, ItemOperation.CLEANUP_DOCUMENT)
    outcome = activities.inputs("record_item_outcome")[0]
    assert (outcome["state"], outcome["code"], outcome["stage"]) == (
        "failed",
        "cleanup_failed",
        "purge_document",
    )


@pytest.mark.parametrize(
    ("answer", "state", "code", "stage"),
    [
        (DEFAULTS["rerender_pages"]({}), "succeeded", None, None),
        (
            {
                **OK,
                "outcome": "skipped",
                "code": "renditions_current",
                "pages": None,
                "documentPdf": None,
            },
            "skipped",
            "renditions_current",
            None,
        ),
        (failure("render_failed", False), "failed", "render_failed", "render"),
    ],
)
async def test_rerender(
    answer: dict[str, Any], state: str, code: str | None, stage: str | None
) -> None:
    harness, activities = make_harness()
    activities.script("rerender_pages", answer)
    await run(harness, ItemOperation.RERENDER)
    assert activities.names() == ["rerender_pages", "record_item_outcome"]
    outcome = activities.inputs("record_item_outcome")[0]
    assert (outcome["state"], outcome["code"], outcome["stage"]) == (state, code, stage)


async def test_an_index_operation_is_refused() -> None:
    harness, _ = make_harness()
    instance = await harness.start(
        orchestrator.NAME, item_input(ItemOperation.CREATE_PHYSICAL_INDEX)
    )
    status = await harness.run_to_completion(instance)
    assert status is OrchestrationRuntimeStatus.Failed


def test_item_input_refuses_garbage() -> None:
    with pytest.raises(orchestrator.DocumentOrchestrationError):
        orchestrator.item_input({"itemId": "x"})
