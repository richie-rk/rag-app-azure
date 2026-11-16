"""`claim_ingestion_items` on a scripted connection (`14-ingestion.md` section 5.6). The ordering,
gates and slot counting against a real SQL Server are T-150's integration test; here each
statement's shape and the result's mapping are checked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import claim_ingestion_items as activity

ITEM = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


async def test_a_claim_returns_each_item_with_its_run_trace_context(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    sql = fakes.FakeSql(
        fakes.Rows([0]),
        fakes.Rows(
            [
                fakes.row(
                    id=ITEM,
                    claim_count=2,
                    instance_id=f"docchat-item-{ITEM}-2",
                    operation="ingest",
                    run_id=RUN,
                    priority=1,
                )
            ]
        ),
        fakes.Rows(rowcount=1),
        fakes.Rows([fakes.row(id=RUN, traceparent=TRACEPARENT, tracestate="a=1")]),
        fakes.Rows(rowcount=1),
    )

    result = await activity.run(
        {"maxParallel": 2, "generation": 3}, _provider(fakes.process(sql=sql))
    )

    assert result["ok"] is True
    assert result["items"] == [
        {
            "itemId": str(ITEM),
            "claimNumber": 2,
            "instanceId": f"docchat-item-{ITEM}-2",
            "operation": "ingest",
            "priority": 1,
            "runId": str(RUN),
            "traceparent": TRACEPARENT,
            "tracestate": "a=1",
        }
    ]
    executed = sql.connection.executed
    assert "WITH (UPDLOCK, HOLDLOCK)" in executed[0].sql
    claim = executed[1].sql
    assert claim.startswith("UPDATE app.ingestion_run_items SET state=N'running'")
    assert "OUTPUT inserted.id, inserted.claim_count, inserted.instance_id" in claim
    assert "row_number() OVER (PARTITION BY coalesce(i.document_id, i.physical_index_id)" in claim
    assert "ORDER BY candidates.priority, candidates.queue_seq" in claim
    assert "dateadd(minute," in claim
    assert executed[1].params["param_1"] == 2
    assert "app.ingestion_runs SET state=N'running'" in executed[2].sql
    assert "last_claim_at=SYSUTCDATETIME()" in executed[4].sql
    assert sql.units == [("claim_ingestion_items", "transient")]
    (span,) = telemetry.spans.get_finished_spans()
    assert span.name == "docchat.ingestion.dispatcher.claim"
    assert span.parent is None
    attributes = dict(span.attributes or {})
    assert attributes["docchat.ingestion.items_claimed"] == 1
    assert attributes["docchat.ingestion.generation"] == 3
    (link,) = span.links
    assert format(link.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_no_free_slot_claims_nothing_and_writes_nothing(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(fakes.Rows([4]))

    result = await activity.run(
        {"maxParallel": 4, "generation": 0}, _provider(fakes.process(sql=sql))
    )

    assert result["items"] == []
    assert len(sql.connection.executed) == 1


async def test_nothing_claimable_still_records_the_claim_time(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(fakes.Rows([0]), fakes.Rows([]), fakes.Rows(rowcount=1))

    result = await activity.run(
        {"maxParallel": 1, "generation": 0}, _provider(fakes.process(sql=sql))
    )

    assert result["items"] == []
    assert "last_claim_at" in sql.connection.executed[-1].sql


async def test_a_unique_violation_claims_nothing_this_wake(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(
        failures={"claim_ingestion_items": SqlUniqueViolation(2601, "23000", "UX_x")}
    )

    result = await activity.run(
        {"maxParallel": 1, "generation": 0}, _provider(fakes.process(sql=sql))
    )

    assert (result["ok"], result["items"]) == (True, [])


async def test_an_invalid_input_is_internal_error(fakes: SimpleNamespace) -> None:
    result = await activity.run({"maxParallel": 0, "generation": 0}, _provider(fakes.process()))

    assert result["errorCode"] == "internal_error"


async def test_an_item_of_a_run_without_trace_context_has_none(fakes: SimpleNamespace) -> None:
    items = activity.claimed_items(
        [
            activity._Claimed(
                item_id=ITEM,
                claim_number=1,
                instance_id=f"docchat-item-{ITEM}-1",
                operation="purge_index_blobs",
                run_id=RUN,
                priority=1,
            )
        ],
        {},
    )

    assert items[0].traceparent is None
