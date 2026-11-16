"""Run and item creation shared by the API and the operator commands, the failure side effects
of an item shared by the ingestion app and the operator commands, and the logical index status
rule (`14-ingestion.md` sections 2.6, 2.8, 6.11, 7, 8.1 to 8.7 and 8.11; `80-data-model.md`
sections 2.10 to 2.17 and 8; ADRs 0034, 0040, 0041, 0045 and 0052).

Each function is the SQL of one ledger operation, written as SQLAlchemy Core statements over
`docchat_core.sql.tables`, and runs on the caller's connection inside the caller's unit of work
(`SqlEngine.run(work, name=..., retry="none")`): the caller writes its audit row in the same
transaction and, after the commit, sends the one `IngestionQueueMessage` (the API; operators hold
no queue-send right). A refusal raises `IngestionRefusedError` with its problem code, which rolls
the whole transaction back, so nothing is half-done. Run creation draws random IDs, so a unit of
work that creates a run is never re-run as a whole on a transient error.

State values that filtered indexes cover stay literals in the statement text (`N'queued'`), so
SQL Server matches the filtered index; every value from a request is a bound parameter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import Connection, and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.sql.elements import ColumnClause

from docchat_core.errors import ITEM_CODES
from docchat_core.ingestion.methods import MethodDescriptor, MethodParameters, compact_json
from docchat_core.ingestion.models import (
    ACTIVE_VERSION_STATES,
    LIVE_CHUNK_SET_STATES,
    SETTINGS_CHANGE_STATUSES,
    ChunkSetState,
    DocumentVersionState,
    IndexStatus,
    IngestionRefusedError,
    ItemOperation,
    ItemState,
    PhysicalIndexState,
    ReingestMode,
    RunKind,
    RunPriority,
    SubmitOutcome,
    TraceContext,
)
from docchat_core.ingestion.names import physical_index_name
from docchat_core.ingestion.settings import (
    UPLOAD_MAX_FILES_PER_BATCH_LIMIT,
    EmbeddingModel,
    EmbeddingModelCatalog,
)
from docchat_core.search.schema import ANALYZERS, CHUNK_INDEX_SCHEMA_VERSION
from docchat_core.sql.tables.access import project_indexes
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import BIT_FALSE, BIT_TRUE, SYSUTCDATETIME, nvarchar_literal

LOCK_ROW: Final = "WITH (UPDLOCK, ROWLOCK)"
"""The hint of every read that serialises a decision on a row (READ COMMITTED with RCSI)."""

RUN_KIND_PRIORITY: Final[Mapping[RunKind, RunPriority]] = MappingProxyType(
    {
        RunKind.UPLOAD: RunPriority.INTERACTIVE,
        RunKind.DOCUMENT_REINGEST: RunPriority.INTERACTIVE,
        RunKind.DOCUMENT_DELETE: RunPriority.INTERACTIVE,
        RunKind.INDEX_CREATE: RunPriority.INTERACTIVE,
        RunKind.INDEX_REINGEST: RunPriority.BULK,
        RunKind.INDEX_REBUILD: RunPriority.BULK,
        RunKind.INDEX_DELETE: RunPriority.BULK,
        RunKind.MAINTENANCE: RunPriority.INTERACTIVE,
    }
)
"""Each kind's priority (`80-data-model.md` section 2.16); a `maintenance` run of sweep duty 12,
which holds only `rerender` items, is bulk instead."""


def run_priority(kind: RunKind, *, rerender: bool = False) -> RunPriority:
    """A run's priority from its kind; `rerender` marks a `maintenance` run of `rerender` items."""
    if rerender:
        if kind is not RunKind.MAINTENANCE:
            raise ValueError("only a maintenance run holds rerender items")
        return RunPriority.BULK
    return RUN_KIND_PRIORITY[kind]


def _literal(value: str) -> ColumnClause[str]:
    return nvarchar_literal(value)


def _literals(values: Sequence[str]) -> list[ColumnClause[str]]:
    return [nvarchar_literal(value) for value in values]


_OPEN_ITEM_STATES: Final = (ItemState.QUEUED, ItemState.RUNNING)


# --- Items ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewItem:
    """An item to insert, with exactly the targets its operation carries (`CK_run_items_shape`)."""

    operation: ItemOperation
    physical_index_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_set_id: UUID | None = None
    not_before: datetime | None = None

    def __post_init__(self) -> None:
        if not _shape_holds(self):
            raise ValueError(f"an {self.operation} item doesn't carry its operation's targets")


def _shape_holds(item: NewItem) -> bool:
    document = item.document_id is not None
    version = item.document_version_id is not None
    physical = item.physical_index_id is not None
    match ItemOperation(item.operation):
        case ItemOperation.INGEST:
            return document and version and physical
        case ItemOperation.CLEANUP_DOCUMENT:
            return document and not version
        case ItemOperation.RERENDER:
            return document and version and physical and item.chunk_set_id is not None
        case ItemOperation.CREATE_PHYSICAL_INDEX | ItemOperation.DELETE_PHYSICAL_INDEX:
            return physical and not document
        case ItemOperation.PURGE_INDEX_BLOBS:
            return not document and not physical


@dataclass(frozen=True, slots=True)
class CreatedRun:
    """A run and its items, in the order they were inserted (`queue_seq` order)."""

    run_id: UUID
    item_ids: tuple[UUID, ...]


def _insert_run(
    connection: Connection,
    *,
    kind: RunKind,
    index_id: UUID,
    priority: RunPriority,
    requested_by: UUID | None,
    trace: TraceContext | None,
) -> UUID:
    run_id = uuid4()
    connection.execute(
        insert(ingestion_runs).values(
            id=run_id,
            kind=kind.value,
            search_index_id=index_id,
            priority=int(priority),
            state="queued",
            requested_by=requested_by,
            traceparent=None if trace is None else trace.traceparent,
            tracestate=None if trace is None else trace.tracestate,
        )
    )
    return run_id


def _insert_items(
    connection: Connection,
    *,
    run_id: UUID,
    index_id: UUID,
    priority: RunPriority,
    items: Sequence[NewItem],
) -> tuple[UUID, ...]:
    if not items:
        return ()
    item_ids = tuple(uuid4() for _ in items)
    rows = [
        {
            "id": item_id,
            "run_id": run_id,
            "operation": ItemOperation(item.operation).value,
            "search_index_id": index_id,
            "physical_index_id": item.physical_index_id,
            "document_id": item.document_id,
            "document_version_id": item.document_version_id,
            "chunk_set_id": item.chunk_set_id,
            "priority": int(priority),
            "not_before": item.not_before,
        }
        for item_id, item in zip(item_ids, items, strict=True)
    ]
    # The driver runs the rows in list order, so `queue_seq` follows the list.
    connection.execute(insert(ingestion_run_items), rows)
    return item_ids


def _create_run(
    connection: Connection,
    *,
    kind: RunKind,
    index_id: UUID,
    items: Sequence[NewItem],
    requested_by: UUID | None,
    trace: TraceContext | None,
    priority: RunPriority | None = None,
) -> CreatedRun:
    run_priority_ = run_priority(kind) if priority is None else priority
    run_id = _insert_run(
        connection,
        kind=kind,
        index_id=index_id,
        priority=run_priority_,
        requested_by=requested_by,
        trace=trace,
    )
    item_ids = _insert_items(
        connection, run_id=run_id, index_id=index_id, priority=run_priority_, items=items
    )
    return CreatedRun(run_id, item_ids)


def create_maintenance_run(
    connection: Connection, *, index_id: UUID, items: Sequence[NewItem]
) -> CreatedRun:
    """A `maintenance` run of one logical index, with no requester and no trace context: the
    sweep's runs (orphaned versions, leftover clean-up, due deletions, stale renditions) and
    `docchat-ops ingestion abandon-rebuild`'s delete item (section 2.8). A run of `rerender`
    items is bulk and holds nothing else; every other maintenance run is interactive."""
    if not items:
        raise ValueError("a maintenance run is created with its items")
    rerender = [ItemOperation(item.operation) is ItemOperation.RERENDER for item in items]
    if any(rerender) and not all(rerender):
        raise ValueError("a run of rerender items holds nothing else")
    return _create_run(
        connection,
        kind=RunKind.MAINTENANCE,
        index_id=index_id,
        items=items,
        requested_by=None,
        trace=None,
        priority=run_priority(RunKind.MAINTENANCE, rerender=all(rerender)),
    )


# --- Shared reads ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LogicalIndex:
    status: IndexStatus
    row_version: bytes
    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str
    active_physical_index_id: UUID | None

    def check_row_version(self, row_version: bytes) -> None:
        if self.row_version != row_version:
            raise IngestionRefusedError("precondition_failed")


def _index_row(connection: Connection, index_id: UUID, *, lock: bool) -> _LogicalIndex:
    """The logical index's row; `index_not_found` when it is missing or `deleted`."""
    statement = select(
        search_indexes.c.status,
        search_indexes.c.row_version,
        search_indexes.c.embedding_model,
        search_indexes.c.embedding_dimensions,
        search_indexes.c.language_analyzer,
        search_indexes.c.active_physical_index_id,
    ).where(search_indexes.c.id == index_id)
    if lock:
        statement = statement.with_hint(search_indexes, LOCK_ROW, "mssql")
    row = connection.execute(statement).first()
    if row is None or row.status == IndexStatus.DELETED:
        raise IngestionRefusedError("index_not_found")
    return _LogicalIndex(
        status=IndexStatus(row.status),
        row_version=bytes(row.row_version),
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
        active_physical_index_id=row.active_physical_index_id,
    )


def _document_refusal(
    connection: Connection, index_id: UUID, document_id: UUID
) -> IngestionRefusedError:
    """Why a guarded document update changed nothing: deleted, absent or of another index is
    `404 document_not_found`; live with another row version is `412 precondition_failed`."""
    row = connection.execute(
        select(documents.c.is_deleted).where(
            documents.c.id == document_id, documents.c.search_index_id == index_id
        )
    ).first()
    if row is None or row.is_deleted:
        return IngestionRefusedError("document_not_found")
    return IngestionRefusedError("precondition_failed")


def _building_index_id(
    connection: Connection, index_id: UUID, *, lock: bool = False
) -> UUID | None:
    statement = select(physical_indexes.c.id).where(
        physical_indexes.c.search_index_id == index_id,
        physical_indexes.c.state == _literal(PhysicalIndexState.BUILDING),
    )
    if lock:
        statement = statement.with_hint(physical_indexes, LOCK_ROW, "mssql")
    found: UUID | None = connection.execute(statement).scalar_one_or_none()
    return found


def has_capacity(physical_index_count: int, max_indexes: int, *, keep_free: bool) -> bool:
    """Room for one more physical index on the service: the non-deleted physical indexes plus 1
    may not exceed `DOCCHAT_SEARCH_MAX_INDEXES`, less the one slot an index creation keeps free
    for a rebuild (sections 8.3 and 8.7)."""
    limit = max_indexes - 1 if keep_free else max_indexes
    return physical_index_count + 1 <= limit


def _check_capacity(connection: Connection, max_indexes: int, *, keep_free: bool) -> None:
    count = connection.execute(
        select(func.count())
        .select_from(physical_indexes)
        .where(physical_indexes.c.state != _literal(PhysicalIndexState.DELETED))
    ).scalar_one()
    if not has_capacity(count, max_indexes, keep_free=keep_free):
        raise IngestionRefusedError("search_index_capacity")


def _live_current_versions(
    connection: Connection, index_id: UUID, target_physical_index_id: UUID
) -> list[NewItem]:
    """One `ingest` item per live document's current version into the target, leaving out
    versions that already have a queued or running item for that target (section 8.6)."""
    # Every lookup of a version's items also names its document: an `ingest` item always
    # carries both (`CK_run_items_shape`), and `IX_run_items_document` is the index that seeks.
    covered = exists().where(
        ingestion_run_items.c.document_id == documents.c.id,
        ingestion_run_items.c.document_version_id == documents.c.current_version_id,
        ingestion_run_items.c.physical_index_id == target_physical_index_id,
        ingestion_run_items.c.operation == _literal(ItemOperation.INGEST),
        ingestion_run_items.c.state.in_(_literals(_OPEN_ITEM_STATES)),
    )
    rows = connection.execute(
        select(documents.c.id, documents.c.current_version_id)
        .where(
            documents.c.search_index_id == index_id,
            documents.c.is_deleted == BIT_FALSE,
            documents.c.current_version_id.is_not(None),
            ~covered,
        )
        .order_by(documents.c.id)
    ).all()
    return [
        NewItem(
            ItemOperation.INGEST,
            physical_index_id=target_physical_index_id,
            document_id=row.id,
            document_version_id=row.current_version_id,
        )
        for row in rows
    ]


def _physical_index_values(
    *,
    physical_index_id: UUID,
    index_id: UUID,
    generation: int,
    embedding: EmbeddingModel,
    dimensions: int,
    analyzer: str,
    build_run_id: UUID,
) -> dict[str, Any]:
    # The record copies the catalogue entry, so a later change of the setting never alters
    # an existing index (section 8.2 item 3).
    return {
        "id": physical_index_id,
        "search_index_id": index_id,
        "generation": generation,
        "name": physical_index_name(index_id, generation),
        "schema_version": CHUNK_INDEX_SCHEMA_VERSION,
        "embedding_model": embedding.model,
        "embedding_model_version": embedding.model_version,
        "embedding_dimensions": dimensions,
        "embedding_encoding": embedding.encoding.value,
        "embedding_max_input_tokens": embedding.max_input_tokens,
        "language_analyzer": analyzer,
        "query_embedding_deployment": embedding.query_deployment,
        "ingestion_embedding_deployment": embedding.ingestion_deployment,
        "state": PhysicalIndexState.BUILDING.value,
        "build_run_id": build_run_id,
    }


def _cancel_message(code: str) -> str:
    return ITEM_CODES[code].message


# --- Upload ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmitResult:
    version_id: UUID
    outcome: SubmitOutcome


@dataclass(frozen=True, slots=True)
class UploadRunResult:
    """`adminSubmitUploads`' outcome: the run (`None` when nothing was queued) and each
    requested version's outcome in request order."""

    run_id: UUID | None
    results: tuple[SubmitResult, ...]

    @property
    def item_count(self) -> int:
        return sum(1 for result in self.results if result.outcome is SubmitOutcome.QUEUED)

    @property
    def not_queued_count(self) -> int:
        return len(self.results) - self.item_count


def classify_submissions(
    version_ids: Sequence[UUID], states: Mapping[UUID, str], already_queued: set[UUID]
) -> tuple[SubmitResult, ...]:
    """Each requested version's outcome (section 2.6 step 2.3): `not_found` when it isn't a
    version of this index, `not_pending` when its state isn't `pending`, `already_queued` when an
    `ingest` item is queued or running for it, else `queued`."""
    results: list[SubmitResult] = []
    for version_id in version_ids:
        state = states.get(version_id)
        if state is None:
            outcome = SubmitOutcome.NOT_FOUND
        elif state != DocumentVersionState.PENDING:
            outcome = SubmitOutcome.NOT_PENDING
        elif version_id in already_queued:
            outcome = SubmitOutcome.ALREADY_QUEUED
        else:
            outcome = SubmitOutcome.QUEUED
        results.append(SubmitResult(version_id, outcome))
    return tuple(results)


def _upload_target(connection: Connection, index_id: UUID) -> UUID:
    active = connection.execute(
        select(search_indexes.c.active_physical_index_id).where(search_indexes.c.id == index_id)
    ).scalar_one_or_none()
    if active is not None:
        target: UUID = active
        return target
    building = _building_index_id(connection, index_id)
    if building is None:
        raise IngestionRefusedError("index_not_accepting_uploads")
    return building


def create_upload_run(
    connection: Connection,
    *,
    index_id: UUID,
    version_ids: Sequence[UUID],
    requested_by: UUID,
    trace: TraceContext | None,
) -> UploadRunResult:
    """`adminSubmitUploads`' transaction (section 2.6 step 2): lock the listed versions of the
    index, classify each, and when at least one is `queued` create one `upload` run with one
    `ingest` item per queued version into the active physical index (or, with none, the building
    one, which becomes active once provisioned). The caller has checked the index, its status and
    the scan gate (section 2.1) and writes `ingestion.run_submitted` when a run was created."""
    if not 1 <= len(version_ids) <= UPLOAD_MAX_FILES_PER_BATCH_LIMIT:
        raise ValueError("a submit names 1 to 100 versions")
    if len(set(version_ids)) != len(version_ids):
        raise ValueError("a submit names each version once")
    found = connection.execute(
        select(document_versions.c.id, document_versions.c.document_id, document_versions.c.state)
        .with_hint(document_versions, LOCK_ROW, "mssql")
        .where(
            document_versions.c.search_index_id == index_id,
            document_versions.c.id.in_(list(version_ids)),
        )
    ).all()
    queued_already = set(
        connection.execute(
            select(ingestion_run_items.c.document_version_id).where(
                ingestion_run_items.c.document_id.in_(sorted({row.document_id for row in found})),
                ingestion_run_items.c.document_version_id.in_(list(version_ids)),
                ingestion_run_items.c.operation == _literal(ItemOperation.INGEST),
                ingestion_run_items.c.state.in_(_literals(_OPEN_ITEM_STATES)),
            )
        ).scalars()
    )
    results = classify_submissions(
        version_ids, {row.id: row.state for row in found}, queued_already
    )
    queued = [result.version_id for result in results if result.outcome is SubmitOutcome.QUEUED]
    if not queued:
        return UploadRunResult(run_id=None, results=results)
    target = _upload_target(connection, index_id)
    document_of = {row.id: row.document_id for row in found}
    created = _create_run(
        connection,
        kind=RunKind.UPLOAD,
        index_id=index_id,
        items=[
            NewItem(
                ItemOperation.INGEST,
                physical_index_id=target,
                document_id=document_of[version_id],
                document_version_id=version_id,
            )
            for version_id in queued
        ],
        requested_by=requested_by,
        trace=trace,
    )
    return UploadRunResult(run_id=created.run_id, results=results)


# --- Documents ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentReingestResult:
    """`adminReingestDocument`'s outcome: the new run, or the newest open run that already holds
    an item for the current version and target (`created` false, `item_count` 0)."""

    run_id: UUID
    created: bool
    item_count: int


def create_document_reingest_run(
    connection: Connection,
    *,
    index_id: UUID,
    document_id: UUID,
    row_version: bytes,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentReingestResult:
    """`adminReingestDocument`'s transaction (section 8.11): the guarded document update (which
    moves its row version), then one `document_reingest` run with one `ingest` item for the
    current version into the building physical index when one exists, else the active one. The
    answers come in the order `404`, `412`, then `409` (`no_current_version`, `index_busy`)."""
    index = _index_row(connection, index_id, lock=False)
    touched = connection.execute(
        update(documents)
        .where(
            documents.c.id == document_id,
            documents.c.search_index_id == index_id,
            documents.c.is_deleted == BIT_FALSE,
            documents.c.row_version == row_version,
        )
        .values(updated_at=SYSUTCDATETIME, updated_by=requested_by)
    ).rowcount
    if touched == 0:
        raise _document_refusal(connection, index_id, document_id)
    current: UUID | None = connection.execute(
        select(documents.c.current_version_id).where(documents.c.id == document_id)
    ).scalar_one()
    if current is None:
        raise IngestionRefusedError("no_current_version")
    if index.status in (IndexStatus.DELETING, IndexStatus.FAILED):
        raise IngestionRefusedError("index_busy")
    # During a rebuild the active index is replaced at the repoint, so the building one is the
    # target, which is also how an admin retries a rebuild item that blocks the repoint.
    target = _building_index_id(connection, index_id) or index.active_physical_index_id
    if target is None:
        raise IngestionRefusedError("index_busy")
    open_run = connection.execute(
        select(ingestion_run_items.c.run_id)
        .join(ingestion_runs, ingestion_runs.c.id == ingestion_run_items.c.run_id)
        .where(
            ingestion_run_items.c.document_id == document_id,
            ingestion_run_items.c.document_version_id == current,
            ingestion_run_items.c.physical_index_id == target,
            ingestion_run_items.c.operation == _literal(ItemOperation.INGEST),
            ingestion_run_items.c.state.in_(_literals(_OPEN_ITEM_STATES)),
        )
        .order_by(ingestion_runs.c.created_at.desc(), ingestion_runs.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if open_run is not None:
        return DocumentReingestResult(run_id=open_run, created=False, item_count=0)
    created = _create_run(
        connection,
        kind=RunKind.DOCUMENT_REINGEST,
        index_id=index_id,
        items=[
            NewItem(
                ItemOperation.INGEST,
                physical_index_id=target,
                document_id=document_id,
                document_version_id=current,
            )
        ],
        requested_by=requested_by,
        trace=trace,
    )
    return DocumentReingestResult(run_id=created.run_id, created=True, item_count=1)


@dataclass(frozen=True, slots=True)
class DocumentDeleteResult:
    """`adminDeleteDocument`'s outcome, with the counts of its `document.deleted` audit row."""

    run_id: UUID
    superseded_version_count: int
    cancelled_item_count: int


def create_document_delete_run(
    connection: Connection,
    *,
    index_id: UUID,
    document_id: UUID,
    row_version: bytes,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentDeleteResult:
    """`adminDeleteDocument`'s transaction (section 7.1; ADR 0040): the guarded delete of the
    document row, its active versions superseded and its uploads failed, its served rows in every
    physical index removed (hidden at once), its live chunk sets retired, its queued items
    cancelled, and one `document_delete` run with one `cleanup_document` item. An index in
    `deleting` answers `409 index_busy` after the `404` and `412` checks."""
    index = _index_row(connection, index_id, lock=False)
    deleted = connection.execute(
        update(documents)
        .where(
            documents.c.id == document_id,
            documents.c.search_index_id == index_id,
            documents.c.is_deleted == BIT_FALSE,
            documents.c.row_version == row_version,
        )
        .values(
            is_deleted=BIT_TRUE,
            deleted_at=SYSUTCDATETIME,
            deleted_by=requested_by,
            current_version_id=None,
        )
    ).rowcount
    if deleted == 0:
        raise _document_refusal(connection, index_id, document_id)
    if index.status == IndexStatus.DELETING:
        raise IngestionRefusedError("index_busy")
    superseded = connection.execute(
        update(document_versions)
        .where(
            document_versions.c.document_id == document_id,
            document_versions.c.state.in_(_literals(ACTIVE_VERSION_STATES)),
        )
        .values(state=_literal(DocumentVersionState.SUPERSEDED), superseded_at=SYSUTCDATETIME)
    ).rowcount
    connection.execute(
        update(document_versions)
        .where(
            document_versions.c.document_id == document_id,
            document_versions.c.state == _literal(DocumentVersionState.UPLOADING),
        )
        .values(
            state=_literal(DocumentVersionState.FAILED),
            failure_code="document_deleted",
            failed_at=SYSUTCDATETIME,
        )
    )
    connection.execute(
        delete(served_chunk_sets).where(served_chunk_sets.c.document_id == document_id)
    )
    connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.document_id == document_id,
            chunk_sets.c.state.in_(_literals(LIVE_CHUNK_SET_STATES)),
        )
        .values(state=_literal(ChunkSetState.RETIRED), retired_at=SYSUTCDATETIME)
    )
    cancelled = connection.execute(
        update(ingestion_run_items)
        .where(
            ingestion_run_items.c.document_id == document_id,
            ingestion_run_items.c.state == _literal(ItemState.QUEUED),
        )
        .values(
            state=_literal(ItemState.CANCELLED),
            outcome_code="document_deleted",
            outcome_message=_cancel_message("document_deleted"),
            completed_at=SYSUTCDATETIME,
        )
    ).rowcount
    created = _create_run(
        connection,
        kind=RunKind.DOCUMENT_DELETE,
        index_id=index_id,
        items=[NewItem(ItemOperation.CLEANUP_DOCUMENT, document_id=document_id)],
        requested_by=requested_by,
        trace=trace,
    )
    return DocumentDeleteResult(
        run_id=created.run_id,
        superseded_version_count=superseded,
        cancelled_item_count=cancelled,
    )


# --- Index creation -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexSettings:
    """The settings a new logical index stores and its first physical index copies, already
    validated against the catalogue (section 8.3 steps 2 to 4)."""

    method: MethodDescriptor
    parameters: MethodParameters
    embedding: EmbeddingModel
    dimensions: int
    analyzer: str

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, self.method.parameters_model):
            raise TypeError("parameters of another method's model")
        if self.dimensions not in self.embedding.dimensions:
            raise ValueError("dimensions the model entry doesn't list")
        if self.analyzer not in ANALYZERS:
            raise ValueError("an analyzer outside the allowlist")


@dataclass(frozen=True, slots=True)
class IndexCreateResult:
    index_id: UUID
    physical_index_id: UUID
    run_id: UUID


def create_index_create_run(
    connection: Connection,
    *,
    display_name: str,
    description: str | None,
    settings: IndexSettings,
    max_indexes: int,
    created_by: UUID,
    trace: TraceContext | None,
) -> IndexCreateResult:
    """`adminCreateIndex`'s transaction (section 8.3): the capacity check, which keeps one slot
    free for a rebuild; the logical index (`provisioning`, parameters validated with defaults
    filled and their hash); physical index generation 1 (`building`, built by the new run); and
    one `index_create` run with one `create_physical_index` item. A display name in use fails the
    insert with `SqlUniqueViolation` on `UX_search_indexes_display_name` (`409
    display_name_taken`)."""
    _check_capacity(connection, max_indexes, keep_free=True)
    index_id = uuid4()
    physical_index_id = uuid4()
    connection.execute(
        insert(search_indexes).values(
            id=index_id,
            display_name=display_name,
            description=description,
            method_key=settings.method.key,
            method_version=settings.method.version,
            method_parameters=compact_json(settings.parameters.dumped()),
            method_parameter_hash=settings.method.parameter_hash(settings.parameters),
            embedding_model=settings.embedding.model,
            embedding_dimensions=settings.dimensions,
            language_analyzer=settings.analyzer,
            status=IndexStatus.PROVISIONING.value,
            created_by=created_by,
            updated_by=created_by,
        )
    )
    priority = run_priority(RunKind.INDEX_CREATE)
    run_id = _insert_run(
        connection,
        kind=RunKind.INDEX_CREATE,
        index_id=index_id,
        priority=priority,
        requested_by=created_by,
        trace=trace,
    )
    connection.execute(
        insert(physical_indexes).values(
            _physical_index_values(
                physical_index_id=physical_index_id,
                index_id=index_id,
                generation=1,
                embedding=settings.embedding,
                dimensions=settings.dimensions,
                analyzer=settings.analyzer,
                build_run_id=run_id,
            )
        )
    )
    _insert_items(
        connection,
        run_id=run_id,
        index_id=index_id,
        priority=priority,
        items=[NewItem(ItemOperation.CREATE_PHYSICAL_INDEX, physical_index_id=physical_index_id)],
    )
    return IndexCreateResult(index_id=index_id, physical_index_id=physical_index_id, run_id=run_id)


# --- Re-ingest and rebuild ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhysicalIndexSettings:
    """What decides whether a physical index can serve a logical index's settings."""

    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str
    schema_version: int


def serves_settings(
    active: PhysicalIndexSettings | None,
    *,
    embedding_model: str,
    embedding_dimensions: int,
    language_analyzer: str,
) -> bool:
    """Whether the active physical index was built with the logical index's embedding model,
    dimensions and analyzer and the current schema version; a change of any of them is never
    applied in place (ADR 0045)."""
    return active is not None and (
        active.embedding_model,
        active.embedding_dimensions,
        active.language_analyzer,
        active.schema_version,
    ) == (embedding_model, embedding_dimensions, language_analyzer, CHUNK_INDEX_SCHEMA_VERSION)


@dataclass(frozen=True, slots=True)
class ReingestResult:
    """`adminStartReingest`'s and `docchat-ops reindex start`'s outcome: `item_count` is the
    `ingest` items created, `physical_index_id` the target they build into."""

    run_id: UUID
    mode: ReingestMode
    item_count: int
    physical_index_id: UUID
    status: IndexStatus


def _active_settings(
    connection: Connection, physical_index_id: UUID | None
) -> PhysicalIndexSettings | None:
    if physical_index_id is None:
        return None
    row = connection.execute(
        select(
            physical_indexes.c.embedding_model,
            physical_indexes.c.embedding_dimensions,
            physical_indexes.c.language_analyzer,
            physical_indexes.c.schema_version,
        ).where(physical_indexes.c.id == physical_index_id)
    ).one()
    return PhysicalIndexSettings(
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
        schema_version=row.schema_version,
    )


def create_reingest_run(
    connection: Connection,
    *,
    index_id: UUID,
    row_version: bytes,
    requested_by: UUID,
    trace: TraceContext | None,
    max_indexes: int,
    catalog: EmbeddingModelCatalog,
) -> ReingestResult:
    """`adminStartReingest`'s transaction, which `docchat-ops reindex start` runs too (sections
    8.5 to 8.7): under the logical index row's update lock, `404`, then `412`, then `409
    index_busy` for a status other than `ready`, `reingest_required` or `failed`; then the mode.

    `same_index` (the active physical index serves the settings): an `index_reingest` run with
    one `ingest` item per live document's current version into the active index, status
    `reingesting`. `new_physical_index`: the capacity check (a rebuild may use the slot index
    creation keeps free), a leftover `building` physical index retired with its queued items
    moved, a new `building` physical index of the next generation copying the catalogue entry,
    an `index_rebuild` run with its `create_physical_index` item first and one `ingest` item per
    live document's current version into the new index, status `rebuilding`."""
    index = _index_row(connection, index_id, lock=True)
    index.check_row_version(row_version)
    if index.status not in SETTINGS_CHANGE_STATUSES:
        raise IngestionRefusedError("index_busy")
    active_id = index.active_physical_index_id
    active = _active_settings(connection, active_id)
    if active_id is not None and serves_settings(
        active,
        embedding_model=index.embedding_model,
        embedding_dimensions=index.embedding_dimensions,
        language_analyzer=index.language_analyzer,
    ):
        _set_index_status(
            connection, index_id, row_version, IndexStatus.REINGESTING, updated_by=requested_by
        )
        items = _live_current_versions(connection, index_id, active_id)
        created = _create_run(
            connection,
            kind=RunKind.INDEX_REINGEST,
            index_id=index_id,
            items=items,
            requested_by=requested_by,
            trace=trace,
        )
        return ReingestResult(
            created.run_id, ReingestMode.SAME_INDEX, len(items), active_id, IndexStatus.REINGESTING
        )
    _check_capacity(connection, max_indexes, keep_free=False)
    embedding = catalog.find(index.embedding_model)
    if embedding is None or index.embedding_dimensions not in embedding.dimensions:
        raise IngestionRefusedError("unsupported_embedding_model")
    _set_index_status(
        connection, index_id, row_version, IndexStatus.REBUILDING, updated_by=requested_by
    )
    target, run_id, item_count = _rebuild(
        connection,
        index_id=index_id,
        index=index,
        embedding=embedding,
        requested_by=requested_by,
        trace=trace,
    )
    return ReingestResult(
        run_id, ReingestMode.NEW_PHYSICAL_INDEX, item_count, target, IndexStatus.REBUILDING
    )


def _set_index_status(
    connection: Connection,
    index_id: UUID,
    row_version: bytes,
    status: IndexStatus,
    *,
    updated_by: UUID,
) -> None:
    """The guarded status update of an admin or operator change; zero rows is `412`."""
    changed = connection.execute(
        update(search_indexes)
        .where(search_indexes.c.id == index_id, search_indexes.c.row_version == row_version)
        .values(
            status=status.value,
            status_changed_at=SYSUTCDATETIME,
            updated_at=SYSUTCDATETIME,
            updated_by=updated_by,
        )
    ).rowcount
    if changed == 0:
        raise IngestionRefusedError("precondition_failed")


def _rebuild(
    connection: Connection,
    *,
    index_id: UUID,
    index: _LogicalIndex,
    embedding: EmbeddingModel,
    requested_by: UUID,
    trace: TraceContext | None,
) -> tuple[UUID, UUID, int]:
    # A failed index's never-provisioned physical index (the one `building` index a logical
    # index can hold) gives way first, so the new one can be `building`.
    leftover = _building_index_id(connection, index_id, lock=True)
    if leftover is not None:
        connection.execute(
            update(physical_indexes)
            .where(
                physical_indexes.c.id == leftover,
                physical_indexes.c.state == _literal(PhysicalIndexState.BUILDING),
            )
            .values(
                state=_literal(PhysicalIndexState.RETIRING),
                retiring_at=SYSUTCDATETIME,
                delete_after=SYSUTCDATETIME,
                build_run_id=None,
            )
        )
    generation = connection.execute(
        select(func.coalesce(func.max(physical_indexes.c.generation), 0)).where(
            physical_indexes.c.search_index_id == index_id
        )
    ).scalar_one()
    priority = run_priority(RunKind.INDEX_REBUILD)
    run_id = _insert_run(
        connection,
        kind=RunKind.INDEX_REBUILD,
        index_id=index_id,
        priority=priority,
        requested_by=requested_by,
        trace=trace,
    )
    target = uuid4()
    connection.execute(
        insert(physical_indexes).values(
            _physical_index_values(
                physical_index_id=target,
                index_id=index_id,
                generation=generation + 1,
                embedding=embedding,
                dimensions=index.embedding_dimensions,
                analyzer=index.language_analyzer,
                build_run_id=run_id,
            )
        )
    )
    if leftover is not None:
        connection.execute(
            update(ingestion_run_items)
            .where(
                ingestion_run_items.c.physical_index_id == leftover,
                ingestion_run_items.c.state == _literal(ItemState.QUEUED),
            )
            .values(physical_index_id=target)
        )
    # The create item first, so it has the lowest `queue_seq` of the run.
    _insert_items(
        connection,
        run_id=run_id,
        index_id=index_id,
        priority=priority,
        items=[NewItem(ItemOperation.CREATE_PHYSICAL_INDEX, physical_index_id=target)],
    )
    items = _live_current_versions(connection, index_id, target)
    _insert_items(connection, run_id=run_id, index_id=index_id, priority=priority, items=items)
    return target, run_id, len(items)


# --- Index deletion -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexDeleteResult:
    """`adminDeleteIndex`'s outcome, with the counts of its `index.deleted` audit row."""

    run_id: UUID
    physical_index_count: int
    document_count: int


def create_index_delete_run(
    connection: Connection,
    *,
    index_id: UUID,
    row_version: bytes,
    requested_by: UUID,
    trace: TraceContext | None,
) -> IndexDeleteResult:
    """`adminDeleteIndex`'s transaction (section 7.2; ADR 0052): under the row's update lock,
    `404`, `412`, `409 index_mapped` while a project maps the index, then `409 index_busy` for a
    status other than `ready`, `reingest_required` or `failed`; then the status `deleting`, every
    live document deleted as one delete would delete it (set-based, without per-document runs),
    every queued item of the index cancelled with `index_deleted`, and one `index_delete` run
    with a `delete_physical_index` item per physical index not yet deleted and one
    `purge_index_blobs` item."""
    index = _index_row(connection, index_id, lock=True)
    index.check_row_version(row_version)
    mapped = connection.execute(
        select(project_indexes.c.project_id)
        .where(project_indexes.c.search_index_id == index_id)
        .limit(1)
    ).first()
    if mapped is not None:
        raise IngestionRefusedError("index_mapped")
    if index.status not in SETTINGS_CHANGE_STATUSES:
        raise IngestionRefusedError("index_busy")
    _set_index_status(
        connection, index_id, row_version, IndexStatus.DELETING, updated_by=requested_by
    )
    document_count = connection.execute(
        update(documents)
        .where(documents.c.search_index_id == index_id, documents.c.is_deleted == BIT_FALSE)
        .values(
            is_deleted=BIT_TRUE,
            deleted_at=SYSUTCDATETIME,
            deleted_by=requested_by,
            current_version_id=None,
        )
    ).rowcount
    connection.execute(
        update(document_versions)
        .where(
            document_versions.c.search_index_id == index_id,
            document_versions.c.state.in_(_literals(ACTIVE_VERSION_STATES)),
        )
        .values(state=_literal(DocumentVersionState.SUPERSEDED), superseded_at=SYSUTCDATETIME)
    )
    connection.execute(
        update(document_versions)
        .where(
            document_versions.c.search_index_id == index_id,
            document_versions.c.state == _literal(DocumentVersionState.UPLOADING),
        )
        .values(
            state=_literal(DocumentVersionState.FAILED),
            failure_code="document_deleted",
            failed_at=SYSUTCDATETIME,
        )
    )
    index_documents = select(documents.c.id).where(documents.c.search_index_id == index_id)
    connection.execute(
        delete(served_chunk_sets).where(served_chunk_sets.c.document_id.in_(index_documents))
    )
    connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.document_id.in_(index_documents),
            chunk_sets.c.state.in_(_literals(LIVE_CHUNK_SET_STATES)),
        )
        .values(state=_literal(ChunkSetState.RETIRED), retired_at=SYSUTCDATETIME)
    )
    connection.execute(
        update(ingestion_run_items)
        .where(
            ingestion_run_items.c.search_index_id == index_id,
            ingestion_run_items.c.state == _literal(ItemState.QUEUED),
        )
        .values(
            state=_literal(ItemState.CANCELLED),
            outcome_code="index_deleted",
            outcome_message=_cancel_message("index_deleted"),
            completed_at=SYSUTCDATETIME,
        )
    )
    physical = list(
        connection.execute(
            select(physical_indexes.c.id)
            .where(
                physical_indexes.c.search_index_id == index_id,
                physical_indexes.c.state != _literal(PhysicalIndexState.DELETED),
            )
            .order_by(physical_indexes.c.generation)
        ).scalars()
    )
    created = _create_run(
        connection,
        kind=RunKind.INDEX_DELETE,
        index_id=index_id,
        items=[
            *(
                NewItem(ItemOperation.DELETE_PHYSICAL_INDEX, physical_index_id=physical_index_id)
                for physical_index_id in physical
            ),
            NewItem(ItemOperation.PURGE_INDEX_BLOBS),
        ],
        requested_by=requested_by,
        trace=trace,
    )
    return IndexDeleteResult(
        run_id=created.run_id,
        physical_index_count=len(physical),
        document_count=document_count,
    )


# --- Failure side effects -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureSideEffects:
    """What the failure side effects of one failed item changed."""

    chunk_set_retired: bool
    version_failed: bool


def apply_failure_side_effects(
    connection: Connection, item_id: UUID, code: str
) -> FailureSideEffects:
    """The failure side effects of section 6.11, which `record_item_outcome`, sweep duty 2 and
    `docchat-ops ingestion fail-item` apply in the transaction that fails the item: its chunk set
    retired while still `building` or `ready`, and its version failed with `code` while `pending`
    or `ingesting` when the item targets the logical index's active physical index or, with no
    active one, the logical index's building one.

    Both statements read the item's row, so an item without a chunk set or a version (an index or
    clean-up item) changes nothing, the state guards leave a `rerender` item's served set and its
    version alone, and a failed re-ingest or rebuild of a current version changes only the item
    and its chunk set. The version update sets only `state`, `failure_code` and `failed_at`, the
    columns `docchat_config_operator`'s column grant covers."""
    item = ingestion_run_items.alias("i")
    retired = connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.id == item.c.chunk_set_id,
            item.c.id == item_id,
            chunk_sets.c.state.in_(_literals((ChunkSetState.BUILDING, ChunkSetState.READY))),
        )
        .values(state=_literal(ChunkSetState.RETIRED), retired_at=SYSUTCDATETIME)
    ).rowcount
    # Nested two levels deep, the building check would otherwise take its own copy of the item
    # alias into its FROM list and test every item instead of this one.
    targets_building_index = (
        exists()
        .where(
            physical_indexes.c.id == item.c.physical_index_id,
            physical_indexes.c.search_index_id == item.c.search_index_id,
            physical_indexes.c.state == _literal(PhysicalIndexState.BUILDING),
        )
        .correlate(item)
    )
    targets_live_index = exists().where(
        search_indexes.c.id == item.c.search_index_id,
        or_(
            search_indexes.c.active_physical_index_id == item.c.physical_index_id,
            and_(search_indexes.c.active_physical_index_id.is_(None), targets_building_index),
        ),
    )
    failed = connection.execute(
        update(document_versions)
        .where(
            document_versions.c.id == item.c.document_version_id,
            item.c.id == item_id,
            document_versions.c.state.in_(
                _literals((DocumentVersionState.PENDING, DocumentVersionState.INGESTING))
            ),
            targets_live_index,
        )
        .values(
            state=_literal(DocumentVersionState.FAILED),
            failure_code=code,
            failed_at=SYSUTCDATETIME,
        )
    ).rowcount
    return FailureSideEffects(chunk_set_retired=retired > 0, version_failed=failed > 0)


# --- The logical index status ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexStatusFacts:
    """What `compute_index_status` decides on: the logical index's settings, its active physical
    index's (or `None`) and whether a live document's served chunk set in the active index was
    built with another method key, version or parameter hash."""

    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str
    active: PhysicalIndexSettings | None
    stale_served_sets: bool


def decide_index_status(facts: IndexStatusFacts) -> IndexStatus:
    """`failed` with no active physical index; `reingest_required` when the active index doesn't
    serve the settings or a served chunk set is stale; `ready` otherwise (`80-data-model.md`
    section 8.4)."""
    if facts.active is None:
        return IndexStatus.FAILED
    if facts.stale_served_sets or not serves_settings(
        facts.active,
        embedding_model=facts.embedding_model,
        embedding_dimensions=facts.embedding_dimensions,
        language_analyzer=facts.language_analyzer,
    ):
        return IndexStatus.REINGEST_REQUIRED
    return IndexStatus.READY


def compute_index_status(connection: Connection, index_id: UUID) -> IndexStatus:
    """The status a logical index's settings and served chunk sets call for (sections 8.1 and
    8.4), used by `adminUpdateIndex`, run finalisation and `abandon-rebuild` inside their own
    transactions. `LookupError` for an index that doesn't exist."""
    active_join = and_(
        physical_indexes.c.id == search_indexes.c.active_physical_index_id,
        physical_indexes.c.state == _literal(PhysicalIndexState.ACTIVE),
    )
    row = connection.execute(
        select(
            search_indexes.c.method_key,
            search_indexes.c.method_version,
            search_indexes.c.method_parameter_hash,
            search_indexes.c.embedding_model,
            search_indexes.c.embedding_dimensions,
            search_indexes.c.language_analyzer,
            physical_indexes.c.id.label("active_id"),
            physical_indexes.c.embedding_model.label("active_model"),
            physical_indexes.c.embedding_dimensions.label("active_dimensions"),
            physical_indexes.c.language_analyzer.label("active_analyzer"),
            physical_indexes.c.schema_version.label("active_schema_version"),
        )
        .select_from(search_indexes.outerjoin(physical_indexes, active_join))
        .where(search_indexes.c.id == index_id)
    ).first()
    if row is None:
        raise LookupError("no logical index with this ID")
    active: PhysicalIndexSettings | None = None
    stale = False
    if row.active_id is not None:
        active = PhysicalIndexSettings(
            embedding_model=row.active_model,
            embedding_dimensions=row.active_dimensions,
            language_analyzer=row.active_analyzer,
            schema_version=row.active_schema_version,
        )
        stale = (
            connection.execute(
                select(documents.c.id)
                .join(
                    served_chunk_sets,
                    and_(
                        served_chunk_sets.c.document_id == documents.c.id,
                        served_chunk_sets.c.physical_index_id == row.active_id,
                    ),
                )
                .join(chunk_sets, chunk_sets.c.id == served_chunk_sets.c.chunk_set_id)
                .where(
                    documents.c.search_index_id == index_id,
                    documents.c.is_deleted == BIT_FALSE,
                    or_(
                        chunk_sets.c.method_key != row.method_key,
                        chunk_sets.c.method_version != row.method_version,
                        chunk_sets.c.parameter_hash != row.method_parameter_hash,
                    ),
                )
                .limit(1)
            ).first()
            is not None
        )
    return decide_index_status(
        IndexStatusFacts(
            embedding_model=row.embedding_model,
            embedding_dimensions=row.embedding_dimensions,
            language_analyzer=row.language_analyzer,
            active=active,
            stale_served_sets=stale,
        )
    )
