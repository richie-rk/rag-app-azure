"""Unit tests of the ingestion instruments and events (`14-ingestion.md` section 18.4)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from opentelemetry.sdk.metrics.export import MetricsData

from docchat_core.ingestion.models import ItemOperation, ItemStage, ItemState
from docchat_core.telemetry import TestExporters
from docchat_core.telemetry.attributes import METRICS
from docchat_ingestion import telemetry as ingestion_telemetry


def points(exporters: TestExporters) -> dict[str, list[Any]]:
    data = exporters.metrics.get_metrics_data()
    assert isinstance(data, MetricsData)
    found: dict[str, list[Any]] = {}
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def test_every_ingestion_metric_is_declared_here() -> None:
    declared = {
        instrument.name
        for instrument in vars(ingestion_telemetry).values()
        if hasattr(instrument, "dimensions") and hasattr(instrument, "name")
    }
    expected = {name for name in METRICS if name.startswith("docchat.ingestion.")}
    assert declared == expected


def test_the_recorders(telemetry: TestExporters) -> None:
    ingestion_telemetry.record_stage(ItemStage.EMBED, "pagewise", 2.5)
    ingestion_telemetry.record_stage(ItemStage.PURGE_DOCUMENT, None, -1)
    ingestion_telemetry.record_item_completed(ItemOperation.INGEST, ItemState.SUCCEEDED, None, 12.0)
    ingestion_telemetry.record_item_completed(
        ItemOperation.RERENDER, ItemState.FAILED, "render_failed", None
    )
    ingestion_telemetry.record_tokens("ingest-summary", "summary_input", 10)
    ingestion_telemetry.record_tokens("ingest-summary", "summary_output", 0)
    ingestion_telemetry.record_throttled("openai", "embed-ingest-te3-large-v1")
    ingestion_telemetry.record_throttled("search")
    ingestion_telemetry.record_docintel_pages(4)
    ingestion_telemetry.record_docintel_pages(0)
    ingestion_telemetry.record_conversion(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "converted",
        3.0,
        5,
    )
    ingestion_telemetry.record_conversion(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "timeout",
        900.0,
    )
    found = points(telemetry)
    stages = {
        tuple(sorted(point.attributes.items()))
        for point in found["docchat.ingestion.stage.duration"]
    }
    assert (("method_key", "pagewise"), ("stage", "embed")) in stages
    assert (("stage", "purge_document"),) in stages
    completed = {
        point.attributes["outcome_code"] for point in found["docchat.ingestion.items.completed"]
    }
    assert completed == {"none", "render_failed"}
    assert len(found["docchat.ingestion.item.duration"]) == 1
    (tokens,) = found["docchat.ingestion.tokens"]
    assert tokens.value == 10
    assert len(found["docchat.ingestion.throttled"]) == 2
    (pages,) = found["docchat.ingestion.docintel.pages"]
    assert pages.value == 4
    assert len(found["docchat.ingestion.convert.duration"]) == 2
    assert len(found["docchat.ingestion.convert.pages"]) == 1


def test_the_item_failed_event(telemetry: TestExporters) -> None:
    item, run = uuid4(), uuid4()
    ingestion_telemetry.item_failed(
        item_id=item,
        run_id=run,
        operation=ItemOperation.RERENDER,
        stage=ItemStage.RENDER,
        code="render_too_large",
    )
    (record,) = [
        entry
        for entry in telemetry.logs.get_finished_logs()
        if entry.log_record.body == "ingestion_item_failed"
    ]
    attributes = dict(record.log_record.attributes or {})
    assert attributes["itemId"] == str(item)
    assert attributes["operation"] == "rerender"
    assert attributes["outcomeCode"] == "render_too_large"
