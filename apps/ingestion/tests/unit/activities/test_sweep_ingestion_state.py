"""`sweep_ingestion_state` (`14-ingestion.md` section 5.3): the duties in order, each failure a
warning naming its duty, the output, the root span and its links, and the poison gauge (T-162).
Each duty's SQL is replaced by a stand-in here; the duties' statements have their own tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from azure.core.exceptions import HttpResponseError

from docchat_core.ingestion.models import ItemOperation, RunKind, RunState
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import (
    finalisation,
    repoint,
    scan_results,
    sweep_duties,
    sweep_ingestion_state,
)

INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
NEW = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
OLD = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
REQUEUED = UUID("8e4f5061-7283-4d94-bea5-c6d7e8f9a0b1")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SWEPT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

DUTIES = (
    "heartbeat",
    "expired_leases",
    "scan_results",
    "stale_uploads",
    "orphaned_versions",
    "leftover_cleanup",
    "repoint",
    "due_deletions",
    "finalisation",
    "counts",
    "poison_gauge",
    "scanner_canaries",
    "stale_renditions",
)


@pytest.fixture
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Every duty's unit of work replaced by a stand-in that records its call."""
    calls: list[str] = []
    found = SimpleNamespace(calls=calls, uploads=[], rebuilding=[], missing=0, finalisable=[])

    def record(name: str, value: Any) -> Any:
        def stand_in(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return value() if callable(value) else value

        return stand_in

    monkeypatch.setattr(sweep_duties, "heartbeat", record("heartbeat", SWEPT))
    monkeypatch.setattr(
        sweep_duties,
        "expire_leases",
        record(
            "expired_leases",
            sweep_duties.ExpiredLeases(
                requeued=(REQUEUED,),
                failed=(
                    sweep_duties.FailedItem(
                        id=ITEM,
                        run_id=RUN,
                        operation=ItemOperation.INGEST,
                        stage="embed",
                    ),
                ),
                run_ids=(RUN,),
            ),
        ),
    )

    async def resolve_versions(process: Any, minutes: int) -> list[UUID]:
        calls.append("scan_results")
        return []

    async def resolve_canaries(process: Any, minutes: int) -> None:
        calls.append("scanner_canaries")

    monkeypatch.setattr(scan_results, "resolve_versions", resolve_versions)
    monkeypatch.setattr(scan_results, "resolve_canaries", resolve_canaries)
    monkeypatch.setattr(
        sweep_duties, "stale_uploads", record("stale_uploads", lambda: found.uploads)
    )
    monkeypatch.setattr(sweep_duties, "remove_stale_upload", record("remove_stale_upload", True))
    monkeypatch.setattr(sweep_duties, "queue_orphaned_versions", record("orphaned_versions", []))
    monkeypatch.setattr(sweep_duties, "queue_leftover_cleanup", record("leftover_cleanup", []))
    monkeypatch.setattr(repoint, "rebuilding_indexes", record("repoint", lambda: found.rebuilding))
    monkeypatch.setattr(
        repoint, "missing_documents", record("repoint_condition", lambda: found.missing)
    )
    monkeypatch.setattr(
        repoint, "repoint", record("repoint_unit", repoint.Repointed(INDEX, OLD, NEW, 2))
    )
    monkeypatch.setattr(sweep_duties, "queue_due_deletions", record("due_deletions", []))
    monkeypatch.setattr(
        finalisation, "finalisable_runs", record("finalisation", lambda: found.finalisable)
    )
    monkeypatch.setattr(
        finalisation,
        "finalise",
        record(
            "finalise",
            finalisation.FinishedRun(
                run_id=RUN,
                kind=RunKind.UPLOAD,
                state=RunState.COMPLETED,
                total=2,
                succeeded=2,
                skipped=0,
                failed=0,
                cancelled=0,
                duration_seconds=12.5,
                traceparent=TRACEPARENT,
            ),
        ),
    )
    monkeypatch.setattr(
        sweep_duties,
        "counts",
        record(
            "counts",
            sweep_duties.Counts(
                queued={"interactive": 1, "bulk": 4},
                running=2,
                oldest_queued_age_seconds=75,
                has_outstanding_work=True,
            ),
        ),
    )
    monkeypatch.setattr(sweep_duties, "queue_stale_renditions", record("stale_renditions", []))
    return found


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


def _payload(fakes: SimpleNamespace, generation: int = 4) -> dict[str, Any]:
    return {"generation": generation, "settings": fakes.settings().model_dump(mode="json")}


def _events(exporters: TestExporters, name: str) -> list[dict[str, Any]]:
    return [
        dict(item.log_record.attributes or {})
        for item in exporters.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name") == name
    ]


def _gauge(exporters: TestExporters, name: str) -> list[float]:
    data: Any = exporters.metrics.get_metrics_data()
    return [
        point.value
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


async def test_a_sweep_runs_every_duty_in_order_and_reports_its_output(
    fakes: SimpleNamespace, stand_ins: SimpleNamespace, telemetry: TestExporters
) -> None:
    stand_ins.finalisable = [finalisation.FinalisableRun(RUN, RunKind.UPLOAD, INDEX, TRACEPARENT)]
    stand_ins.rebuilding = [repoint.RebuildingIndex(INDEX, NEW, 2, RUN)]
    sql = fakes.FakeSql(fakes.Rows([fakes.row(id=RUN, traceparent=TRACEPARENT)]))
    process = fakes.process(sql=sql, poison_queue=fakes.FakeQueue(5), scanning_enabled=True)

    result = await sweep_ingestion_state.run(_payload(fakes), _provider(process))

    assert result == {
        "ok": True,
        "errorCode": None,
        "retryable": False,
        "retryAfterSeconds": None,
        "sweptAt": "2026-01-01T12:00:00Z",
        "requeuedItemIds": [str(REQUEUED)],
        "failedItemIds": [str(ITEM)],
        "hasOutstandingWork": True,
        "queued": {"interactive": 1, "bulk": 4},
        "running": 2,
        "oldestQueuedAgeSeconds": 75,
    }
    assert [call for call in stand_ins.calls if call in DUTIES] == [
        duty for duty in DUTIES if duty != "poison_gauge"
    ]
    assert _events(telemetry, "ingestion_item_failed") == [
        {
            "microsoft.custom_event.name": "ingestion_item_failed",
            "itemId": str(ITEM),
            "runId": str(RUN),
            "operation": "ingest",
            "outcomeCode": "processing_interrupted",
            "stage": "embed",
        }
    ]
    (repointed,) = _events(telemetry, "index_repointed")
    assert repointed["newPhysicalIndexId"] == str(NEW)
    assert repointed["oldPhysicalIndexId"] == str(OLD)
    (finished,) = _events(telemetry, "ingestion_run_finished")
    assert (finished["state"], finished["itemsTotal"], finished["durationSeconds"]) == (
        "completed",
        2,
        12.5,
    )
    assert _gauge(telemetry, "docchat.ingestion.poison_queue.length") == [5]
    (span,) = telemetry.spans.get_finished_spans()
    assert span.name == "docchat.ingestion.dispatcher.sweep"
    assert span.parent is None
    attributes = dict(span.attributes or {})
    assert attributes["docchat.ingestion.generation"] == 4
    assert attributes["docchat.ingestion.items_requeued"] == 1
    assert attributes["docchat.ingestion.runs_finalised"] == 1
    (link,) = span.links
    assert format(link.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(("messages", "recorded"), [(0, 0), (5, 5), (40, 32)])
async def test_t162_the_poison_gauge_records_a_32_message_peek(
    fakes: SimpleNamespace,
    stand_ins: SimpleNamespace,
    telemetry: TestExporters,
    messages: int,
    recorded: int,
) -> None:
    queue = fakes.FakeQueue(messages)

    await sweep_ingestion_state.run(
        _payload(fakes),
        _provider(fakes.process(sql=fakes.FakeSql(fakes.Rows([])), poison_queue=queue)),
    )

    assert queue.peeks == [{"max_messages": 32}]
    assert _gauge(telemetry, "docchat.ingestion.poison_queue.length") == [recorded]


async def test_a_failed_duty_is_a_warning_and_the_next_duty_runs(
    fakes: SimpleNamespace,
    stand_ins: SimpleNamespace,
    telemetry: TestExporters,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("cnry-duty-0a1b2c3d")

    monkeypatch.setattr(sweep_duties, "counts", broken)
    queue = fakes.FakeQueue()
    queue.failure = HttpResponseError("refused")
    process = fakes.process(sql=fakes.FakeSql(fakes.Rows([])), poison_queue=queue)

    result = await sweep_ingestion_state.run(_payload(fakes, generation=0), _provider(process))

    assert result["ok"] is True
    assert result["queued"] is None
    assert result["running"] is None
    assert result["hasOutstandingWork"] is True
    assert "stale_renditions" in stand_ins.calls
    warnings = [
        dict(item.log_record.attributes or {})
        for item in telemetry.logs.get_finished_logs()
        if item.log_record.body == "sweep_duty_failed"
    ]
    assert warnings == [
        {"docchat.ingestion.duty": "counts", "error.type": "builtins.ValueError"},
        {
            "docchat.ingestion.duty": "poison_gauge",
            "error.type": "azure.core.exceptions.HttpResponseError",
        },
    ]
    assert _gauge(telemetry, "docchat.ingestion.poison_queue.length") == []
    (span,) = telemetry.spans.get_finished_spans()
    assert "docchat.ingestion.generation" not in dict(span.attributes or {})
    assert "cnry-" not in span.to_json()


async def test_the_sweep_fails_only_when_every_duty_failed(fakes: SimpleNamespace) -> None:
    queue = fakes.FakeQueue()
    queue.failure = HttpResponseError("refused")

    class Broken:
        async def run(self, work: Any, *, name: str, retry: str) -> Any:
            raise OSError("down")

    process = fakes.process(sql=Broken(), poison_queue=queue, scanning_enabled=True)

    result = await sweep_ingestion_state.run(_payload(fakes), _provider(process))

    assert result["ok"] is False
    assert result["errorCode"] == "internal_error"


async def test_scanner_canaries_wait_for_scanning(
    fakes: SimpleNamespace, stand_ins: SimpleNamespace
) -> None:
    process = fakes.process(sql=fakes.FakeSql(fakes.Rows([])), scanning_enabled=False)

    await sweep_ingestion_state.run(_payload(fakes), _provider(process))

    assert "scanner_canaries" not in stand_ins.calls


async def test_stale_uploads_lose_their_blob_before_their_row(
    fakes: SimpleNamespace, stand_ins: SimpleNamespace
) -> None:
    present = sweep_duties.StaleUpload(ITEM, INDEX, "a/source", SWEPT)
    absent = sweep_duties.StaleUpload(REQUEUED, INDEX, "b/source", SWEPT)
    stand_ins.uploads = [present, absent]
    documents = fakes.FakeContainer("a/source")
    process = fakes.process(sql=fakes.FakeSql(fakes.Rows([])), documents=documents)

    await sweep_ingestion_state.run(_payload(fakes), _provider(process))

    assert documents.deleted == ["a/source"]
    assert stand_ins.calls.count("remove_stale_upload") == 2


async def test_a_stale_upload_whose_blob_delete_fails_keeps_its_row(
    fakes: SimpleNamespace, stand_ins: SimpleNamespace
) -> None:
    stand_ins.uploads = [sweep_duties.StaleUpload(ITEM, INDEX, "a/source", SWEPT)]
    documents = fakes.FakeContainer("a/source")
    documents.delete_failure = HttpResponseError("refused")
    process = fakes.process(sql=fakes.FakeSql(fakes.Rows([])), documents=documents)

    await sweep_ingestion_state.run(_payload(fakes), _provider(process))

    assert "remove_stale_upload" not in stand_ins.calls


async def test_a_rebuilding_index_still_missing_documents_is_not_repointed(
    fakes: SimpleNamespace, stand_ins: SimpleNamespace, telemetry: TestExporters
) -> None:
    stand_ins.rebuilding = [repoint.RebuildingIndex(INDEX, NEW, 2, RUN)]
    stand_ins.missing = 3

    await sweep_ingestion_state.run(
        _payload(fakes), _provider(fakes.process(sql=fakes.FakeSql(fakes.Rows([]))))
    )

    assert "repoint_unit" not in stand_ins.calls
    assert _events(telemetry, "index_repointed") == []


async def test_an_invalid_input_is_internal_error(fakes: SimpleNamespace) -> None:
    result = await sweep_ingestion_state.run({"generation": 1}, _provider(fakes.process()))

    assert result["errorCode"] == "internal_error"
