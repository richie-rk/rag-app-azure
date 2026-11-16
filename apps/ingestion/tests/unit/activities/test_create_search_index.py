"""`create_search_index` (`14-ingestion.md` section 8.12 item 1): the fenced lease, the index built
from the physical index row, an existing equal index counted as created, the activation while the
logical index has no active index, and each failure's code."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from docchat_core.search.schema import build_chunk_index
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import create_search_index as activity

ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
PHYSICAL = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
NAME = f"docchat-{INDEX.hex}-g1"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def envelope(traceparent: str | None = TRACEPARENT) -> dict[str, Any]:
    return {
        "itemId": str(ITEM),
        "claimNumber": 1,
        "runId": str(RUN),
        "traceparent": traceparent,
        "tracestate": None,
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


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


def _target_row(fakes: SimpleNamespace) -> Any:
    return fakes.row(
        search_index_id=INDEX,
        id=PHYSICAL,
        name=NAME,
        embedding_dimensions=1024,
        language_analyzer="standard.lucene",
    )


def _sql(fakes: SimpleNamespace, *after: Any) -> Any:
    return fakes.FakeSql(fakes.Rows(rowcount=1), fakes.Rows([_target_row(fakes)]), *after)


def _http_error(
    status: int, message: str = "refused", headers: dict[str, str] | None = None
) -> HttpResponseError:
    error = HttpResponseError(message)
    error.status_code = status
    error.response = SimpleNamespace(headers=headers or {})
    return error


async def test_the_first_physical_index_is_created_and_activated(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    indexes = fakes.FakeSearchIndexes()
    sql = _sql(
        fakes,
        fakes.Rows(rowcount=1),
        fakes.Rows([None]),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
    )

    result = await activity.run(
        envelope(), _provider(fakes.process(sql=sql, search_indexes=indexes))
    )

    assert result == {
        "ok": True,
        "errorCode": None,
        "retryable": False,
        "retryAfterSeconds": None,
        "activated": True,
    }
    (created,) = indexes.created
    assert created.name == NAME
    fence = sql.connection.executed[0]
    assert fence.params["stage"] == "create_index"
    assert fence.params["dateadd_1"] == 900
    sql_text = [item.sql for item in sql.connection.executed]
    assert "SET provisioned_at=SYSUTCDATETIME()" in sql_text[2]
    assert "WITH (UPDLOCK, ROWLOCK)" in sql_text[3]
    assert "status=N'ready'" in sql_text[4]
    assert "SET state=N'active'" in sql_text[5]
    (span,) = telemetry.spans.get_finished_spans()
    attributes = dict(span.attributes or {})
    assert span.name == "docchat.ingestion.activity"
    assert format(span.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert attributes["docchat.activity_name"] == "create_search_index"
    assert attributes["docchat.physical_index_id"] == str(PHYSICAL)
    assert attributes["docchat.outcome"] == "ok"


async def test_a_rebuild_index_is_provisioned_but_not_activated(fakes: SimpleNamespace) -> None:
    sql = _sql(fakes, fakes.Rows(rowcount=1), fakes.Rows([UUID(int=1)]))

    result = await activity.run(envelope(None), _provider(fakes.process(sql=sql)))

    assert result["activated"] is False


async def test_an_existing_equal_index_counts_as_created(fakes: SimpleNamespace) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.existing[NAME] = build_chunk_index(NAME, 1024, "standard.lucene")
    sql = _sql(fakes, fakes.Rows(rowcount=0), fakes.Rows([UUID(int=1)]))

    result = await activity.run(
        envelope(), _provider(fakes.process(sql=sql, search_indexes=indexes))
    )

    assert result["ok"] is True


async def test_an_existing_index_with_another_schema_is_a_conflict(fakes: SimpleNamespace) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.existing[NAME] = build_chunk_index(NAME, 3072, "standard.lucene")

    result = await activity.run(
        envelope(), _provider(fakes.process(sql=_sql(fakes), search_indexes=indexes))
    )

    assert (result["ok"], result["errorCode"], result["retryable"]) == (
        False,
        "index_schema_conflict",
        False,
    )


@pytest.mark.parametrize(
    ("error", "code", "retry_after"),
    [
        (_http_error(429, headers={"retry-after-ms": "1500"}), "search_unavailable", 2),
        (_http_error(503, headers={"Retry-After": "30"}), "search_unavailable", None),
        (_http_error(502), "search_unavailable", None),
        (_http_error(400), "index_create_failed", None),
        (_http_error(403), "search_index_capacity", None),
        (_http_error(400, "Index quota exceeded"), "search_index_capacity", None),
        (ServiceRequestError("unreachable"), "search_unavailable", None),
    ],
)
async def test_each_refusal_maps_to_its_code(
    fakes: SimpleNamespace, error: Exception, code: str, retry_after: int | None
) -> None:
    indexes = fakes.FakeSearchIndexes()
    indexes.create_failure = error

    result = await activity.run(
        envelope(), _provider(fakes.process(sql=_sql(fakes), search_indexes=indexes))
    )

    assert result["errorCode"] == code
    assert result["retryAfterSeconds"] == retry_after


async def test_a_lost_claim_creates_nothing(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    indexes = fakes.FakeSearchIndexes()
    sql = fakes.FakeSql(fakes.Rows(rowcount=0))

    result = await activity.run(
        envelope(), _provider(fakes.process(sql=sql, search_indexes=indexes))
    )

    assert (result["ok"], result["errorCode"], result["retryable"]) == (False, "lease_lost", False)
    assert indexes.created == []
    (span,) = telemetry.spans.get_finished_spans()
    assert "docchat.error_code" not in dict(span.attributes or {})


async def test_an_envelope_that_fails_validation_is_internal_error(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    result = await activity.run({"itemId": "cnry-input-0a1b2c3d"}, _provider(fakes.process()))

    assert result["errorCode"] == "internal_error"
    assert "cnry-" not in str(
        [item.log_record.attributes for item in telemetry.logs.get_finished_logs()]
    )


def test_the_comparison_reads_sub_fields() -> None:
    built = build_chunk_index(NAME, 1024, "standard.lucene")
    assert activity.same_schema(built, build_chunk_index(NAME, 1024, "standard.lucene"))
    assert not activity.same_schema(built, build_chunk_index(NAME, 1024, "en.microsoft"))
    assert not activity.same_schema(SimpleNamespace(fields=None), built)
