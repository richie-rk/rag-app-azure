"""Unit tests of the ingestion ledger's closed sets and wire models (T-101; `14-ingestion.md`
sections 3.2, 4.4, 5.1 and 16; `80-data-model.md` sections 2.12 to 2.17, 5.2 and 8)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel, ValidationError
from sqlalchemy import Table

from docchat_core.errors import CODES, ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.models import (
    ACTIVE_VERSION_STATES,
    CONTENT_TYPE_BY_EXTENSION,
    DISPATCHER_INSTANCE_ID,
    DOCUMENT_OPERATIONS,
    LEASE_LOST,
    LIVE_CHUNK_SET_STATES,
    MAPPABLE_INDEX_STATUSES,
    MAX_ITEM_WARNINGS,
    ActivityResult,
    CanaryResult,
    ChunkSet,
    ChunkSetState,
    ClaimedItem,
    ContentType,
    DispatcherInput,
    Document,
    DocumentPdfStatus,
    DocumentVersion,
    DocumentVersionState,
    IndexStatus,
    IngestionQueueMessage,
    IngestionRefusedError,
    IngestionRun,
    ItemEnvelope,
    ItemInput,
    ItemOperation,
    ItemSettings,
    ItemStage,
    ItemState,
    ItemWarning,
    PhysicalIndexState,
    RefusalCode,
    RetrySettings,
    RunItem,
    RunKind,
    RunPriority,
    RunState,
    ScanStatus,
    TraceContext,
    decode_item_warnings,
    encode_item_warnings,
    item_instance_id,
    retryable,
    truncate_tracestate,
)
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.sql.tables import ingestion as tables

RUN_ID = UUID("6c1f3e2a-9b4d-4f0e-8a71-2d5c9e0b7f13")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
EXAMPLE = '{"runId":"6c1f3e2a-9b4d-4f0e-8a71-2d5c9e0b7f13","traceparent":"' + TRACEPARENT + '"}'
"""The example of `80-data-model.md` section 5.2."""


# --- Closed sets ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (IndexStatus, tables.SEARCH_INDEX_STATUSES),
        (PhysicalIndexState, tables.PHYSICAL_INDEX_STATES),
        (DocumentVersionState, tables.DOCUMENT_VERSION_STATES),
        (ScanStatus, tables.SCAN_STATUSES),
        (DocumentPdfStatus, tables.DOCUMENT_PDF_STATUSES),
        (ContentType, tables.CONTENT_TYPES),
        (ChunkSetState, tables.CHUNK_SET_STATES),
        (RunKind, tables.RUN_KINDS),
        (RunState, tables.RUN_STATES),
        (ItemOperation, tables.ITEM_OPERATIONS),
        (ItemState, tables.ITEM_STATES),
        (ItemStage, tables.ITEM_STAGES),
        (CanaryResult, tables.CANARY_RESULTS),
    ],
)
def test_each_enum_is_its_tables_check_constraint(
    enum: type[StrEnum], values: tuple[str, ...]
) -> None:
    assert [member.value for member in enum] == list(values)


def test_derived_sets_follow_the_tables() -> None:
    assert [state.value for state in ACTIVE_VERSION_STATES] == list(tables.ACTIVE_VERSION_STATES)
    assert [state.value for state in LIVE_CHUNK_SET_STATES] == list(tables.LIVE_CHUNK_SET_STATES)
    assert {status.value for status in MAPPABLE_INDEX_STATUSES} == {
        "provisioning",
        "ready",
        "reingest_required",
        "reingesting",
        "rebuilding",
    }
    assert {operation.value for operation in DOCUMENT_OPERATIONS} == {
        "ingest",
        "cleanup_document",
        "rerender",
    }


def test_media_types_come_from_the_three_extensions() -> None:
    assert dict(CONTENT_TYPE_BY_EXTENSION) == {
        ".pdf": ContentType.PDF,
        ".docx": ContentType.DOCX,
        ".pptx": ContentType.PPTX,
    }


def test_priorities_are_interactive_and_bulk() -> None:
    assert int(RunPriority.INTERACTIVE) == 0
    assert int(RunPriority.BULK) == 1
    assert RunPriority.INTERACTIVE.label == "interactive"
    assert RunPriority.BULK.label == "bulk"


# --- Instance IDs ---------------------------------------------------------------------------------


def test_item_instance_ids_carry_the_claim_number() -> None:
    item_id = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
    assert item_instance_id(item_id, 2) == "docchat-item-3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998-2"
    assert DISPATCHER_INSTANCE_ID == "docchat-ingestion-dispatcher"


@pytest.mark.parametrize("claim", [0, -1, True, 1.0])
def test_a_claim_number_is_a_positive_integer(claim: Any) -> None:
    with pytest.raises(ValueError, match="claim number"):
        item_instance_id(uuid4(), claim)


def test_an_instance_id_is_built_from_a_uuid() -> None:
    with pytest.raises(TypeError):
        item_instance_id(str(uuid4()), 1)  # type: ignore[arg-type]


# --- Trace context --------------------------------------------------------------------------------


def test_a_short_tracestate_is_kept() -> None:
    assert truncate_tracestate("congo=t61rcWkgMzE,rojo=00f067aa0ba902b7") == (
        "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7"
    )
    assert truncate_tracestate(None) is None
    assert truncate_tracestate("") is None
    assert truncate_tracestate(" , ") is None
    assert truncate_tracestate(" a=1 , ,b=2") == "a=1,b=2"


def test_long_members_go_first_then_members_from_the_end() -> None:
    long_member = "long=" + "x" * 200
    shorts = [f"k{index}=" + "v" * 40 for index in range(10)]
    value = ",".join([shorts[0], long_member, *shorts[1:]])
    assert len(value) > 512
    cut = truncate_tracestate(value)
    assert cut is not None
    assert "long=" not in cut
    assert len(cut) <= 512
    # Without the long member the header fits, so every short member stays.
    assert cut.split(",") == shorts


def test_members_are_cut_from_the_end_when_long_ones_are_not_enough() -> None:
    shorts = [f"k{index:02d}=" + "v" * 50 for index in range(20)]
    cut = truncate_tracestate(",".join(shorts))
    assert cut is not None
    assert len(cut) <= 512
    kept = cut.split(",")
    assert kept == shorts[: len(kept)]
    assert len(",".join(shorts[: len(kept) + 1])) > 512


def test_a_long_member_survives_when_a_shorter_cut_suffices() -> None:
    long_member = "long=" + "x" * 130
    shorts = [f"k{index}=" + "v" * 40 for index in range(8)]
    value = ",".join([long_member, *shorts])
    assert len(value) <= 512
    assert truncate_tracestate(value) == value


def test_trace_context_checks_its_values() -> None:
    TraceContext(TRACEPARENT)
    TraceContext(TRACEPARENT, "a=1")
    with pytest.raises(ValueError, match="traceparent"):
        TraceContext("01-" + TRACEPARENT[3:])
    with pytest.raises(ValueError, match="tracestate"):
        TraceContext(TRACEPARENT, "")
    with pytest.raises(ValueError, match="tracestate"):
        TraceContext(TRACEPARENT, "a" * 513)


def test_trace_context_from_a_carrier() -> None:
    assert TraceContext.from_carrier({}) is None
    assert TraceContext.from_carrier({"traceparent": "garbage"}) is None
    context = TraceContext.from_carrier(
        {"traceparent": TRACEPARENT, "tracestate": "a=" + "x" * 600 + ",b=1"}
    )
    assert context == TraceContext(TRACEPARENT, "b=1")
    assert TraceContext.from_carrier({"traceparent": TRACEPARENT}) == TraceContext(TRACEPARENT)


def test_trace_context_of_the_current_span() -> None:
    assert TraceContext.current() is None
    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("request") as span:
        context = TraceContext.current()
        assert context is not None
        trace_id = format(span.get_span_context().trace_id, "032x")
        assert context.traceparent.split("-")[1] == trace_id
        assert context.tracestate is None


# --- The queue message ----------------------------------------------------------------------------


def test_t101_the_example_is_accepted_and_encodes_unchanged() -> None:
    message = IngestionQueueMessage.validate_wire(EXAMPLE)
    assert message.run_id == RUN_ID
    assert message.tracestate is None
    assert message.encode() == EXAMPLE


def test_t101_an_empty_tracestate_is_omitted() -> None:
    message = IngestionQueueMessage(run_id=RUN_ID, traceparent=TRACEPARENT, tracestate="")
    assert message.tracestate is None
    assert "tracestate" not in json.loads(message.encode())
    kept = IngestionQueueMessage(run_id=RUN_ID, traceparent=TRACEPARENT, tracestate="a=1")
    assert json.loads(kept.encode()) == {
        "runId": str(RUN_ID),
        "traceparent": TRACEPARENT,
        "tracestate": "a=1",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"runId": str(RUN_ID), "traceparent": TRACEPARENT, "extra": 1},
        {"runId": "not-a-uuid", "traceparent": TRACEPARENT},
        {"runId": str(RUN_ID).upper(), "traceparent": TRACEPARENT},
        {"runId": str(RUN_ID), "traceparent": "00-abc-def-01"},
        {"runId": str(RUN_ID), "traceparent": TRACEPARENT.upper()},
        {"runId": str(RUN_ID), "traceparent": TRACEPARENT, "tracestate": "a" * 513},
        {"run_id": str(RUN_ID), "traceparent": TRACEPARENT},
        {"traceparent": TRACEPARENT},
        {"runId": str(RUN_ID)},
    ],
)
def test_t101_a_malformed_message_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IngestionQueueMessage.validate_wire(json.dumps(body))


def test_t101_a_512_character_tracestate_is_accepted() -> None:
    body = {"runId": str(RUN_ID), "traceparent": TRACEPARENT, "tracestate": "a" * 512}
    assert IngestionQueueMessage.validate_wire(json.dumps(body)).tracestate == "a" * 512


def test_the_message_for_a_run_survives_base64() -> None:
    message = IngestionQueueMessage.for_run(RUN_ID, TraceContext(TRACEPARENT, "a=1"))
    encoded = base64.b64encode(message.encode().encode("utf-8"))
    assert IngestionQueueMessage.validate_wire(base64.b64decode(encoded)) == message
    assert len(encoded) < 1024


# --- Warnings and results -------------------------------------------------------------------------


def test_warnings_hold_warning_codes_only() -> None:
    assert ItemWarning(code="cleanup_incomplete").page is None
    assert ItemWarning(code="table_summary_filtered", page=3).page == 3
    with pytest.raises(ValidationError):
        ItemWarning(code="internal_error")
    with pytest.raises(ValidationError):
        ItemWarning(code="table_summary_filtered", page=0)


def test_warnings_keep_the_first_fifty() -> None:
    warnings = [ItemWarning(code="table_summary_discarded", page=page) for page in range(1, 61)]
    text = encode_item_warnings(warnings)
    assert len(json.loads(text)) == MAX_ITEM_WARNINGS
    assert json.loads(text)[0] == {"code": "table_summary_discarded", "page": 1}
    assert len(text) <= 4000
    assert decode_item_warnings(text) == tuple(warnings[:50])
    assert decode_item_warnings(None) == ()
    assert encode_item_warnings([]) == "[]"


def test_a_success_carries_no_failure_fields() -> None:
    result = ActivityResult.success()
    assert result.model_dump() == {
        "ok": True,
        "errorCode": None,
        "retryable": False,
        "retryAfterSeconds": None,
    }
    with pytest.raises(ValidationError):
        ActivityResult(ok=True, error_code="internal_error")
    with pytest.raises(ValidationError):
        ActivityResult(ok=True, retryable=True)


def test_a_failure_takes_retryable_from_its_code() -> None:
    throttled = ActivityResult.failure("docintel_throttled", retry_after_seconds=30)
    assert throttled.retryable
    assert throttled.retry_after_seconds == 30
    assert not ActivityResult.failure("corrupt_document").retryable
    assert ActivityResult.failure("internal_error").retryable
    lost = ActivityResult.failure(LEASE_LOST)
    assert lost.error_code == LEASE_LOST
    assert not lost.retryable


def test_a_failure_needs_a_failure_code_and_the_catalogue_retry_rule() -> None:
    with pytest.raises(ValidationError):
        ActivityResult(ok=False)
    with pytest.raises(ValidationError):
        ActivityResult(ok=False, error_code="corrupt_document", retryable=True)
    with pytest.raises(ValueError, match="failure code"):
        ActivityResult.failure("already_served")
    with pytest.raises(ValidationError):
        ActivityResult(ok=False, error_code="cleanup_incomplete")
    with pytest.raises(ValidationError):
        ActivityResult.failure("internal_error", retry_after_seconds=-1)


def test_retryable_follows_every_failure_row() -> None:
    for code, entry in ITEM_CODES.items():
        if entry.kind in (ItemCodeKind.FAILURE, ItemCodeKind.FAILURE_AFTER_RETRIES):
            assert retryable(code) is entry.retried
    assert retryable(LEASE_LOST) is False
    with pytest.raises(ValueError, match="failure code"):
        retryable("no_such_code")


def test_an_activity_output_extends_the_result() -> None:
    class ChunkOutput(ActivityResult):
        chunk_count: int | None = None

    output = ChunkOutput.success(chunk_count=4)
    assert output.model_dump(mode="json")["chunkCount"] == 4
    failed = ChunkOutput.failure("no_extractable_text")
    assert failed.chunk_count is None
    assert ChunkOutput.validate_wire(output.model_dump_json()) == output


# --- Orchestration inputs -------------------------------------------------------------------------


def ops_config() -> IngestionOpsConfig:
    values = {
        "ingestion.max_parallel_items": 1,
        "ingestion.active_wake_seconds": 60,
        "ingestion.idle_wake_minutes": 60,
        "ingestion.scan_wait_minutes": 240,
        "ingestion.stale_upload_hours": 24,
        "ingestion.orphan_pending_minutes": 15,
        "ingestion.cleanup_retry_minutes": 15,
        "ingestion.max_item_claims": 3,
        "ingestion.retry_max_attempts": 7,
        "ingestion.retry_first_delay_seconds": 15,
        "ingestion.retry_max_delay_seconds": 480,
        "ingestion.activity_time_budget_seconds": 1200,
        "ingestion.max_pages_per_document": 2000,
        "ingestion.pagewise_pages_per_extract": 40,
        "ingestion.max_parse_seconds_per_document": 7200,
        "ingestion.embedding_batch_max_inputs": 64,
        "ingestion.embedding_batch_max_tokens": 60000,
        "ingestion.embedding_concurrency": 2,
        "ingestion.index_batch_max_documents": 100,
        "ingestion.index_batch_max_bytes": 8000000,
        "ingestion.table_summary_concurrency": 4,
        "ingestion.table_summary_max_input_tokens": 6000,
        "ingestion.table_summary_max_output_tokens": 300,
        "ingestion.run_retention_days": 180,
        "ingestion.durable_history_retention_days": 30,
    }
    return IngestionOpsConfig.model_validate(values)


def claim(**overrides: Any) -> ClaimedItem:
    item_id = uuid4()
    values: dict[str, Any] = {
        "item_id": item_id,
        "claim_number": 2,
        "instance_id": item_instance_id(item_id, 2),
        "operation": ItemOperation.INGEST,
        "priority": RunPriority.INTERACTIVE,
        "run_id": RUN_ID,
        "traceparent": TRACEPARENT,
    }
    values.update(overrides)
    return ClaimedItem(**values)


def test_item_settings_are_the_snapshots_twelve_values() -> None:
    settings = ItemSettings.from_ops_config(ops_config())
    assert settings.model_dump() == {
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
    }
    assert RetrySettings.from_ops_config(ops_config()).model_dump() == {
        "maxAttempts": 7,
        "firstDelaySeconds": 15,
        "maxDelaySeconds": 480,
    }


def test_item_settings_keep_the_ops_config_ranges() -> None:
    for name, field in ItemSettings.model_fields.items():
        source = IngestionOpsConfig.model_fields[name]
        assert field.metadata == source.metadata, name


def test_an_item_input_is_built_from_its_claim() -> None:
    claimed = claim(tracestate="a=1")
    item_input = ItemInput.from_claim(claimed, ops_config())
    dumped = item_input.model_dump(mode="json")
    assert dumped["itemId"] == str(claimed.item_id)
    assert dumped["claimNumber"] == 2
    assert dumped["operation"] == "ingest"
    assert dumped["traceparent"] == TRACEPARENT
    assert dumped["retry"]["maxAttempts"] == 7
    assert ItemInput.validate_wire(item_input.model_dump_json()) == item_input
    envelope = item_input.envelope()
    assert envelope == ItemEnvelope(
        item_id=claimed.item_id,
        claim_number=2,
        run_id=RUN_ID,
        traceparent=TRACEPARENT,
        tracestate="a=1",
        settings=item_input.settings,
    )


def test_a_claim_names_its_own_instance() -> None:
    with pytest.raises(ValidationError):
        claim(instance_id=item_instance_id(uuid4(), 2))
    assert claim(traceparent=None).traceparent is None
    with pytest.raises(ValidationError):
        claim(claim_number=0)


def test_dispatcher_input_carries_the_last_valid_snapshot() -> None:
    first = DispatcherInput(generation=0)
    assert first.model_dump(mode="json") == {"generation": 0, "settings": None}
    later = DispatcherInput(generation=3, settings=ops_config())
    dumped = later.model_dump(mode="json")
    assert dumped["settings"]["ingestion.max_parallel_items"] == 1
    assert DispatcherInput.validate_wire(json.dumps(dumped)) == later
    with pytest.raises(ValidationError):
        DispatcherInput(generation=-1)


# --- Row models -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "table"),
    [
        (IngestionRun, tables.ingestion_runs),
        (RunItem, tables.ingestion_run_items),
        (Document, tables.documents),
        (DocumentVersion, tables.document_versions),
        (ChunkSet, tables.chunk_sets),
    ],
)
def test_each_row_model_has_its_tables_columns(model: type[BaseModel], table: Table) -> None:
    assert list(model.model_fields) == [column.name for column in table.columns]


def test_a_stored_time_reads_as_utc() -> None:
    naive = datetime(2026, 10, 30, 9, 30, 0, 123456)  # noqa: DTZ001 - SQL returns naive UTC
    row = {
        "id": RUN_ID,
        "kind": "upload",
        "search_index_id": uuid4(),
        "priority": 0,
        "state": "queued",
        "requested_by": None,
        "traceparent": TRACEPARENT,
        "tracestate": None,
        "total_items": None,
        "succeeded_items": None,
        "skipped_items": None,
        "failed_items": None,
        "cancelled_items": None,
        "created_at": naive,
        "started_at": datetime(2026, 10, 30, 11, 0, tzinfo=timezone(timedelta(hours=2))),
        "completed_at": None,
    }
    run = IngestionRun.from_row(row)
    assert run.created_at == datetime(2026, 10, 30, 9, 30, 0, 123000, tzinfo=UTC)
    assert run.started_at == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    assert run.kind is RunKind.UPLOAD
    assert run.priority is RunPriority.INTERACTIVE
    with pytest.raises(ValidationError):
        IngestionRun.from_row({**row, "unknown": 1})


def test_a_run_item_reads_its_warnings() -> None:
    now = datetime(2026, 10, 30, tzinfo=UTC)
    item = RunItem.from_row(
        {
            "id": uuid4(),
            "run_id": RUN_ID,
            "queue_seq": 7,
            "operation": "ingest",
            "search_index_id": uuid4(),
            "physical_index_id": uuid4(),
            "document_id": uuid4(),
            "document_version_id": uuid4(),
            "priority": 1,
            "state": "succeeded",
            "stage": "flip",
            "claim_count": 1,
            "instance_id": "docchat-item-x-1",
            "lease_expires_at": None,
            "not_before": None,
            "chunk_set_id": None,
            "outcome_code": None,
            "outcome_message": None,
            "warnings": '[{"code":"cleanup_incomplete","page":null}]',
            "embedding_tokens": 10,
            "summary_input_tokens": None,
            "summary_output_tokens": None,
            "docintel_pages": None,
            "created_at": now,
            "started_at": now,
            "completed_at": now,
        }
    )
    assert item.item_warnings() == (ItemWarning(code="cleanup_incomplete"),)
    assert item.stage is ItemStage.FLIP


# --- Refusals -------------------------------------------------------------------------------------


def test_every_refusal_is_a_registered_problem_code() -> None:
    codes = get_args(RefusalCode.__value__)
    assert len(codes) == 11
    for code in codes:
        entry = CODES[code]
        assert entry.status is not None
        refused = IngestionRefusedError(code)
        assert refused.code == code
        assert str(refused) == code


def test_an_unregistered_refusal_is_refused() -> None:
    with pytest.raises(ValueError, match="registered"):
        IngestionRefusedError("no_such_code")  # type: ignore[arg-type]
