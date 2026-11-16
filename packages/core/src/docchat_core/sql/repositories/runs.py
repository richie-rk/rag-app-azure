"""The ingestion-run reads of the administrator API (`11-api.md` sections 1.8, 3.12 and 4.7;
`80-data-model.md` sections 2.16 and 2.17): the run list, one run with a page of its items, and
the item rows a document's detail shows.

Each statement shape is one constant text per case (with or without each filter and a cursor),
built once at import, never an `:x IS NULL OR …` predicate, so every plan stays a seek: the run
list's page read in a derived table from `IX_ingestion_runs_created` or, filtered by index,
`IX_ingestion_runs_index_created`, both of which include every run column the list selects, so a
page never reads the whole table; the item counts of a page in one grouped query over
`IX_run_items_run`; a run's items on the same index; a document's recent items on
`IX_run_items_document`. Requester and document names come from joins inside the statement, never
from one lookup per row. The functions return rows; the API builds its models from them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Row, Select, and_, bindparam, func, or_, select

from docchat_core.ingestion.models import (
    ItemOperation,
    ItemStage,
    ItemState,
    ItemWarning,
    PhysicalIndexState,
    RunKind,
    RunPriority,
    RunState,
    decode_item_warnings,
)
from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, page_keys, top_rows, utc
from docchat_core.sql.tables.access import users
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
)

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
LIST_RUNS: Final = "admin_list_runs"
READ_RUN: Final = "admin_get_run"

RECENT_ITEMS_MAX: Final = 20
"""A document's detail shows its 20 newest items (`DocumentDetail.recentItems`)."""

# Fixed identifiers for the plan audit: a plan doesn't depend on whether the rows exist.
_EXAMPLE_INDEX: Final = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
_EXAMPLE_RUN: Final = UUID("9c4d5e6f-7a8b-4c9d-8e1f-3a4b5c6d7e63")
_EXAMPLE_PAGE: Final = 51


# --- Rows -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunCounts:
    """A run's items by state, from the grouped count over its items."""

    total: int = 0
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0

    @classmethod
    def of(cls, by_state: Mapping[str, int]) -> RunCounts:
        return cls(
            total=sum(by_state.values()),
            queued=by_state.get(ItemState.QUEUED, 0),
            running=by_state.get(ItemState.RUNNING, 0),
            succeeded=by_state.get(ItemState.SUCCEEDED, 0),
            skipped=by_state.get(ItemState.SKIPPED, 0),
            failed=by_state.get(ItemState.FAILED, 0),
            cancelled=by_state.get(ItemState.CANCELLED, 0),
        )


@dataclass(frozen=True, slots=True)
class RunRow:
    """One run with its index's display name, its requester's display name (`None` for a
    system run or a requester without an `app.users` row) and its item counts."""

    id: UUID
    kind: RunKind
    state: RunState
    index_id: UUID
    index_display_name: str
    priority: RunPriority
    requested_by: UUID | None
    requested_by_name: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    counts: RunCounts


@dataclass(frozen=True, slots=True)
class RunFilters:
    """The run list's optional filters; `index_id` is a filter, never an existence check."""

    index_id: UUID | None = None
    state: RunState | None = None
    kind: RunKind | None = None


@dataclass(frozen=True, slots=True)
class RunPosition:
    """A keyset position in the run list: the last item's creation time and run ID."""

    created_at: datetime
    run_id: UUID


@dataclass(frozen=True, slots=True)
class ItemTarget:
    """The physical index an item targets, as its generation and state."""

    generation: int
    state: PhysicalIndexState


@dataclass(frozen=True, slots=True)
class RunItemRow:
    """One run item with its document's source name, its version's number and its target."""

    id: UUID
    queue_seq: int
    operation: ItemOperation
    document_id: UUID | None
    document_name: str | None
    version_number: int | None
    target: ItemTarget | None
    state: ItemState
    stage: ItemStage | None
    attempts: int
    outcome_code: str | None
    outcome_message: str | None
    warnings: tuple[ItemWarning, ...]
    embedding_tokens: int | None
    summary_input_tokens: int | None
    summary_output_tokens: int | None
    docintel_pages: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunDetailRow:
    """One run and up to `limit_plus_one` of its items in queue order."""

    run: RunRow
    items: tuple[RunItemRow, ...]


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else utc(value)


# --- The run list ---------------------------------------------------------------------------


def _run_columns(r: Any, s: Any, u: Any) -> list[Any]:
    return [
        r.c.id,
        r.c.kind,
        r.c.state,
        r.c.search_index_id,
        s.c.display_name.label("index_display_name"),
        r.c.priority,
        r.c.requested_by,
        u.c.display_name.label("requested_by_name"),
        r.c.created_at,
        r.c.started_at,
        r.c.completed_at,
    ]


def _runs_page(*, index: bool, state: bool, kind: bool, cursor: bool) -> Select[Any]:
    """The page in a derived table, read in order from `IX_ingestion_runs_created`, or
    `IX_ingestion_runs_index_created` with an index filter, which include every run column the
    list selects (`80-data-model.md` section 2.16), so the `TOP` reads the index directly and
    stops at `limit_plus_one` rows (`84-testing.md` section 2.11); then the logical index's and
    the requester's names. The inner join to the logical index drops no run, since every run names
    one (`FK_ingestion_runs_search_indexes`)."""
    k = ingestion_runs.alias("k")
    conditions: list[Any] = []
    if index:
        conditions.append(k.c.search_index_id == bindparam("index_id"))
    if state:
        conditions.append(k.c.state == bindparam("state", type_=ingestion_runs.c.state.type))
    if kind:
        conditions.append(k.c.kind == bindparam("kind", type_=ingestion_runs.c.kind.type))
    if cursor:
        after = bindparam("after_created_at", type_=ingestion_runs.c.created_at.type)
        conditions.append(
            or_(
                k.c.created_at < after,
                and_(k.c.created_at == after, k.c.id < bindparam("after_run_id")),
            )
        )
    r = page_keys(
        select(
            k.c.id,
            k.c.kind,
            k.c.state,
            k.c.search_index_id,
            k.c.priority,
            k.c.requested_by,
            k.c.created_at,
            k.c.started_at,
            k.c.completed_at,
        ).where(*conditions),
        k.c.created_at.desc(),
        k.c.id.desc(),
    ).subquery("r")
    s = search_indexes.alias("s")
    u = users.alias("u")
    return (
        select(*_run_columns(r, s, u))
        .select_from(
            r.join(s, s.c.id == r.c.search_index_id).outerjoin(u, u.c.object_id == r.c.requested_by)
        )
        .order_by(r.c.created_at.desc(), r.c.id.desc())
    )


RUN_PAGES: Final[Mapping[tuple[bool, bool, bool, bool], Select[Any]]] = MappingProxyType(
    {
        (index, state, kind, cursor): _runs_page(index=index, state=state, kind=kind, cursor=cursor)
        for index in (False, True)
        for state in (False, True)
        for kind in (False, True)
        for cursor in (False, True)
    }
)
"""The run list by (index filter, state filter, kind filter, cursor): newest first, then by run
ID descending."""


def _item_counts() -> Select[Any]:
    i = ingestion_run_items.alias("i")
    return (
        select(i.c.run_id, i.c.state, func.count().label("item_count"))
        .where(i.c.run_id.in_(bindparam("run_ids", expanding=True)))
        .group_by(i.c.run_id, i.c.state)
    )


RUN_ITEM_COUNTS: Final = _item_counts()
"""The item counts by state of a page's runs: one grouped query over `IX_run_items_run`."""


def _counts(connection: Connection, run_ids: Sequence[UUID]) -> dict[UUID, RunCounts]:
    if not run_ids:
        return {}
    by_run: dict[UUID, dict[str, int]] = {run_id: {} for run_id in run_ids}
    for row in connection.execute(RUN_ITEM_COUNTS, {"run_ids": list(run_ids)}).all():
        by_run.setdefault(row.run_id, {})[row.state] = int(row.item_count)
    return {run_id: RunCounts.of(states) for run_id, states in by_run.items()}


def _run(row: Row[Any], counts: RunCounts) -> RunRow:
    return RunRow(
        id=row.id,
        kind=RunKind(row.kind),
        state=RunState(row.state),
        index_id=row.search_index_id,
        index_display_name=row.index_display_name,
        priority=RunPriority(row.priority),
        requested_by=row.requested_by,
        requested_by_name=row.requested_by_name,
        created_at=utc(row.created_at),
        started_at=_optional_utc(row.started_at),
        completed_at=_optional_utc(row.completed_at),
        counts=counts,
    )


def _runs_with_counts(connection: Connection, rows: Sequence[Row[Any]]) -> list[RunRow]:
    counts = _counts(connection, [row.id for row in rows])
    return [_run(row, counts.get(row.id, RunCounts())) for row in rows]


@request_path(
    example_params={"filters": RunFilters(), "after": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_runs(
    engine: SqlEngine,
    *,
    filters: RunFilters,
    after: RunPosition | None,
    limit_plus_one: int,
) -> list[RunRow]:
    """Up to `limit_plus_one` runs after `after`, newest first, each with its item counts."""
    parameters: dict[str, object] = {LIMIT_PLUS_ONE: limit_plus_one}
    if filters.index_id is not None:
        parameters["index_id"] = filters.index_id
    if filters.state is not None:
        parameters["state"] = filters.state.value
    if filters.kind is not None:
        parameters["kind"] = filters.kind.value
    if after is not None:
        parameters["after_created_at"] = after.created_at.replace(tzinfo=None)
        parameters["after_run_id"] = after.run_id
    statement = RUN_PAGES[
        (
            filters.index_id is not None,
            filters.state is not None,
            filters.kind is not None,
            after is not None,
        )
    ]

    def work(connection: Connection) -> list[RunRow]:
        return _runs_with_counts(connection, connection.execute(statement, parameters).all())

    return await engine.run(work, name=LIST_RUNS, retry="transient")


# --- One run and its items ------------------------------------------------------------------


def _run_by_id() -> Select[Any]:
    r = ingestion_runs.alias("r")
    s = search_indexes.alias("s")
    u = users.alias("u")
    return (
        select(*_run_columns(r, s, u))
        .select_from(
            r.join(s, s.c.id == r.c.search_index_id).outerjoin(u, u.c.object_id == r.c.requested_by)
        )
        .where(r.c.id == bindparam("run_id"))
    )


RUN_BY_ID: Final = _run_by_id()
"""One run by primary key, with its index's and requester's display names."""


def _item_source(i: Any) -> tuple[Any, list[Any]]:
    d = documents.alias("d")
    v = document_versions.alias("v")
    p = physical_indexes.alias("p")
    source = (
        i.outerjoin(d, d.c.id == i.c.document_id)
        .outerjoin(v, v.c.id == i.c.document_version_id)
        .outerjoin(p, p.c.id == i.c.physical_index_id)
    )
    columns = [
        i.c.id,
        i.c.queue_seq,
        i.c.operation,
        i.c.document_id,
        d.c.source_name.label("document_name"),
        v.c.version_number,
        p.c.generation.label("target_generation"),
        p.c.state.label("target_state"),
        i.c.state,
        i.c.stage,
        i.c.claim_count,
        i.c.outcome_code,
        i.c.outcome_message,
        i.c.warnings,
        i.c.embedding_tokens,
        i.c.summary_input_tokens,
        i.c.summary_output_tokens,
        i.c.docintel_pages,
        i.c.created_at,
        i.c.started_at,
        i.c.completed_at,
    ]
    return source, columns


def _items_page(*, state: bool, cursor: bool) -> Select[Any]:
    i = ingestion_run_items.alias("i")
    source, columns = _item_source(i)
    conditions: list[Any] = [i.c.run_id == bindparam("run_id")]
    if state:
        conditions.append(
            i.c.state == bindparam("item_state", type_=ingestion_run_items.c.state.type)
        )
    if cursor:
        conditions.append(i.c.queue_seq > bindparam("after_queue_seq"))
    return top_rows(select(*columns).select_from(source).where(*conditions).order_by(i.c.queue_seq))


RUN_ITEM_PAGES: Final[Mapping[tuple[bool, bool], Select[Any]]] = MappingProxyType(
    {
        (state, cursor): _items_page(state=state, cursor=cursor)
        for state in (False, True)
        for cursor in (False, True)
    }
)
"""A run's items by (state filter, cursor), in queue order."""


def _document_items() -> Select[Any]:
    i = ingestion_run_items.alias("i")
    source, columns = _item_source(i)
    return (
        select(*columns)
        .select_from(source)
        .where(i.c.document_id == bindparam("document_id"))
        .order_by(i.c.created_at.desc(), i.c.queue_seq.desc())
        .limit(RECENT_ITEMS_MAX)
    )


DOCUMENT_ITEMS: Final = _document_items()
"""A document's 20 newest items, on `IX_run_items_document`."""


def item_row(row: Row[Any]) -> RunItemRow:
    """A `RunItemRow` from a row of `RUN_ITEM_PAGES` or `DOCUMENT_ITEMS`."""
    target = None
    if row.target_generation is not None:
        target = ItemTarget(row.target_generation, PhysicalIndexState(row.target_state))
    return RunItemRow(
        id=row.id,
        queue_seq=int(row.queue_seq),
        operation=ItemOperation(row.operation),
        document_id=row.document_id,
        document_name=row.document_name,
        version_number=row.version_number,
        target=target,
        state=ItemState(row.state),
        stage=None if row.stage is None else ItemStage(row.stage),
        attempts=row.claim_count,
        outcome_code=row.outcome_code,
        outcome_message=row.outcome_message,
        warnings=decode_item_warnings(row.warnings),
        embedding_tokens=row.embedding_tokens,
        summary_input_tokens=row.summary_input_tokens,
        summary_output_tokens=row.summary_output_tokens,
        docintel_pages=row.docintel_pages,
        created_at=utc(row.created_at),
        started_at=_optional_utc(row.started_at),
        completed_at=_optional_utc(row.completed_at),
    )


def read_document_items(connection: Connection, document_id: UUID) -> tuple[RunItemRow, ...]:
    """A document's 20 newest items, on the caller's connection."""
    rows = connection.execute(DOCUMENT_ITEMS, {"document_id": document_id}).all()
    return tuple(item_row(row) for row in rows)


@request_path(
    example_params={
        "run_id": _EXAMPLE_RUN,
        "item_state": None,
        "after_queue_seq": None,
        LIMIT_PLUS_ONE: 101,
    }
)
async def read_run(
    engine: SqlEngine,
    *,
    run_id: UUID,
    item_state: ItemState | None,
    after_queue_seq: int | None,
    limit_plus_one: int,
) -> RunDetailRow | None:
    """The run and up to `limit_plus_one` of its items after `after_queue_seq`; `None` when no
    run has this ID."""
    parameters: dict[str, object] = {"run_id": run_id, LIMIT_PLUS_ONE: limit_plus_one}
    if item_state is not None:
        parameters["item_state"] = item_state.value
    if after_queue_seq is not None:
        parameters["after_queue_seq"] = after_queue_seq
    statement = RUN_ITEM_PAGES[(item_state is not None, after_queue_seq is not None)]

    def work(connection: Connection) -> RunDetailRow | None:
        row = connection.execute(RUN_BY_ID, {"run_id": run_id}).first()
        if row is None:
            return None
        (run,) = _runs_with_counts(connection, [row])
        items = connection.execute(statement, parameters).all()
        return RunDetailRow(run=run, items=tuple(item_row(item) for item in items))

    return await engine.run(work, name=READ_RUN, retry="transient")
