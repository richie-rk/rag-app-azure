"""`delete_search_index` (`14-ingestion.md` section 8.12 item 2): the Azure index deleted (an
absent one counts), then the served rows, the chunk sets and the physical index in one
transaction, and the `physical_index_deleted` event."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from azure.core.exceptions import HttpResponseError, ServiceResponseError

from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import delete_search_index as activity

ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
PHYSICAL = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
NAME = f"docchat-{INDEX.hex}-g1"
SETTINGS = {
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
ENVELOPE = {"itemId": str(ITEM), "claimNumber": 3, "runId": str(RUN), "settings": SETTINGS}


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


def _sql(fakes: SimpleNamespace, *after: Any) -> Any:
    return fakes.FakeSql(
        fakes.Rows(rowcount=1), fakes.Rows([fakes.row(id=PHYSICAL, name=NAME)]), *after
    )


def _http_error(status: int) -> HttpResponseError:
    error = HttpResponseError("refused")
    error.status_code = status
    return error


async def test_the_index_and_its_rows_are_deleted(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.existing[NAME] = object()
    sql = _sql(fakes, fakes.Rows(rowcount=4), fakes.Rows(rowcount=4), fakes.Rows(rowcount=1))

    result = await activity.run(ENVELOPE, _provider(fakes.process(sql=sql, search_indexes=indexes)))

    assert result["ok"] is True
    assert indexes.deleted == [NAME]
    fence, _, served, chunks, physical = sql.connection.executed
    assert fence.params["stage"] == "delete_index"
    assert "DELETE FROM app.served_chunk_sets" in served.sql
    assert "chunks_deleted_at=coalesce(" in chunks.sql
    assert "state IN (N'retiring', N'active', N'building')" in physical.sql
    events = [
        dict(item.log_record.attributes or {})
        for item in telemetry.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name")
        == "physical_index_deleted"
    ]
    assert events == [
        {
            "microsoft.custom_event.name": "physical_index_deleted",
            "physicalIndexId": str(PHYSICAL),
        }
    ]


async def test_an_absent_index_counts_as_deleted_and_a_repeat_emits_nothing(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    sql = _sql(fakes, fakes.Rows(rowcount=0), fakes.Rows(rowcount=0), fakes.Rows(rowcount=0))

    result = await activity.run(ENVELOPE, _provider(fakes.process(sql=sql)))

    assert result["ok"] is True
    assert not any(
        (item.log_record.attributes or {}).get("microsoft.custom_event.name")
        for item in telemetry.logs.get_finished_logs()
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (_http_error(429), "search_unavailable"),
        (_http_error(503), "search_unavailable"),
        (_http_error(500), "search_unavailable"),
        (_http_error(403), "index_delete_failed"),
        (ServiceResponseError("cut"), "search_unavailable"),
    ],
)
async def test_each_refusal_maps_to_its_retryable_code(
    fakes: SimpleNamespace, error: Exception, code: str
) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.delete_failure = error

    result = await activity.run(
        ENVELOPE, _provider(fakes.process(sql=_sql(fakes), search_indexes=indexes))
    )

    assert (result["errorCode"], result["retryable"]) == (code, True)


async def test_a_lost_claim_deletes_nothing(fakes: SimpleNamespace) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.existing[NAME] = object()

    result = await activity.run(
        ENVELOPE,
        _provider(fakes.process(sql=fakes.FakeSql(fakes.Rows(rowcount=0)), search_indexes=indexes)),
    )

    assert result["errorCode"] == "lease_lost"
    assert indexes.deleted == []
