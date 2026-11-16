"""The logical-index statements of the administrator API (`11-api.md` sections 3.10 and 4.7;
`14-ingestion.md` sections 7.2 and 8.1 to 8.7; `80-data-model.md` sections 2.9 to 2.11): the
method catalogue the index form offers, the index list, one index's detail, and the four index
writes, which wrap the run-creation functions of `docchat_core.ingestion.runs` with the lookups,
the version check, the catalogue checks and the audit row of their operation.

The list's shapes are one constant text per case (with or without the status filter, the name
prefix and a cursor), built once at import. Without a status filter the list reads
`UX_search_indexes_display_name`, whose filter `status <> N'deleted'` the statement repeats as a
literal, so it holds every index that isn't deleted in name order; with one it seeks
`IX_search_indexes_status`. Each index's live-document count is a correlated count on
`IX_documents_index_live`. The detail adds its physical indexes (`UX_physical_indexes_generation`),
its document counts by display state, its rebuild progress (`IX_served_chunk_sets_physical`), the
projects that map it (`IX_project_indexes_index`) and its newest open run
(`IX_ingestion_runs_index_created`), all in one unit of work.

A write reads the index under an update lock (`404`), runs the caller's version check (`428`,
`412`), then its catalogue checks (`422`) and state rules (`409`) and its statements, in one
transaction, and reads the detail it answers with before the commit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, Row, Select, and_, bindparam, case, exists, func, or_, select
from sqlalchemy import update as sql_update

from docchat_core.audit import AuditEvent, write_audit_event
from docchat_core.audit.models import (
    IndexCreatedMetadata,
    IndexDeletedMetadata,
    IndexField,
    IndexReingestStartedMetadata,
    IndexUpdatedMetadata,
)
from docchat_core.ingestion.methods import (
    MethodDescriptor,
    MethodParameters,
    compact_json,
    resolve_method,
)
from docchat_core.ingestion.models import (
    SETTINGS_CHANGE_STATUSES,
    ContentType,
    IndexStatus,
    IngestionRefusedError,
    PhysicalIndexState,
    ReingestMode,
    RunKind,
    RunState,
    TraceContext,
)
from docchat_core.ingestion.runs import (
    LOCK_ROW,
    IndexSettings,
    compute_index_status,
    create_index_create_run,
    create_index_delete_run,
    create_reingest_run,
)
from docchat_core.ingestion.settings import EmbeddingModel, EmbeddingModelCatalog
from docchat_core.search.schema import ANALYZERS, DEFAULT_ANALYZER
from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, top_rows, utc
from docchat_core.sql.repositories.documents import (
    VersionCheck,
    accept_any_version,
    display_state,
    latest_version_id,
)
from docchat_core.sql.repositories.users import LIKE_ESCAPE, like_prefix
from docchat_core.sql.tables.access import project_indexes, projects
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_methods,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import BIT_FALSE, BIT_TRUE, SYSUTCDATETIME, nvarchar_literal

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
INDEX_OPTIONS: Final = "admin_index_options"
LIST_INDEXES: Final = "admin_list_indexes"
READ_INDEX: Final = "admin_get_index"
CREATE_INDEX: Final = "admin_create_index"
UPDATE_INDEX: Final = "admin_update_index"
DELETE_INDEX: Final = "admin_delete_index"
START_REINGEST: Final = "admin_start_reingest"

MAPPED_PROJECTS_MAX: Final = 200
"""`IndexDetail.mappedProjects` lists at most 200 projects."""

SETTINGS_FIELDS: Final[frozenset[IndexField]] = frozenset(
    {
        "methodKey",
        "methodVersion",
        "methodParameters",
        "embeddingModel",
        "embeddingDimensions",
        "languageAnalyzer",
    }
)
"""The fields that change what new builds use; they change only while the status is `ready`,
`reingest_required` or `failed` (section 8.4)."""

NAME_FIELDS: Final[frozenset[IndexField]] = frozenset({"displayName", "description"})
"""The fields that change in any status except `deleting`."""

FIELD_ORDER: Final[tuple[IndexField, ...]] = (
    "displayName",
    "description",
    "methodKey",
    "methodVersion",
    "methodParameters",
    "embeddingModel",
    "embeddingDimensions",
    "languageAnalyzer",
)

_OPEN_RUN_STATES: Final = (RunState.QUEUED, RunState.RUNNING)

# Fixed identifiers for the plan audit: a plan doesn't depend on whether the rows exist.
_EXAMPLE_INDEX: Final = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
_EXAMPLE_ADMIN: Final = UUID("6f1c2a4e-8b3d-4e5f-9a7b-1c2d3e4f5a60")
_EXAMPLE_PAGE: Final = 51


def _literal(value: str) -> Any:
    return nvarchar_literal(value)


# --- Refusals of a requested setting --------------------------------------------------------

type SettingsRefusalCode = Literal[
    "method_not_found",
    "method_inactive",
    "invalid_method_parameters",
    "unsupported_embedding_model",
    "unsupported_dimensions",
    "unsupported_analyzer",
]


class IndexSettingsRefusedError(Exception):
    """A requested index setting the catalogue doesn't offer (section 8.3 steps 2 to 4): the
    API answers the `422` of `code`. `parameter_errors` are the method parameter model's errors
    (their input left out) for `invalid_method_parameters`. The text is the code alone."""

    def __init__(
        self, code: SettingsRefusalCode, parameter_errors: Sequence[Mapping[str, Any]] = ()
    ) -> None:
        super().__init__(code)
        self.code: SettingsRefusalCode = code
        self.parameter_errors = tuple(parameter_errors)


def _method(connection: Connection, method_key: str, method_version: int | None) -> Any:
    try:
        return resolve_method(connection, method_key, method_version)
    except IngestionRefusedError as refusal:
        code: SettingsRefusalCode = (
            "method_inactive" if refusal.code == "method_inactive" else "method_not_found"
        )
        raise IndexSettingsRefusedError(code) from None


def _parameters(descriptor: MethodDescriptor, parameters: Mapping[str, Any] | None) -> Any:
    try:
        return descriptor.validate_parameters(parameters)
    except ValidationError as invalid:
        errors = invalid.errors(include_url=False, include_input=False, include_context=False)
        raise IndexSettingsRefusedError("invalid_method_parameters", errors) from None


def _embedding(
    catalog: EmbeddingModelCatalog, model: str, dimensions: int | None
) -> tuple[EmbeddingModel, int]:
    entry = catalog.find(model)
    if entry is None:
        raise IndexSettingsRefusedError("unsupported_embedding_model")
    chosen = entry.default_dimensions if dimensions is None else dimensions
    if chosen not in entry.dimensions:
        raise IndexSettingsRefusedError("unsupported_dimensions")
    return entry, chosen


def _analyzer(analyzer: str) -> str:
    if analyzer not in ANALYZERS:
        raise IndexSettingsRefusedError("unsupported_analyzer")
    return analyzer


# --- Rows -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MethodOptionRow:
    """An active row of the method catalogue, its JSON columns parsed."""

    key: str
    version: int
    display_name: str
    description: str
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    supported_content_types: tuple[ContentType, ...]


@dataclass(frozen=True, slots=True)
class IndexSummaryRow:
    id: UUID
    display_name: str
    status: IndexStatus
    method_key: str
    method_version: int
    method_display_name: str
    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str
    live_document_count: int
    updated_at: datetime
    row_version: bytes


@dataclass(frozen=True, slots=True)
class PhysicalIndexRow:
    id: UUID
    generation: int
    schema_version: int
    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str
    state: PhysicalIndexState
    created_at: datetime
    activated_at: datetime | None


@dataclass(frozen=True, slots=True)
class DocumentCounts:
    """`live`: documents not deleted that have a current version; the others: documents not
    deleted by display state."""

    live: int
    ingested: int
    in_progress: int
    failed: int


@dataclass(frozen=True, slots=True)
class RebuildProgress:
    served_current: int
    live_documents: int


@dataclass(frozen=True, slots=True)
class ProjectRefRow:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class OpenRunRow:
    id: UUID
    kind: RunKind
    state: RunState


@dataclass(frozen=True, slots=True)
class IndexDetailRow:
    summary: IndexSummaryRow
    description: str | None
    method_parameters: dict[str, Any]
    active: PhysicalIndexRow | None
    building: PhysicalIndexRow | None
    document_counts: DocumentCounts
    rebuild_progress: RebuildProgress | None
    mapped_projects: tuple[ProjectRefRow, ...]
    open_run: OpenRunRow | None
    created_at: datetime
    created_by: UUID
    updated_by: UUID


@dataclass(frozen=True, slots=True)
class IndexPosition:
    """A keyset position in the index list: the last item's display name and index ID."""

    display_name: str
    index_id: UUID


@dataclass(frozen=True, slots=True)
class IndexFilters:
    """The index list's optional filters: one status (absent means every status but
    `deleted`) and the normalised display-name prefix."""

    status: IndexStatus | None = None
    name_prefix: str | None = None


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else utc(value)


# --- The method catalogue -------------------------------------------------------------------

ACTIVE_METHODS: Final[Select[Any]] = (
    select(
        ingestion_methods.c.method_key,
        ingestion_methods.c.method_version,
        ingestion_methods.c.display_name,
        ingestion_methods.c.description,
        ingestion_methods.c.parameter_schema,
        ingestion_methods.c.default_parameters,
        ingestion_methods.c.supported_content_types,
    )
    .where(ingestion_methods.c.is_active == BIT_TRUE)
    .order_by(ingestion_methods.c.method_key, ingestion_methods.c.method_version.desc())
)
"""The active catalogue rows, by key, then version descending."""


@request_path(example_params={})
async def read_index_options(engine: SqlEngine) -> list[MethodOptionRow]:
    """The active rows of `app.ingestion_methods`."""
    rows = await engine.fetch_all(ACTIVE_METHODS, name=INDEX_OPTIONS)
    return [
        MethodOptionRow(
            key=row.method_key,
            version=row.method_version,
            display_name=row.display_name,
            description=row.description,
            parameter_schema=json.loads(row.parameter_schema),
            default_parameters=json.loads(row.default_parameters),
            supported_content_types=tuple(
                ContentType(value) for value in json.loads(row.supported_content_types)
            ),
        )
        for row in rows
    ]


# --- The index list and detail --------------------------------------------------------------


def _live_count(s: Any) -> Any:
    d = documents.alias("live_d")
    return (
        select(func.count())
        .where(
            d.c.search_index_id == s.c.id,
            d.c.is_deleted == BIT_FALSE,
            d.c.current_version_id.is_not(None),
        )
        .correlate(s)
        .scalar_subquery()
    )


def _index_source() -> tuple[Any, Any, list[Any]]:
    s = search_indexes.alias("s")
    m = ingestion_methods.alias("m")
    source = s.join(
        m, and_(m.c.method_key == s.c.method_key, m.c.method_version == s.c.method_version)
    )
    columns = [
        s.c.id,
        s.c.display_name,
        s.c.description,
        s.c.status,
        s.c.method_key,
        s.c.method_version,
        m.c.display_name.label("method_display_name"),
        s.c.method_parameters,
        s.c.embedding_model,
        s.c.embedding_dimensions,
        s.c.language_analyzer,
        s.c.created_at,
        s.c.created_by,
        s.c.updated_at,
        s.c.updated_by,
        s.c.row_version,
        _live_count(s).label("live_document_count"),
    ]
    return source, s, columns


def _indexes_page(*, status: bool, prefix: bool, cursor: bool) -> Select[Any]:
    source, s, columns = _index_source()
    conditions: list[Any] = [
        s.c.status == bindparam("status", type_=search_indexes.c.status.type)
        if status
        else s.c.status != _literal(IndexStatus.DELETED)
    ]
    if prefix:
        conditions.append(
            s.c.display_name.like(
                bindparam("prefix", type_=search_indexes.c.display_name.type), escape=LIKE_ESCAPE
            )
        )
    if cursor:
        after = bindparam("after_name", type_=search_indexes.c.display_name.type)
        conditions.append(
            or_(
                s.c.display_name > after,
                and_(s.c.display_name == after, s.c.id > bindparam("after_index_id")),
            )
        )
    return top_rows(
        select(*columns).select_from(source).where(*conditions).order_by(s.c.display_name, s.c.id)
    )


INDEX_PAGES: Final[Mapping[tuple[bool, bool, bool], Select[Any]]] = MappingProxyType(
    {
        (status, prefix, cursor): _indexes_page(status=status, prefix=prefix, cursor=cursor)
        for status in (False, True)
        for prefix in (False, True)
        for cursor in (False, True)
    }
)
"""The index list by (status filter, name prefix, cursor), by display name, then index ID."""


def _index_by_id() -> Select[Any]:
    source, s, columns = _index_source()
    return select(*columns).select_from(source).where(s.c.id == bindparam("index_id"))


INDEX_BY_ID: Final = _index_by_id()

PHYSICAL_INDEXES: Final[Select[Any]] = (
    select(
        physical_indexes.c.id,
        physical_indexes.c.generation,
        physical_indexes.c.schema_version,
        physical_indexes.c.embedding_model,
        physical_indexes.c.embedding_dimensions,
        physical_indexes.c.language_analyzer,
        physical_indexes.c.state,
        physical_indexes.c.created_at,
        physical_indexes.c.activated_at,
    )
    .where(
        physical_indexes.c.search_index_id == bindparam("index_id"),
        physical_indexes.c.state.in_(
            [_literal(PhysicalIndexState.ACTIVE), _literal(PhysicalIndexState.BUILDING)]
        ),
    )
    .order_by(physical_indexes.c.generation)
)
"""The logical index's active and building physical indexes (one of each at most)."""


def _document_counts() -> Select[Any]:
    d = documents.alias("d")
    lv = document_versions.alias("lv")
    # The display state holds `EXISTS` subqueries, and SQL Server refuses an aggregate over an
    # expression with a subquery (error 130), so a derived table names it first.
    states = (
        select(
            d.c.current_version_id,
            display_state(d, lv.c.state).label("display_state"),
        )
        .select_from(d.outerjoin(lv, lv.c.id == latest_version_id(d)))
        .where(d.c.search_index_id == bindparam("index_id"), d.c.is_deleted == BIT_FALSE)
        .subquery("states")
    )

    def total(condition: Any) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    return select(
        total(states.c.current_version_id.is_not(None)).label("live"),
        total(states.c.display_state == _literal("ingested")).label("ingested"),
        total(states.c.display_state == _literal("in_progress")).label("in_progress"),
        total(states.c.display_state == _literal("failed")).label("failed"),
    ).select_from(states)


DOCUMENT_COUNTS: Final = _document_counts()
"""The index's live documents and its documents by display state."""


def _served_current() -> Select[Any]:
    d = documents.alias("d")
    sc = served_chunk_sets.alias("sc")
    return select(func.count()).where(
        d.c.search_index_id == bindparam("index_id"),
        d.c.is_deleted == BIT_FALSE,
        d.c.current_version_id.is_not(None),
        exists().where(
            sc.c.document_id == d.c.id,
            sc.c.physical_index_id == bindparam("building_id"),
            sc.c.document_version_id == d.c.current_version_id,
        ),
    )


SERVED_CURRENT: Final = _served_current()
"""The live documents the building index serves for their current version: the repoint
condition's other side (`14-ingestion.md` section 8.9)."""

MAPPED_PROJECTS: Final[Select[Any]] = (
    select(projects.c.id, projects.c.name)
    .select_from(project_indexes.join(projects, projects.c.id == project_indexes.c.project_id))
    .where(project_indexes.c.search_index_id == bindparam("index_id"))
    .order_by(projects.c.name)
    .limit(MAPPED_PROJECTS_MAX)
)
"""The projects that map the index, by name."""

OPEN_RUN: Final[Select[Any]] = (
    select(ingestion_runs.c.id, ingestion_runs.c.kind, ingestion_runs.c.state)
    .where(
        ingestion_runs.c.search_index_id == bindparam("index_id"),
        ingestion_runs.c.state.in_([_literal(state) for state in _OPEN_RUN_STATES]),
    )
    .order_by(ingestion_runs.c.created_at.desc(), ingestion_runs.c.id.desc())
    .limit(1)
)
"""The index's newest `queued` or `running` run."""


def _summary(row: Row[Any]) -> IndexSummaryRow:
    return IndexSummaryRow(
        id=row.id,
        display_name=row.display_name,
        status=IndexStatus(row.status),
        method_key=row.method_key,
        method_version=row.method_version,
        method_display_name=row.method_display_name,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
        live_document_count=int(row.live_document_count),
        updated_at=utc(row.updated_at),
        row_version=bytes(row.row_version),
    )


def _physical(row: Row[Any]) -> PhysicalIndexRow:
    return PhysicalIndexRow(
        id=row.id,
        generation=row.generation,
        schema_version=row.schema_version,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
        state=PhysicalIndexState(row.state),
        created_at=utc(row.created_at),
        activated_at=_optional_utc(row.activated_at),
    )


def read_index_detail(connection: Connection, index_id: UUID) -> IndexDetailRow | None:
    """The index's detail on the caller's connection; `None` when it is missing or
    `deleted`."""
    parameters = {"index_id": index_id}
    row = connection.execute(INDEX_BY_ID, parameters).first()
    if row is None or row.status == IndexStatus.DELETED:
        return None
    summary = _summary(row)
    physical = [_physical(item) for item in connection.execute(PHYSICAL_INDEXES, parameters)]
    active = next((item for item in physical if item.state is PhysicalIndexState.ACTIVE), None)
    building = next((item for item in physical if item.state is PhysicalIndexState.BUILDING), None)
    counts = connection.execute(DOCUMENT_COUNTS, parameters).one()
    progress = None
    if summary.status is IndexStatus.REBUILDING:
        served = 0
        if building is not None:
            served = int(
                connection.execute(
                    SERVED_CURRENT, {"index_id": index_id, "building_id": building.id}
                ).scalar_one()
            )
        progress = RebuildProgress(served_current=served, live_documents=int(counts.live))
    projects_mapped = tuple(
        ProjectRefRow(id=item.id, name=item.name)
        for item in connection.execute(MAPPED_PROJECTS, parameters)
    )
    open_run = connection.execute(OPEN_RUN, parameters).first()
    return IndexDetailRow(
        summary=summary,
        description=row.description,
        method_parameters=json.loads(row.method_parameters),
        active=active,
        building=building,
        document_counts=DocumentCounts(
            live=int(counts.live),
            ingested=int(counts.ingested),
            in_progress=int(counts.in_progress),
            failed=int(counts.failed),
        ),
        rebuild_progress=progress,
        mapped_projects=projects_mapped,
        open_run=(
            None
            if open_run is None
            else OpenRunRow(
                id=open_run.id, kind=RunKind(open_run.kind), state=RunState(open_run.state)
            )
        ),
        created_at=utc(row.created_at),
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _detail(connection: Connection, index_id: UUID) -> IndexDetailRow:
    detail = read_index_detail(connection, index_id)
    if detail is None:
        raise IngestionRefusedError("index_not_found")
    return detail


@request_path(
    example_params={"filters": IndexFilters(), "after": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_indexes(
    engine: SqlEngine,
    *,
    filters: IndexFilters,
    after: IndexPosition | None,
    limit_plus_one: int,
) -> list[IndexSummaryRow]:
    """Up to `limit_plus_one` indexes after `after`, by display name, then index ID.
    `filters.name_prefix` is the normalised prefix, matched case-insensitively by the
    database's collation."""
    parameters: dict[str, object] = {LIMIT_PLUS_ONE: limit_plus_one}
    if filters.status is not None:
        parameters["status"] = filters.status.value
    if filters.name_prefix is not None:
        parameters["prefix"] = like_prefix(filters.name_prefix)
    if after is not None:
        parameters["after_name"] = after.display_name
        parameters["after_index_id"] = after.index_id
    statement = INDEX_PAGES[
        (filters.status is not None, filters.name_prefix is not None, after is not None)
    ]
    rows = await engine.fetch_all(statement, parameters, name=LIST_INDEXES)
    return [_summary(row) for row in rows]


@request_path(example_params={"index_id": _EXAMPLE_INDEX})
async def read_index(engine: SqlEngine, *, index_id: UUID) -> IndexDetailRow | None:
    """The index's detail; `None` when it is missing or `deleted`."""
    return await engine.run(
        lambda connection: read_index_detail(connection, index_id),
        name=READ_INDEX,
        retry="transient",
    )


# --- Locked reads of a write ----------------------------------------------------------------

INDEX_FOR_UPDATE: Final[Select[Any]] = (
    select(
        search_indexes.c.status,
        search_indexes.c.row_version,
        search_indexes.c.display_name,
        search_indexes.c.description,
        search_indexes.c.method_key,
        search_indexes.c.method_version,
        search_indexes.c.method_parameters,
        search_indexes.c.embedding_model,
        search_indexes.c.embedding_dimensions,
        search_indexes.c.language_analyzer,
    )
    .with_hint(search_indexes, LOCK_ROW, "mssql")
    .where(search_indexes.c.id == bindparam("index_id"))
)
"""The logical index under an update lock, for a guarded write."""


@dataclass(frozen=True, slots=True)
class StoredIndex:
    """The settings and state a write compares and changes."""

    status: IndexStatus
    row_version: bytes
    display_name: str
    description: str | None
    method_key: str
    method_version: int
    method_parameters: dict[str, Any]
    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str


def lock_index(connection: Connection, index_id: UUID) -> StoredIndex:
    """The logical index under an update lock; `index_not_found` when it is missing or
    `deleted`."""
    row = connection.execute(INDEX_FOR_UPDATE, {"index_id": index_id}).first()
    if row is None or row.status == IndexStatus.DELETED:
        raise IngestionRefusedError("index_not_found")
    return StoredIndex(
        status=IndexStatus(row.status),
        row_version=bytes(row.row_version),
        display_name=row.display_name,
        description=row.description,
        method_key=row.method_key,
        method_version=row.method_version,
        method_parameters=json.loads(row.method_parameters),
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
    )


# --- Create ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewIndex:
    """`IndexCreate` as the API validated it; `None` takes the catalogue's default."""

    display_name: str
    description: str | None
    method_key: str
    method_version: int | None
    method_parameters: Mapping[str, Any] | None
    embedding_model: str
    embedding_dimensions: int | None
    language_analyzer: str | None


@dataclass(frozen=True, slots=True)
class IndexCreated:
    index: IndexDetailRow
    run_id: UUID
    events: tuple[AuditEvent, ...]


def resolve_new_settings(
    connection: Connection, request: NewIndex, catalog: EmbeddingModelCatalog
) -> IndexSettings:
    """Section 8.3 steps 2 to 4: the method (key and version, `None` the key's highest active
    version) exists and is active, the parameters pass its model (`None` its defaults), the
    model is in the catalogue, the dimensions are among its own (`None` its default) and the
    analyzer is in the allowlist (`None` `standard.lucene`)."""
    descriptor = _method(connection, request.method_key, request.method_version)
    parameters = _parameters(descriptor, request.method_parameters)
    entry, dimensions = _embedding(catalog, request.embedding_model, request.embedding_dimensions)
    analyzer = _analyzer(
        DEFAULT_ANALYZER if request.language_analyzer is None else request.language_analyzer
    )
    return IndexSettings(descriptor, parameters, entry, dimensions, analyzer)


def _create(
    connection: Connection,
    *,
    request: NewIndex,
    catalog: EmbeddingModelCatalog,
    max_indexes: int,
    created_by: UUID,
    trace: TraceContext | None,
) -> IndexCreated:
    settings = resolve_new_settings(connection, request, catalog)
    result = create_index_create_run(
        connection,
        display_name=request.display_name,
        description=request.description,
        settings=settings,
        max_indexes=max_indexes,
        created_by=created_by,
        trace=trace,
    )
    event = AuditEvent(
        action="index.created",
        actor_type="user",
        actor_object_id=created_by,
        target_type="search_index",
        target_id=str(result.index_id),
        metadata=IndexCreatedMetadata(
            method_key=settings.method.key,
            method_version=settings.method.version,
            embedding_model=settings.embedding.model,
            embedding_dimensions=settings.dimensions,
            language_analyzer=settings.analyzer,
            physical_index_id=result.physical_index_id,
            run_id=result.run_id,
        ),
    )
    write_audit_event(connection, event)
    return IndexCreated(
        index=_detail(connection, result.index_id), run_id=result.run_id, events=(event,)
    )


_EXAMPLE_NEW_INDEX: Final = NewIndex(
    display_name="Plan audit",
    description=None,
    method_key="recursive",
    method_version=None,
    method_parameters=None,
    embedding_model="text-embedding-3-large",
    embedding_dimensions=None,
    language_analyzer=None,
)


@request_path(
    example_params={
        "request": _EXAMPLE_NEW_INDEX,
        "catalog": EmbeddingModelCatalog.model_construct(),
        "max_indexes": 200,
        "created_by": _EXAMPLE_ADMIN,
        "trace": None,
    }
)
async def create_index(
    engine: SqlEngine,
    *,
    request: NewIndex,
    catalog: EmbeddingModelCatalog,
    max_indexes: int,
    created_by: UUID,
    trace: TraceContext | None,
) -> IndexCreated:
    """`adminCreateIndex`'s transaction (section 8.3): the catalogue checks
    (`IndexSettingsRefusedError`), the capacity check (`search_index_capacity`), the logical
    index, its first physical index and the `index_create` run, the audit row `index.created`,
    and the detail it answers with. A display name in use fails with `SqlUniqueViolation` on
    `UX_search_indexes_display_name`. It creates a run, so it is never re-run on a transient
    error."""

    def work(connection: Connection) -> IndexCreated:
        return _create(
            connection,
            request=request,
            catalog=catalog,
            max_indexes=max_indexes,
            created_by=created_by,
            trace=trace,
        )

    return await engine.run(work, name=CREATE_INDEX, retry="none")


# --- Update ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexChanges:
    """`IndexUpdate` as the API validated it: `fields` names the members the body holds, and
    only those values count (`description` `None` with `description` in `fields` clears it)."""

    fields: frozenset[IndexField]
    display_name: str | None = None
    description: str | None = None
    method_key: str | None = None
    method_version: int | None = None
    method_parameters: Mapping[str, Any] | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    language_analyzer: str | None = None


@dataclass(frozen=True, slots=True)
class TargetSettings:
    """The settings the index will have after an update."""

    display_name: str
    description: str | None
    method_key: str
    method_version: int
    method_parameters: dict[str, Any]
    method_parameter_hash: bytes | None
    embedding_model: str
    embedding_dimensions: int
    language_analyzer: str


@dataclass(frozen=True, slots=True)
class IndexUpdated:
    """`events` is empty when the body equalled the stored values and nothing was written."""

    index: IndexDetailRow
    events: tuple[AuditEvent, ...] = field(default=())


def _target_method(
    connection: Connection, stored: StoredIndex, changes: IndexChanges
) -> tuple[str, int, dict[str, Any], bytes | None]:
    touched = changes.fields & {"methodKey", "methodVersion", "methodParameters"}
    if not touched:
        return stored.method_key, stored.method_version, stored.method_parameters, None
    key = stored.method_key if changes.method_key is None else changes.method_key
    version: int | None
    if "methodVersion" in changes.fields:
        version = changes.method_version
    elif key != stored.method_key:
        version = None
    else:
        version = stored.method_version
    descriptor = _method(connection, key, version)
    requested: Mapping[str, Any] | None
    if "methodParameters" in changes.fields:
        requested = changes.method_parameters
    elif descriptor.key != stored.method_key:
        # A new method takes its own defaults (section 8.4 item 1).
        requested = None
    else:
        # Another version of the same method reads the stored parameters with its own model,
        # defaults filling what it adds, as a catalogue migration does (section 9.5 item 2).
        requested = stored.method_parameters
    parameters: MethodParameters = _parameters(descriptor, requested)
    return (
        descriptor.key,
        descriptor.version,
        parameters.dumped(),
        descriptor.parameter_hash(parameters),
    )


def resolve_target(
    connection: Connection,
    stored: StoredIndex,
    changes: IndexChanges,
    catalog: EmbeddingModelCatalog,
) -> TargetSettings:
    """Section 8.4 item 1 and section 8.3 steps 2 to 4 against the settings after the update:
    a new method key without a version takes its highest active version and without parameters
    its defaults; parameters alone are validated against the index's method; a new model
    without dimensions takes its default dimensions. Only the settings the body touches are
    checked against the catalogue."""
    key, version, parameters, parameter_hash = _target_method(connection, stored, changes)
    model, dimensions = stored.embedding_model, stored.embedding_dimensions
    if changes.fields & {"embeddingModel", "embeddingDimensions"}:
        model = (
            stored.embedding_model if changes.embedding_model is None else changes.embedding_model
        )
        requested_dimensions: int | None = changes.embedding_dimensions
        if requested_dimensions is None and model == stored.embedding_model:
            requested_dimensions = stored.embedding_dimensions
        _, dimensions = _embedding(catalog, model, requested_dimensions)
    analyzer = stored.language_analyzer
    if "languageAnalyzer" in changes.fields and changes.language_analyzer is not None:
        analyzer = _analyzer(changes.language_analyzer)
    return TargetSettings(
        display_name=stored.display_name if changes.display_name is None else changes.display_name,
        description=changes.description if "description" in changes.fields else stored.description,
        method_key=key,
        method_version=version,
        method_parameters=parameters,
        method_parameter_hash=parameter_hash,
        embedding_model=model,
        embedding_dimensions=dimensions,
        language_analyzer=analyzer,
    )


def changed_fields(stored: StoredIndex, target: TargetSettings) -> tuple[IndexField, ...]:
    """The `IndexUpdate` fields whose values differ from the stored ones, in field order."""
    differs: dict[IndexField, bool] = {
        "displayName": target.display_name != stored.display_name,
        "description": target.description != stored.description,
        "methodKey": target.method_key != stored.method_key,
        "methodVersion": target.method_version != stored.method_version,
        "methodParameters": target.method_parameters != stored.method_parameters,
        "embeddingModel": target.embedding_model != stored.embedding_model,
        "embeddingDimensions": target.embedding_dimensions != stored.embedding_dimensions,
        "languageAnalyzer": target.language_analyzer != stored.language_analyzer,
    }
    return tuple(name for name in FIELD_ORDER if differs[name])


def _guarded_update(
    connection: Connection,
    index_id: UUID,
    row_version: bytes,
    values: Mapping[str, Any],
) -> None:
    changed = connection.execute(
        sql_update(search_indexes)
        .where(search_indexes.c.id == index_id, search_indexes.c.row_version == row_version)
        .values(**values)
    ).rowcount
    if changed == 0:
        # Under the update lock this can't happen; if it does, the re-read decides.
        lock_index(connection, index_id)
        raise IngestionRefusedError("precondition_failed")


def _update(
    connection: Connection,
    *,
    index_id: UUID,
    changes: IndexChanges,
    check_version: VersionCheck,
    catalog: EmbeddingModelCatalog,
    updated_by: UUID,
) -> IndexUpdated:
    stored = lock_index(connection, index_id)
    check_version(stored.row_version)
    target = resolve_target(connection, stored, changes, catalog)
    changed = changed_fields(stored, target)
    settings_changed = SETTINGS_FIELDS.intersection(changed)
    if settings_changed and stored.status not in SETTINGS_CHANGE_STATUSES:
        raise IngestionRefusedError("index_busy")
    if NAME_FIELDS.intersection(changed) and stored.status is IndexStatus.DELETING:
        raise IngestionRefusedError("index_busy")
    if not changed:
        return IndexUpdated(index=_detail(connection, index_id))
    values: dict[str, Any] = {
        "display_name": target.display_name,
        "description": target.description,
        "embedding_model": target.embedding_model,
        "embedding_dimensions": target.embedding_dimensions,
        "language_analyzer": target.language_analyzer,
        "updated_at": SYSUTCDATETIME,
        "updated_by": updated_by,
    }
    if target.method_parameter_hash is not None:
        values |= {
            "method_key": target.method_key,
            "method_version": target.method_version,
            "method_parameters": compact_json(target.method_parameters),
            "method_parameter_hash": target.method_parameter_hash,
        }
    _guarded_update(connection, index_id, stored.row_version, values)
    status = stored.status
    if settings_changed and stored.status is not IndexStatus.FAILED:
        status = compute_index_status(connection, index_id)
        if status is not stored.status:
            connection.execute(
                sql_update(search_indexes)
                .where(search_indexes.c.id == index_id)
                .values(status=status.value, status_changed_at=SYSUTCDATETIME)
            )
    event = AuditEvent(
        action="index.updated",
        actor_type="user",
        actor_object_id=updated_by,
        target_type="search_index",
        target_id=str(index_id),
        metadata=IndexUpdatedMetadata(
            changed_fields=list(changed), status=cast("Any", status.value)
        ),
    )
    write_audit_event(connection, event)
    return IndexUpdated(index=_detail(connection, index_id), events=(event,))


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "changes": IndexChanges(fields=frozenset({"displayName"}), display_name="Plan audit"),
        "check_version": accept_any_version,
        "catalog": EmbeddingModelCatalog.model_construct(),
        "updated_by": _EXAMPLE_ADMIN,
    }
)
async def update_index(
    engine: SqlEngine,
    *,
    index_id: UUID,
    changes: IndexChanges,
    check_version: VersionCheck,
    catalog: EmbeddingModelCatalog,
    updated_by: UUID,
) -> IndexUpdated:
    """`adminUpdateIndex`'s transaction (section 8.4): the locked read (`404`), `check_version`,
    the catalogue checks against the settings after the update (`IndexSettingsRefusedError`),
    `409 index_busy` for a change the status doesn't allow, then nothing for a body equal to the
    stored values, else the guarded update, the status the settings call for (unless `failed`)
    and the audit row `index.updated`. A display name in use fails with `SqlUniqueViolation`."""

    def work(connection: Connection) -> IndexUpdated:
        return _update(
            connection,
            index_id=index_id,
            changes=changes,
            check_version=check_version,
            catalog=catalog,
            updated_by=updated_by,
        )

    return await engine.run(work, name=UPDATE_INDEX, retry="none")


# --- Delete and re-ingest -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndexDeleted:
    """`item_count`: one `delete_physical_index` item per physical index not yet deleted, and
    the `purge_index_blobs` item."""

    index: IndexDetailRow
    run_id: UUID
    item_count: int
    events: tuple[AuditEvent, ...]


def _delete(
    connection: Connection,
    *,
    index_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> IndexDeleted:
    stored = lock_index(connection, index_id)
    check_version(stored.row_version)
    result = create_index_delete_run(
        connection,
        index_id=index_id,
        row_version=stored.row_version,
        requested_by=requested_by,
        trace=trace,
    )
    event = AuditEvent(
        action="index.deleted",
        actor_type="user",
        actor_object_id=requested_by,
        target_type="search_index",
        target_id=str(index_id),
        metadata=IndexDeletedMetadata(
            run_id=result.run_id,
            physical_index_count=result.physical_index_count,
            document_count=result.document_count,
        ),
    )
    write_audit_event(connection, event)
    return IndexDeleted(
        index=_detail(connection, index_id),
        run_id=result.run_id,
        item_count=result.physical_index_count + 1,
        events=(event,),
    )


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "check_version": accept_any_version,
        "requested_by": _EXAMPLE_ADMIN,
        "trace": None,
    }
)
async def delete_index(
    engine: SqlEngine,
    *,
    index_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
) -> IndexDeleted:
    """`adminDeleteIndex`'s transaction (section 7.2; ADR 0052): the locked read (`404`),
    `check_version`, then `409 index_mapped`, `409 index_busy`, the status `deleting` with every
    live document deleted, its queued items cancelled and the `index_delete` run, and the audit
    row `index.deleted`. It creates a run, so it is never re-run on a transient error."""

    def work(connection: Connection) -> IndexDeleted:
        return _delete(
            connection,
            index_id=index_id,
            check_version=check_version,
            requested_by=requested_by,
            trace=trace,
        )

    return await engine.run(work, name=DELETE_INDEX, retry="none")


@dataclass(frozen=True, slots=True)
class ReingestStarted:
    index: IndexDetailRow
    run_id: UUID
    mode: ReingestMode
    item_count: int
    events: tuple[AuditEvent, ...]


def _reingest(
    connection: Connection,
    *,
    index_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
    max_indexes: int,
    catalog: EmbeddingModelCatalog,
) -> ReingestStarted:
    stored = lock_index(connection, index_id)
    check_version(stored.row_version)
    result = create_reingest_run(
        connection,
        index_id=index_id,
        row_version=stored.row_version,
        requested_by=requested_by,
        trace=trace,
        max_indexes=max_indexes,
        catalog=catalog,
    )
    event = AuditEvent(
        action="index.reingest_started",
        actor_type="user",
        actor_object_id=requested_by,
        target_type="search_index",
        target_id=str(index_id),
        metadata=IndexReingestStartedMetadata(
            run_id=result.run_id, mode=cast("Any", result.mode.value), item_count=result.item_count
        ),
    )
    write_audit_event(connection, event)
    return ReingestStarted(
        index=_detail(connection, index_id),
        run_id=result.run_id,
        mode=result.mode,
        item_count=result.item_count,
        events=(event,),
    )


@request_path(
    example_params={
        "index_id": _EXAMPLE_INDEX,
        "check_version": accept_any_version,
        "requested_by": _EXAMPLE_ADMIN,
        "trace": None,
        "max_indexes": 200,
        "catalog": EmbeddingModelCatalog.model_construct(),
    }
)
async def start_reingest(
    engine: SqlEngine,
    *,
    index_id: UUID,
    check_version: VersionCheck,
    requested_by: UUID,
    trace: TraceContext | None,
    max_indexes: int,
    catalog: EmbeddingModelCatalog,
) -> ReingestStarted:
    """`adminStartReingest`'s transaction (sections 8.5 to 8.7): the locked read (`404`),
    `check_version`, then `409 index_busy`, the mode, the capacity check of a rebuild
    (`search_index_capacity`), the `index_reingest` or `index_rebuild` run, and the audit row
    `index.reingest_started`. It creates a run, so it is never re-run on a transient error."""

    def work(connection: Connection) -> ReingestStarted:
        return _reingest(
            connection,
            index_id=index_id,
            check_version=check_version,
            requested_by=requested_by,
            trace=trace,
            max_indexes=max_indexes,
            catalog=catalog,
        )

    return await engine.run(work, name=START_REINGEST, retry="none")
