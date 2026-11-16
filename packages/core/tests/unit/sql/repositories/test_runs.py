"""Unit tests of the ingestion-run reads (`11-api.md` sections 1.8 and 3.12), on a scripted
connection: every statement is compiled for SQL Server and each read is answered from the script
in order. The reads against SQL Server are the run-list and keyset tests of
`apps/api/tests/integration/routes/test_admin_runs.py`."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.dialects import mssql
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.models import (
    ItemOperation,
    ItemStage,
    ItemState,
    ItemWarning,
    PhysicalIndexState,
    RunKind,
    RunPriority,
    RunState,
)
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import runs
from docchat_core.sql.repositories.runs import (
    ItemTarget,
    RunCounts,
    RunFilters,
    RunPosition,
)

DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
RUN = UUID("9c4d5e6f-7a8b-4c9d-8e1f-3a4b5c6d7e63")
ADMIN = UUID("0ca20100-0000-4000-8000-000000000003")
# The driver returns `DATETIME2` values without an offset.
CREATED = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC).replace(tzinfo=None)


class Row:
    """A driver row: attributes and `_mapping`."""

    def __init__(self, **values: Any) -> None:
        self._mapping: dict[str, Any] = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Iterator[Any]:
        return iter(self.rows)


def sql(statement: ClauseElement) -> str:
    return re.sub(r"\s+", " ", str(statement.compile(dialect=DIALECT))).strip()


class Script:
    def __init__(self, *results: Result) -> None:
        self.script = deque(results)
        self.executed: list[tuple[ClauseElement, Any]] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Result:
        sql(statement)
        self.executed.append((statement, parameters))
        assert self.script, f"no scripted answer for {sql(statement)}"
        return self.script.popleft()


@dataclass
class Engine:
    connection: Script
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.calls.append((name, retry))
        return work(self.connection)


def run_row(run_id: UUID = RUN, **overrides: Any) -> Row:
    values: dict[str, Any] = {
        "id": run_id,
        "kind": "upload",
        "state": "running",
        "search_index_id": INDEX,
        "index_display_name": "Policy library",
        "priority": 0,
        "requested_by": ADMIN,
        "requested_by_name": "Carol",
        "created_at": CREATED,
        "started_at": CREATED,
        "completed_at": None,
    }
    return Row(**(values | overrides))


def item_row(**overrides: Any) -> Row:
    values: dict[str, Any] = {
        "id": uuid4(),
        "queue_seq": 7,
        "operation": "ingest",
        "document_id": uuid4(),
        "document_name": "travel-policy.pdf",
        "version_number": 2,
        "target_generation": 1,
        "target_state": "active",
        "state": "failed",
        "stage": "embed",
        "claim_count": 2,
        "outcome_code": "embedding_failed",
        "outcome_message": "The document's passages couldn't be embedded.",
        "warnings": '[{"code":"cleanup_incomplete","page":null}]',
        "embedding_tokens": 1200,
        "summary_input_tokens": None,
        "summary_output_tokens": None,
        "docintel_pages": 3,
        "created_at": CREATED,
        "started_at": CREATED,
        "completed_at": CREATED,
    }
    return Row(**(values | overrides))


# --- Statements -----------------------------------------------------------------------------


def test_every_run_list_shape_is_its_own_constant_text() -> None:
    texts = {sql(statement) for statement in runs.RUN_PAGES.values()}

    assert len(texts) == 16
    assert not any("IS NULL OR" in text for text in texts)
    # The page is read whole from the covering creation-time indexes, with no hint and no join
    # back to the runs table; the names come from the joins around it.
    assert all(
        text.startswith("SELECT r.id")
        and "FROM (SELECT TOP (:limit_plus_one) k.id AS id, k.kind AS kind, k.state AS state, "
        "k.search_index_id AS search_index_id, k.priority AS priority, "
        "k.requested_by AS requested_by, k.created_at AS created_at, "
        "k.started_at AS started_at, k.completed_at AS completed_at "
        "FROM app.ingestion_runs AS k "
        in text
        and "ORDER BY k.created_at DESC , k.id DESC ) AS r "
        "JOIN app.search_indexes AS s ON s.id = r.search_index_id "
        "LEFT OUTER JOIN app.users AS u ON u.object_id = r.requested_by "
        in text
        and text.count("app.ingestion_runs") == 1
        for text in texts
    )
    assert all(text.endswith("ORDER BY r.created_at DESC, r.id DESC") for text in texts)


def test_the_filtered_list_with_a_cursor_names_every_filter() -> None:
    text = sql(runs.RUN_PAGES[(True, True, True, True)])

    assert (
        "WHERE k.search_index_id = :index_id AND k.state = :state AND k.kind = :kind AND "
        "(k.created_at < :after_created_at OR k.created_at = :after_created_at AND "
        "k.id < :after_run_id) ORDER BY"
    ) in text
    assert "LEFT OUTER JOIN app.users AS u ON u.object_id = r.requested_by" in text


def test_the_counts_are_one_grouped_query_over_the_pages_runs() -> None:
    assert sql(runs.RUN_ITEM_COUNTS) == (
        "SELECT i.run_id, i.state, count(*) AS item_count FROM app.ingestion_run_items AS i "
        "WHERE i.run_id IN (__[POSTCOMPILE_run_ids]) GROUP BY i.run_id, i.state"
    )


def test_items_page_in_queue_order_and_a_documents_newest_items_first() -> None:
    page = sql(runs.RUN_ITEM_PAGES[(True, True)])
    recent = sql(runs.DOCUMENT_ITEMS)

    assert page.endswith(
        "WHERE i.run_id = :run_id AND i.state = :item_state AND i.queue_seq > :after_queue_seq "
        "ORDER BY i.queue_seq"
    )
    assert recent.endswith(
        "WHERE i.document_id = :document_id ORDER BY i.created_at DESC, i.queue_seq DESC"
    )
    assert "TOP __[POSTCOMPILE_param_1]" in recent


# --- Reads ----------------------------------------------------------------------------------


def test_counts_sum_the_states() -> None:
    counts = RunCounts.of({"queued": 1, "running": 2, "succeeded": 3, "failed": 4})

    assert counts == RunCounts(total=10, queued=1, running=2, succeeded=3, failed=4)


async def test_read_runs_binds_the_filters_and_adds_each_runs_counts() -> None:
    other = uuid4()
    script = Script(
        Result([run_row(), run_row(other, requested_by=None, requested_by_name=None)]),
        Result(
            [
                Row(run_id=RUN, state="succeeded", item_count=2),
                Row(run_id=RUN, state="queued", item_count=1),
            ]
        ),
    )
    engine = Engine(script)
    after = RunPosition(created_at=CREATED.replace(tzinfo=UTC), run_id=uuid4())

    rows = await runs.read_runs(
        engine,  # type: ignore[arg-type]
        filters=RunFilters(index_id=INDEX, state=RunState.RUNNING, kind=RunKind.UPLOAD),
        after=after,
        limit_plus_one=51,
    )

    assert engine.calls == [(runs.LIST_RUNS, "transient")]
    statement, parameters = script.executed[0]
    assert statement is runs.RUN_PAGES[(True, True, True, True)]
    assert parameters == {
        "limit_plus_one": 51,
        "index_id": INDEX,
        "state": "running",
        "kind": "upload",
        "after_created_at": CREATED,
        "after_run_id": after.run_id,
    }
    assert script.executed[1][1] == {"run_ids": [RUN, other]}
    first, second = rows
    assert first.counts == RunCounts(total=3, queued=1, succeeded=2)
    assert first.priority is RunPriority.INTERACTIVE
    assert first.created_at == CREATED.replace(tzinfo=UTC)
    assert first.completed_at is None
    assert second.counts == RunCounts()
    assert second.requested_by_name is None


async def test_read_runs_without_filters_uses_the_plain_shape_and_skips_empty_counts() -> None:
    script = Script(Result([]))

    rows = await runs.read_runs(
        Engine(script),  # type: ignore[arg-type]
        filters=RunFilters(),
        after=None,
        limit_plus_one=11,
    )

    assert rows == []
    assert script.executed == [
        (runs.RUN_PAGES[(False, False, False, False)], {"limit_plus_one": 11})
    ]


async def test_read_run_is_none_for_an_unknown_run() -> None:
    script = Script(Result([]))

    detail = await runs.read_run(
        Engine(script),  # type: ignore[arg-type]
        run_id=RUN,
        item_state=None,
        after_queue_seq=None,
        limit_plus_one=101,
    )

    assert detail is None
    assert len(script.executed) == 1


async def test_read_run_reads_the_run_its_counts_and_a_page_of_items() -> None:
    index_item = item_row(
        operation="create_physical_index",
        document_id=None,
        document_name=None,
        version_number=None,
        stage=None,
        warnings=None,
        state="succeeded",
        outcome_code=None,
        outcome_message=None,
    )
    script = Script(
        Result([run_row(kind="index_rebuild", priority=1)]),
        Result([Row(run_id=RUN, state="failed", item_count=1)]),
        Result([item_row(), index_item]),
    )
    engine = Engine(script)

    detail = await runs.read_run(
        engine,  # type: ignore[arg-type]
        run_id=RUN,
        item_state=ItemState.FAILED,
        after_queue_seq=6,
        limit_plus_one=3,
    )

    assert detail is not None
    assert engine.calls == [(runs.READ_RUN, "transient")]
    assert detail.run.kind is RunKind.INDEX_REBUILD
    assert detail.run.priority is RunPriority.BULK
    assert detail.run.counts == RunCounts(total=1, failed=1)
    statement, parameters = script.executed[2]
    assert statement is runs.RUN_ITEM_PAGES[(True, True)]
    assert parameters == {
        "run_id": RUN,
        "limit_plus_one": 3,
        "item_state": "failed",
        "after_queue_seq": 6,
    }
    ingest, create = detail.items
    assert ingest.operation is ItemOperation.INGEST
    assert ingest.target == ItemTarget(1, PhysicalIndexState.ACTIVE)
    assert ingest.stage is ItemStage.EMBED
    assert ingest.attempts == 2
    assert ingest.warnings == (ItemWarning(code="cleanup_incomplete", page=None),)
    assert ingest.started_at == CREATED.replace(tzinfo=UTC)
    assert create.document_id is None
    assert create.stage is None
    assert create.warnings == ()


def test_read_document_items_maps_each_row() -> None:
    document = uuid4()
    script = Script(Result([item_row(target_generation=None, target_state=None)]))

    (item,) = runs.read_document_items(script, document)  # type: ignore[arg-type]

    assert script.executed == [(runs.DOCUMENT_ITEMS, {"document_id": document})]
    assert item.target is None


def test_both_reads_are_registered_for_the_plan_audit() -> None:
    registered: Mapping[str, Any] = REQUEST_PATH_REGISTRY
    for name in ("read_runs", "read_run"):
        entry = registered[f"docchat_core.sql.repositories.runs.{name}"]
        assert entry.function is getattr(runs, name)
    assert set(registered["docchat_core.sql.repositories.runs.read_run"].example_params) == {
        "run_id",
        "item_state",
        "after_queue_seq",
        "limit_plus_one",
    }
