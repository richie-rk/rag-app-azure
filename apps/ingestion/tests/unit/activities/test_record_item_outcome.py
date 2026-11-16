"""Unit tests of `record_item_outcome` (`14-ingestion.md` section 6.11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from opentelemetry.sdk.metrics.export import MetricsData

from docchat_core.errors import ITEM_CODES
from docchat_core.sql.engine import SqlUnavailable
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import record_item_outcome

# `DATETIME2(3)` values come back without an offset.
STARTED = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).replace(tzinfo=None)
COMPLETED = datetime(2026, 1, 1, 9, 2, 30, tzinfo=UTC).replace(tzinfo=None)


def returned(
    kit: SimpleNamespace, ids: Any, *, operation: str = "ingest", stage: str = "embed"
) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                run_id=ids.run,
                operation=operation,
                stage=stage,
                started_at=STARTED,
                completed_at=COMPLETED,
            )
        ]
    )


def payload(kit: SimpleNamespace, ids: Any, **fields: Any) -> dict[str, Any]:
    return {
        "itemId": str(ids.item),
        "claimNumber": 1,
        "usage": {
            "embeddingTokens": 100,
            "summaryInputTokens": 20,
            "summaryOutputTokens": 5,
            "docIntelPages": 3,
        },
        **fields,
    }


def metric_points(exporters: TestExporters, name: str) -> list[Any]:
    data = exporters.metrics.get_metrics_data()
    assert isinstance(data, MetricsData)
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


async def test_a_success_is_recorded_under_the_claim(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    world.sql.script(returned(kit, ids))
    output = await record_item_outcome.run(
        {**kit.envelope(ids), **payload(kit, ids, state="succeeded")}, world.provider
    )
    assert output == {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}
    (statement,) = world.sql.connection.executed
    values = statement.values
    assert values["outcome_code"] is None
    assert values["outcome_message"] is None
    assert values["warnings"] is None
    assert values["embedding_tokens"] == 100
    assert values["docintel_pages"] == 3
    assert "stage=app.ingestion_run_items.stage" in statement.sql
    assert "claim_count = :claim_count_1" in statement.sql
    assert "OUTPUT inserted.run_id" in statement.sql
    (completed,) = metric_points(telemetry, "docchat.ingestion.items.completed")
    assert dict(completed.attributes) == {
        "operation": "ingest",
        "state": "succeeded",
        "outcome_code": "none",
    }
    (duration,) = metric_points(telemetry, "docchat.ingestion.item.duration")
    assert duration.sum == 150
    (span,) = [
        span
        for span in telemetry.spans.get_finished_spans()
        if span.name == "docchat.ingestion.activity"
    ]
    attributes = dict(span.attributes or {})
    assert attributes["docchat.activity_name"] == "record_item_outcome"
    assert attributes["docchat.outcome"] == "ok"
    assert span.parent is not None
    assert format(span.context.trace_id, "032x") == kit.TRACEPARENT.split("-")[1]


async def test_a_failure_applies_its_side_effects_and_emits_the_event(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    world.sql.script(returned(kit, ids), kit.Result(rowcount=1), kit.Result(rowcount=1))
    warnings = [{"code": "table_summary_filtered", "page": page} for page in range(1, 60)]
    output = await record_item_outcome.run(
        payload(
            kit, ids, state="failed", code="embedding_failed", stage="embed", warnings=warnings
        ),
        world.provider,
    )
    assert output["ok"] is True
    item, chunk_set, version = world.sql.connection.executed
    assert item.values["outcome_message"] == ITEM_CODES["embedding_failed"].message
    assert item.values["stage"] == "embed"
    assert len(json.loads(item.values["warnings"])) == 50
    assert chunk_set.sql.startswith("UPDATE app.chunk_sets SET state=N'retired'")
    assert "N'building', N'ready'" in chunk_set.sql
    assert version.sql.startswith("UPDATE app.document_versions SET state=N'failed'")
    assert "active_physical_index_id IS NULL" in version.sql
    assert "N'pending', N'ingesting'" in version.sql
    events = [
        record
        for record in telemetry.logs.get_finished_logs()
        if record.log_record.body == "ingestion_item_failed"
    ]
    assert len(events) == 1


async def test_a_skip_stores_its_message(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(returned(kit, ids, stage="validate"))
    output = await record_item_outcome.run(
        payload(kit, ids, state="skipped", code="already_served"), world.provider
    )
    assert output["ok"] is True
    (statement,) = world.sql.connection.executed
    assert statement.values["outcome_message"] == ITEM_CODES["already_served"].message


async def test_a_lost_claim_writes_nothing_more(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.Result(rows=[]))
    output = await record_item_outcome.run(
        payload(kit, ids, state="failed", code="internal_error"), world.provider
    )
    assert output["errorCode"] == "lease_lost"
    assert len(world.sql.connection.executed) == 1


async def test_sql_out_of_reach(kit: SimpleNamespace, world: Any) -> None:
    world.sql.failures.append(SqlUnavailable("exhausted"))
    output = await record_item_outcome.run(
        payload(kit, kit.Ids(), state="succeeded"), world.provider
    )
    assert output["errorCode"] == "database_unavailable"


async def test_codes_must_match_the_state(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    for fields in (
        {"state": "succeeded", "code": "already_served"},
        {"state": "skipped", "code": "embedding_failed"},
        {"state": "failed", "code": "already_served"},
    ):
        output = await record_item_outcome.run(payload(kit, ids, **fields), world.provider)
        assert output["errorCode"] == "internal_error"
