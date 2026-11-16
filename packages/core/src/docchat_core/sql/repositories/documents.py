"""The document statements of the administrator API (`11-api.md` sections 3.11.1 to 3.11.4 and
4.7; `14-ingestion.md` sections 7.1 and 8.11; `80-data-model.md` sections 2.12 and 2.13): the
document list of an index, one document's detail, and the two document writes, which wrap the
run-creation functions of `docchat_core.ingestion.runs` with the lookups, the version check and
the audit row of their operation.

Each list shape is one constant text per case (with or without the state filter, the name prefix
and a cursor), built once at import. The page reads the index's documents in name order on
`UQ_documents_index_name`; each document's latest version is the row a correlated `TOP (1) …
ORDER BY version_number DESC` on `IX_document_versions_document_state` names (SQLAlchemy renders
no `OUTER APPLY` for SQL Server, so the latest version joins on that subquery instead); the
current version joins on its primary key; uploader and project names come from joins inside the
statement, never from one lookup per row. A document's display state is one `CASE` over two
`EXISTS` seeks (an open version on `IX_document_versions_document_state`, an open item on
`IX_run_items_document`) and the latest version's state.

A write runs its lookups (`404`), then the caller's version check (`428`, `412`), then the
ledger function (its own `409`s), in one transaction; the caller passes the check as a function
of the row's version, so this module needs nothing of the API.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import Connection, Row, Select, and_, bindparam, case, exists, or_, select

from docchat_core.audit import AuditEvent, write_audit_event
from docchat_core.audit.models import DocumentDeletedMetadata, DocumentReingestRequestedMetadata
from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    IndexStatus,
    IngestionRefusedError,
    ItemState,
    ScanStatus,
    TraceContext,
)
from docchat_core.ingestion.runs import (
    LOCK_ROW,
    create_document_delete_run,
    create_document_reingest_run,
)
from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, top_rows, utc
from docchat_core.sql.repositories.runs import RunItemRow, read_document_items
from docchat_core.sql.repositories.users import LIKE_ESCAPE, like_prefix
from docchat_core.sql.tables.access import projects, users
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_run_items,
    search_indexes,
)
from docchat_core.sql.tables.schema import BIT_FALSE, nvarchar_literal

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
LIST_DOCUMENTS: Final = "admin_list_documents"
READ_DOCUMENT: Final = "admin_get_document"
DELETE_DOCUMENT: Final = "admin_delete_document"
REINGEST_DOCUMENT: Final = "admin_reingest_document"

VERSIONS_MAX: Final = 100
"""A document's detail lists its 100 newest versions (`DocumentDetail.versions`)."""

type DisplayState = Literal["ingested", "in_progress", "failed", "no_content"]
type VersionCheck = Callable[[bytes], None]
"""The caller's `If-Match` check of a row's version: returns, or raises its `428` or `412`."""

_OPEN_VERSION_STATES: Final = (
    DocumentVersionState.UPLOADING,
    DocumentVersionState.PENDING,
    DocumentVersionState.INGESTING,
)
_OPEN_ITEM_STATES: Final = (ItemState.QUEUED, ItemState.RUNNING)

# Fixed identifiers for the plan audit: a plan doesn't depend on whether the rows exist.
_EXAMPLE_INDEX: Final = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
_EXAMPLE_DOCUMENT: Final = UUID("ad5e6f7a-8b9c-4dae-9f20-4b5c6d7e8f64")
_EXAMPLE_ADMIN: Final = UUID("6f1c2a4e-8b3d-4e5f-9a7b-1c2d3e4f5a60")
_EXAMPLE_PAGE: Final = 51


def accept_any_version(_row_version: bytes) -> None:
    """The plan audit's version check: every version passes."""


def _literal(value: str) -> Any:
    return nvarchar_literal(value)


# --- Rows -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionRow:
    """One version with its uploader's display name (`None` without an `app.users` row) and the
    name of the project it came through."""

    id: UUID
    version_number: int
    state: DocumentVersionState
    size_bytes: int
    sha256: bytes
    content_type: ContentType
    scan_status: ScanStatus
    failure_code: str | None
    uploaded_by: UUID
    uploaded_by_name: str | None
    project_id: UUID
    project_name: str
    reserved_at: datetime
    committed_at: datetime | None
    ingested_at: datetime | None
    document_pdf_status: DocumentPdfStatus | None


@dataclass(frozen=True, slots=True)
class DocumentRow:
    """One live document with its display state, its current version (the one readers see) and
    its latest version (the highest number)."""

    id: UUID
    source_name: str
    normalized_name: str
    display_state: DisplayState
    current_version: VersionRow | None
    latest_version: VersionRow | None
    updated_at: datetime
    row_version: bytes


@dataclass(frozen=True, slots=True)
class DocumentDetailRow:
    document: DocumentRow
    versions: tuple[VersionRow, ...]
    recent_items: tuple[RunItemRow, ...]


@dataclass(frozen=True, slots=True)
class DocumentPosition:
    """A keyset position in the document list: the last item's normalised name and ID."""

    normalized_name: str
    document_id: UUID


@dataclass(frozen=True, slots=True)
class DocumentFilters:
    """The document list's optional filters: a display state (`no_content` isn't one) and the
    normalised name prefix."""

    display_state: Literal["ingested", "in_progress", "failed"] | None = None
    name_prefix: str | None = None


# --- Shared expressions ---------------------------------------------------------------------

_VERSION_FIELDS: Final = (
    "id",
    "version_number",
    "state",
    "size_bytes",
    "sha256",
    "content_type",
    "scan_status",
    "failure_code",
    "uploaded_by",
    "uploaded_via_project_id",
    "reserved_at",
    "committed_at",
    "ingested_at",
    "document_pdf_status",
)


def _version_columns(prefix: str, v: Any, u: Any, p: Any) -> list[Any]:
    columns = [getattr(v.c, name).label(prefix + name) for name in _VERSION_FIELDS]
    columns.append(u.c.display_name.label(prefix + "uploaded_by_name"))
    columns.append(p.c.name.label(prefix + "project_name"))
    return columns


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else utc(value)


def version_row(row: Row[Any], prefix: str = "") -> VersionRow | None:
    """The version a row's `prefix` columns hold; `None` when they are null (no such version)."""
    values = row._mapping
    if values[prefix + "id"] is None:
        return None
    pdf_status = values[prefix + "document_pdf_status"]
    return VersionRow(
        id=values[prefix + "id"],
        version_number=values[prefix + "version_number"],
        state=DocumentVersionState(values[prefix + "state"]),
        size_bytes=int(values[prefix + "size_bytes"]),
        sha256=bytes(values[prefix + "sha256"]),
        content_type=ContentType(values[prefix + "content_type"]),
        scan_status=ScanStatus(values[prefix + "scan_status"]),
        failure_code=values[prefix + "failure_code"],
        uploaded_by=values[prefix + "uploaded_by"],
        uploaded_by_name=values[prefix + "uploaded_by_name"],
        project_id=values[prefix + "uploaded_via_project_id"],
        project_name=values[prefix + "project_name"],
        reserved_at=utc(values[prefix + "reserved_at"]),
        committed_at=_optional_utc(values[prefix + "committed_at"]),
        ingested_at=_optional_utc(values[prefix + "ingested_at"]),
        document_pdf_status=None if pdf_status is None else DocumentPdfStatus(pdf_status),
    )


def latest_version_id(d: Any) -> Any:
    """The ID of `d`'s highest-numbered version, as a correlated scalar subquery."""
    v = document_versions.alias("lv_pick")
    return (
        select(v.c.id)
        .where(v.c.document_id == d.c.id)
        .order_by(v.c.version_number.desc())
        .limit(1)
        .correlate(d)
        .scalar_subquery()
    )


def display_state(d: Any, latest_state: Any) -> Any:
    """`DocumentSummary.displayState` in this order: `in_progress` when a version is
    `uploading`, `pending` or `ingesting` or an item for the document is `queued` or `running`;
    else `failed` when the latest version is `failed`; else `ingested` with a current version;
    else `no_content`."""
    v = document_versions.alias("open_v")
    i = ingestion_run_items.alias("open_i")
    in_progress = or_(
        exists()
        .where(
            v.c.document_id == d.c.id,
            v.c.state.in_([_literal(state) for state in _OPEN_VERSION_STATES]),
        )
        .correlate(d),
        exists()
        .where(
            i.c.document_id == d.c.id,
            i.c.state.in_([_literal(state) for state in _OPEN_ITEM_STATES]),
        )
        .correlate(d),
    )
    return case(
        (in_progress, _literal("in_progress")),
        (latest_state == _literal(DocumentVersionState.FAILED), _literal("failed")),
        (d.c.current_version_id.is_not(None), _literal("ingested")),
        else_=_literal("no_content"),
    )


def _document_source() -> tuple[Any, Any, list[Any]]:
    """The documents with their current and latest versions joined: the source, the document
    alias and the selected columns (the display state labelled `display_state`)."""
    d = documents.alias("d")
    cv = document_versions.alias("cv")
    cu = users.alias("cu")
    cp = projects.alias("cp")
    lv = document_versions.alias("lv")
    lu = users.alias("lu")
    lp = projects.alias("lp")
    source = (
        d.outerjoin(cv, cv.c.id == d.c.current_version_id)
        .outerjoin(cu, cu.c.object_id == cv.c.uploaded_by)
        .outerjoin(cp, cp.c.id == cv.c.uploaded_via_project_id)
        .outerjoin(lv, lv.c.id == latest_version_id(d))
        .outerjoin(lu, lu.c.object_id == lv.c.uploaded_by)
        .outerjoin(lp, lp.c.id == lv.c.uploaded_via_project_id)
    )
    state = display_state(d, lv.c.state)
    columns = [
        d.c.id,
        d.c.source_name,
        d.c.normalized_name,
        d.c.updated_at,
        d.c.row_version,
        state.label("display_state"),
        *_version_columns("cv_", cv, cu, cp),
        *_version_columns("lv_", lv, lu, lp),
    ]
    return source, (d, state), columns


def document_row(row: Row[Any]) -> DocumentRow:
    """A `DocumentRow` from a row of `DOCUMENT_PAGES` or `DOCUMENT_BY_ID`."""
    return DocumentRow(
        id=row.id,
        source_name=row.source_name,
        normalized_name=row.normalized_name,
        display_state=row.display_state,
        current_version=version_row(row, "cv_"),
        latest_version=version_row(row, "lv_"),
        updated_at=utc(row.updated_at),
        row_version=bytes(row.row_version),
    )


# --- The document list ----------------------------------------------------------------------


def _documents_page(*, state: bool, prefix: bool, cursor: bool) -> Select[Any]:
    source, (d, display), columns = _document_source()
    conditions: list[Any] = [
        d.c.search_index_id == bindparam("index_id"),
        d.c.is_deleted == BIT_FALSE,
    ]
    if state:
        conditions.append(display == bindparam("display_state", type_=documents.c.source_name.type))
    if prefix:
        conditions.append(
            d.c.normalized_name.like(
                bindparam("prefix", type_=documents.c.normalized_name.type), escape=LIKE_ESCAPE
            )
        )
    if cursor:
        after = bindparam("after_name", type_=documents.c.normalized_name.type)
        conditions.append(
            or_(
                d.c.normalized_name > after,
                and_(d.c.normalized_name == after, d.c.id > bindparam("after_document_id")),
            )
        )
    return top_rows(
        select(*columns)
        .select_from(source)
        .where(*conditions)
        .order_by(d.c.normalized_name, d.c.id)
    )


DOCUMENT_PAGES: Final[Mapping[tuple[bool, bool, bool], Select[Any]]] = MappingProxyType(
    {
        (state, prefix, cursor): _documents_page(state=state, prefix=prefix, cursor=cursor)
        for state in (False, True)
        for prefix in (False, True)
        for cursor in (False, True)
    }
)
"""The live documents of an index by (state filter, name prefix, cursor), in normalised name
order, then by document ID."""


def _document_by_id() -> Select[Any]:
    source, (d, _), columns = _document_source()
    return (
        select(*columns)
        .select_from(source)
        .where(
            d.c.id == bindparam("document_id"),
            d.c.search_index_id == bindparam("index_id"),
            d.c.is_deleted == BIT_FALSE,
        )
    )


DOCUMENT_BY_ID: Final = _document_by_id()
"""One live document of an index; a document of another index or a deleted one reads as
absent."""


def _versions() -> Select[Any]:
    v = document_versions.alias("v")
    u = users.alias("u")
    p = projects.alias("p")
    return (
        select(*_version_columns("", v, u, p))
        .select_from(
            v.outerjoin(u, u.c.object_id == v.c.uploaded_by).outerjoin(
                p, p.c.id == v.c.uploaded_via_project_id
            )
        )
        .where(v.c.document_id == bindparam("document_id"))
        .order_by(v.c.version_number.desc())
        .limit(VERSIONS_MAX)
    )


DOCUMENT_VERSIONS: Final = _versions()
"""A document's 100 newest versions, on `UQ_document_versions_number`."""

INDEX_STATUS: Final[Select[Any]] = select(search_indexes.c.status).where(
    search_indexes.c.id == bindparam("index_id")
)
"""A logical index's status by primary key."""

LIVE_DOCUMENT_FOR_UPDATE: Final[Select[Any]] = (
    select(documents.c.row_version)
    .with_hint(documents, LOCK_ROW, "mssql")
    .where(
        documents.c.id == bindparam("document_id"),
        documents.c.search_index_id == bindparam("index_id"),
        documents.c.is_deleted == BIT_FALSE,
    )
)
"""The live document of an index under an update lock, for a guarded write."""


def require_index(connection: Connection, index_id: UUID) -> IndexStatus:
    """The logical index's status; `index_not_found` when it is missing or `deleted`."""
    status = connection.execute(INDEX_STATUS, {"index_id": index_id}).scalar_one_or_none()
    if status is None or status == IndexStatus.DELETED:
        raise IngestionRefusedError("index_not_found")
    return IndexStatus(status)


def _document(connection: Connection, index_id: UUID, document_id: UUID) -> DocumentRow:
    row = connection.execute(
        DOCUMENT_BY_ID, {"index_id": index_id, "document_id": document_id}
    ).first()
    if row is None:
        raise IngestionRefusedError("document_not_found")
    return document_row(row)


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "filters": DocumentFilters(),
        "after": None,
        LIMIT_PLUS_ONE: _EXAMPLE_PAGE,
    }
)
async def read_documents(
    engine: SqlEngine,
    *,
    index_id: UUID,
    filters: DocumentFilters,
    after: DocumentPosition | None,
    limit_plus_one: int,
) -> list[DocumentRow]:
    """Up to `limit_plus_one` live documents of the index after `after`; `index_not_found`
    when the index is missing or `deleted`. `filters.name_prefix` is the normalised prefix."""
    parameters: dict[str, object] = {"index_id": index_id, LIMIT_PLUS_ONE: limit_plus_one}
    if filters.display_state is not None:
        parameters["display_state"] = filters.display_state
    if filters.name_prefix is not None:
        parameters["prefix"] = like_prefix(filters.name_prefix)
    if after is not None:
        parameters["after_name"] = after.normalized_name
        parameters["after_document_id"] = after.document_id
    statement = DOCUMENT_PAGES[
        (filters.display_state is not None, filters.name_prefix is not None, after is not None)
    ]

    def work(connection: Connection) -> list[DocumentRow]:
        require_index(connection, index_id)
        return [document_row(row) for row in connection.execute(statement, parameters).all()]

    return await engine.run(work, name=LIST_DOCUMENTS, retry="transient")


@request_path(example_params={"index_id": _EXAMPLE_INDEX, "document_id": _EXAMPLE_DOCUMENT})
async def read_document(
    engine: SqlEngine, *, index_id: UUID, document_id: UUID
) -> DocumentDetailRow:
    """One live document of the index with its versions and recent items; `index_not_found`,
    then `document_not_found`."""

    def work(connection: Connection) -> DocumentDetailRow:
        require_index(connection, index_id)
        document = _document(connection, index_id, document_id)
        versions = connection.execute(DOCUMENT_VERSIONS, {"document_id": document_id}).all()
        return DocumentDetailRow(
            document=document,
            versions=tuple(row for row in map(version_row, versions) if row is not None),
            recent_items=read_document_items(connection, document_id),
        )

    return await engine.run(work, name=READ_DOCUMENT, retry="transient")


# --- Writes ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentDeleted:
    run_id: UUID
    events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class DocumentReingested:
    """`created` false when an item for the current version and target was already open: the
    run is then the newest open run holding it and `item_count` is 0."""

    run_id: UUID
    created: bool
    item_count: int
    document: DocumentRow
    events: tuple[AuditEvent, ...]


def _lock_live_document(connection: Connection, index_id: UUID, document_id: UUID) -> bytes:
    row_version = connection.execute(
        LIVE_DOCUMENT_FOR_UPDATE, {"index_id": index_id, "document_id": document_id}
    ).scalar_one_or_none()
    if row_version is None:
        raise IngestionRefusedError("document_not_found")
    return bytes(row_version)


def _delete(
    connection: Connection,
    *,
    index_id: UUID,
    document_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentDeleted:
    require_index(connection, index_id)
    row_version = _lock_live_document(connection, index_id, document_id)
    check_version(row_version)
    result = create_document_delete_run(
        connection,
        index_id=index_id,
        document_id=document_id,
        row_version=row_version,
        requested_by=requested_by,
        trace=trace,
    )
    event = AuditEvent(
        action="document.deleted",
        actor_type="user",
        actor_object_id=requested_by,
        target_type="document",
        target_id=str(document_id),
        metadata=DocumentDeletedMetadata(
            index_id=index_id,
            run_id=result.run_id,
            superseded_version_count=result.superseded_version_count,
            cancelled_item_count=result.cancelled_item_count,
        ),
    )
    write_audit_event(connection, event)
    return DocumentDeleted(run_id=result.run_id, events=(event,))


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "document_id": _EXAMPLE_DOCUMENT,
        "check_version": accept_any_version,
        "requested_by": _EXAMPLE_ADMIN,
        "trace": None,
    }
)
async def delete_document(
    engine: SqlEngine,
    *,
    index_id: UUID,
    document_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentDeleted:
    """`adminDeleteDocument`'s transaction (section 7.1): the index and the live document
    (`404`), `check_version` on the document's row version, then the delete and its
    `document_delete` run (`409 index_busy` for an index in `deleting`) and the audit row
    `document.deleted`. It creates a run, so it is never re-run on a transient error."""

    def work(connection: Connection) -> DocumentDeleted:
        return _delete(
            connection,
            index_id=index_id,
            document_id=document_id,
            check_version=check_version,
            requested_by=requested_by,
            trace=trace,
        )

    return await engine.run(work, name=DELETE_DOCUMENT, retry="none")


def _reingest(
    connection: Connection,
    *,
    index_id: UUID,
    document_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentReingested:
    require_index(connection, index_id)
    row_version = _lock_live_document(connection, index_id, document_id)
    check_version(row_version)
    result = create_document_reingest_run(
        connection,
        index_id=index_id,
        document_id=document_id,
        row_version=row_version,
        requested_by=requested_by,
        trace=trace,
    )
    event = AuditEvent(
        action="document.reingest_requested",
        actor_type="user",
        actor_object_id=requested_by,
        target_type="document",
        target_id=str(document_id),
        metadata=DocumentReingestRequestedMetadata(
            index_id=index_id,
            run_id=result.run_id if result.created else None,
            item_count=result.item_count,
        ),
    )
    write_audit_event(connection, event)
    return DocumentReingested(
        run_id=result.run_id,
        created=result.created,
        item_count=result.item_count,
        document=_document(connection, index_id, document_id),
        events=(event,),
    )


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "document_id": _EXAMPLE_DOCUMENT,
        "check_version": accept_any_version,
        "requested_by": _EXAMPLE_ADMIN,
        "trace": None,
    }
)
async def reingest_document(
    engine: SqlEngine,
    *,
    index_id: UUID,
    document_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> DocumentReingested:
    """`adminReingestDocument`'s transaction (section 8.11): the index and the live document
    (`404`), `check_version`, then the guarded document update and its `document_reingest` run
    (`409 no_current_version`, then `409 index_busy`), the audit row
    `document.reingest_requested`, and the document as it now reads, with its new row
    version."""

    def work(connection: Connection) -> DocumentReingested:
        return _reingest(
            connection,
            index_id=index_id,
            document_id=document_id,
            check_version=check_version,
            requested_by=requested_by,
            trace=trace,
        )

    return await engine.run(work, name=REINGEST_DOCUMENT, retry="none")
