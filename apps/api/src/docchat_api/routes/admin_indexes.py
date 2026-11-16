"""The administrator index options and indexes (`11-api.md` sections 1.8, 1.9, 3.10 and 4.7;
`14-ingestion.md` sections 7.2, 8 and 9.6).

`adminGetIndexOptions`, `adminListIndexes`, `adminCreateIndex`, `adminGetIndex`,
`adminUpdateIndex`, `adminDeleteIndex` and `adminStartReingest`. Every route is `admin`, so the
route class refuses a caller without the `Admin` role before any validation or lookup;
administrators act on every index, with no per-project restriction. A `deleted` index answers
`404 index_not_found` everywhere.

The writes read the logical index under an update lock in their transaction and answer, in
order, `404`, then the `If-Match` check (`428`, then `412`), then their catalogue `422`s, then
their state rules (`409`); `docchat_core.sql.repositories.indexes` holds the statements and the
run-creating ones call `docchat_core.ingestion.runs`. A run-creating operation commits first and
then sends one queue message, inside the span `docchat.ingestion.submit`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from pydantic import Field

from docchat_api.http.access import access
from docchat_api.http.audit import report_audit
from docchat_api.http.models import RequestModel, ResponseModel
from docchat_api.http.paging import CursorModel, decode_cursor
from docchat_api.http.preconditions import IfMatchHeader, if_match_header
from docchat_api.http.queue import IngestionQueuePort, RunSubmission, ingestion_queue
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.http.text import MultiLineText, SingleLineText
from docchat_api.routes.admin_common import (
    INDEX_DISPLAY_NAME_MAX_LENGTH,
    ContentTypeValue,
    PageQuery,
    keyset_page,
)
from docchat_api.routes.admin_models import (
    DESCRIPTION_MAX_LENGTH,
    AnalyzerValue,
    DimensionsValue,
    IndexDetail,
    IndexRunStarted,
    IndexStatusFilter,
    IndexSummaryPage,
    ReingestModeValue,
    ReingestRequest,
    index_detail,
    index_summary,
)
from docchat_api.routes.ingestion_problems import (
    DisplayNameTaken,
    EmptyUpdate,
    IndexNotFound,
    is_display_name_taken,
    raise_refusal,
    settings_problem,
)
from docchat_core.audit.models import IndexField
from docchat_core.errors import FieldIssue
from docchat_core.ingestion.models import IndexStatus, IngestionRefusedError, ReingestMode, RunKind
from docchat_core.models import CanonicalUuid
from docchat_core.search.schema import ANALYZER_ALLOWLIST, DEFAULT_ANALYZER
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.repositories import indexes
from docchat_core.sql.repositories.indexes import (
    IndexChanges,
    IndexFilters,
    IndexPosition,
    IndexSettingsRefusedError,
    NewIndex,
)

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-indexes"
DEFAULT_METHOD_KEY: Final = "recursive"
DEFAULT_METHOD_VERSION: Final = 1
DEFAULT_EMBEDDING_MODEL: Final = "text-embedding-3-large"
DEFAULT_EMBEDDING_DIMENSIONS: Final = 1024
ALLOWED_EXTENSIONS: Final = (".pdf", ".docx", ".pptx")

CREATE_REFUSALS: Final = frozenset({"search_index_capacity"})
UPDATE_REFUSALS: Final = frozenset({"index_not_found", "precondition_failed", "index_busy"})
DELETE_REFUSALS: Final = frozenset(
    {"index_not_found", "precondition_failed", "index_mapped", "index_busy"}
)
REINGEST_REFUSALS: Final = frozenset(
    {"index_not_found", "precondition_failed", "index_busy", "search_index_capacity"}
)
CATALOGUE_CODES: Final = (
    "method_not_found",
    "method_inactive",
    "invalid_method_parameters",
    "unsupported_embedding_model",
    "unsupported_dimensions",
    "unsupported_analyzer",
)

Process = Annotated[ApiProcess, Depends(current_process)]
Queue = Annotated[IngestionQueuePort, Depends(ingestion_queue)]
IfMatch = Annotated[IfMatchHeader, Depends(if_match_header)]

MethodKey = Annotated[str, Field(min_length=1, max_length=64)]
MethodVersion = Annotated[int, Field(ge=1, le=1000)]
EmbeddingModelName = Annotated[str, Field(min_length=1, max_length=64)]
Dimensions = Annotated[int, Field(ge=1, le=3072)]
Analyzer = Annotated[str, Field(min_length=1, max_length=32)]
DisplayName = Annotated[str, SingleLineText(1, INDEX_DISPLAY_NAME_MAX_LENGTH)]
Description = Annotated[str, MultiLineText(0, DESCRIPTION_MAX_LENGTH)]


def _absent_when_unset(schema: dict[str, Any]) -> None:
    # An optional member that isn't nullable has no default to state.
    schema.pop("default", None)


def _optional() -> Any:
    # Request fields are non-nullable unless the operation says otherwise (section 1.7): the
    # field keeps its own type, so `null` fails it as any other wrong type does, and only an
    # absent member leaves the unvalidated default. Read such a field through `given`.
    return Field(default=None, json_schema_extra=_absent_when_unset)


def given[T](model: RequestModel, name: str, value: T) -> T | None:
    """`value` when the body holds the member `name` (a field name), else `None`."""
    return value if name in model.model_fields_set else None


# --- adminGetIndexOptions -------------------------------------------------------------------


class MethodOption(ResponseModel):
    key: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[int, Field(ge=1, le=1000)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    supported_content_types: Annotated[list[ContentTypeValue], Field(min_length=1, max_length=3)]


class EmbeddingModelOption(ResponseModel):
    model: Annotated[str, Field(min_length=1, max_length=64)]
    model_version: Annotated[str, Field(min_length=1, max_length=32)]
    dimensions: Annotated[list[DimensionsValue], Field(min_length=1, max_length=3)]
    default_dimensions: DimensionsValue


class LanguageAnalyzerOption(ResponseModel):
    value: AnalyzerValue
    label: Annotated[str, Field(min_length=1, max_length=64)]


class IndexDefaults(ResponseModel):
    method_key: Annotated[str, Field(min_length=1, max_length=64)]
    method_version: Annotated[int, Field(ge=1, le=1000)]
    embedding_model: Annotated[str, Field(min_length=1, max_length=64)]
    embedding_dimensions: DimensionsValue
    language_analyzer: AnalyzerValue


class UploadLimits(ResponseModel):
    max_file_bytes: Annotated[int, Field(ge=1_048_576, le=209_715_200)]
    max_files_per_batch: Annotated[int, Field(ge=1, le=100)]
    allowed_extensions: Annotated[list[str], Field(min_length=3, max_length=3)]


class IndexOptions(ResponseModel):
    methods: Annotated[list[MethodOption], Field(min_length=1, max_length=50)]
    embedding_models: Annotated[list[EmbeddingModelOption], Field(min_length=1, max_length=8)]
    language_analyzers: Annotated[
        list[LanguageAnalyzerOption],
        Field(min_length=len(ANALYZER_ALLOWLIST), max_length=len(ANALYZER_ALLOWLIST)),
    ]
    defaults: IndexDefaults
    upload_limits: UploadLimits


@router.get(
    "/admin/index-options",
    operation_id="adminGetIndexOptions",
    tags=[TAG],
    response_model=IndexOptions,
)
@access("admin")
async def admin_get_index_options(process: Process) -> IndexOptions:
    methods = await indexes.read_index_options(process.sql)
    settings = process.settings
    return IndexOptions(
        methods=[
            MethodOption(
                key=method.key,
                version=method.version,
                display_name=method.display_name,
                description=method.description,
                parameter_schema=method.parameter_schema,
                default_parameters=method.default_parameters,
                supported_content_types=list(method.supported_content_types),
            )
            for method in methods
        ],
        embedding_models=[
            EmbeddingModelOption(
                model=entry.model,
                model_version=entry.model_version,
                dimensions=list(entry.dimensions),
                default_dimensions=entry.default_dimensions,
            )
            for entry in settings.embeddings.openai_embedding_models
        ],
        language_analyzers=[
            LanguageAnalyzerOption(value=option.value, label=option.label)
            for option in ANALYZER_ALLOWLIST
        ],
        defaults=IndexDefaults(
            method_key=DEFAULT_METHOD_KEY,
            method_version=DEFAULT_METHOD_VERSION,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_dimensions=DEFAULT_EMBEDDING_DIMENSIONS,
            language_analyzer=DEFAULT_ANALYZER,
        ),
        upload_limits=UploadLimits(
            max_file_bytes=settings.upload.upload_max_bytes,
            max_files_per_batch=settings.upload.upload_max_files_per_batch,
            allowed_extensions=list(ALLOWED_EXTENSIONS),
        ),
    )


# --- adminListIndexes -----------------------------------------------------------------------


class ListIndexesQuery(PageQuery):
    status: IndexStatusFilter | None = None
    q: DisplayName | None = None


class IndexesCursor(CursorModel):
    """`{"v":1,"n":"<displayName>","i":"<indexId>","s":"<status>" or null,"q":"<q>" or null}`;
    `s` and `q` are the filters it was issued for."""

    n: Annotated[str, Field(min_length=1, max_length=INDEX_DISPLAY_NAME_MAX_LENGTH)]
    i: CanonicalUuid
    s: str | None
    q: str | None


@router.get(
    "/admin/indexes",
    operation_id="adminListIndexes",
    tags=[TAG],
    response_model=IndexSummaryPage,
)
@access("admin")
async def admin_list_indexes(
    query: Annotated[ListIndexesQuery, Query()], process: Process
) -> IndexSummaryPage:
    status = query.status
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(query.cursor, IndexesCursor, filters={"s": status, "q": query.q})
        after = IndexPosition(display_name=cursor.n, index_id=cursor.i)
    rows = await indexes.read_indexes(
        process.sql,
        filters=IndexFilters(
            status=None if status is None else IndexStatus(status), name_prefix=query.q
        ),
        after=after,
        limit_plus_one=query.limit + 1,
    )
    page, cursor_text = keyset_page(
        rows,
        query.limit,
        lambda row: IndexesCursor(n=row.display_name, i=row.id, s=status, q=query.q),
    )
    return IndexSummaryPage(items=[index_summary(row) for row in page], next_cursor=cursor_text)


# --- adminCreateIndex -----------------------------------------------------------------------


class IndexCreate(RequestModel):
    display_name: DisplayName
    description: Description | None = None
    method_key: MethodKey
    method_version: MethodVersion = _optional()
    method_parameters: dict[str, Any] = _optional()
    embedding_model: EmbeddingModelName
    embedding_dimensions: Dimensions = _optional()
    language_analyzer: Analyzer = DEFAULT_ANALYZER


CREATE_EXAMPLE: Final = {
    "displayName": "Policy library",
    "description": "Shared policy and procedure documents",
    "methodKey": "recursive",
    "methodVersion": 1,
    "methodParameters": {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600},
    "embeddingModel": "text-embedding-3-large",
    "embeddingDimensions": 1024,
    "languageAnalyzer": "standard.lucene",
}


@router.post(
    "/admin/indexes",
    operation_id="adminCreateIndex",
    tags=[TAG],
    response_model=IndexRunStarted,
    status_code=202,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": CREATE_EXAMPLE}}}},
)
@access("admin", error_codes=["display_name_taken", "search_index_capacity", *CATALOGUE_CODES])
async def admin_create_index(
    *,
    body: IndexCreate,
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
    queue: Queue,
) -> IndexRunStarted:
    settings = process.settings
    new_index = NewIndex(
        display_name=body.display_name,
        # An empty description is stored as null.
        description=body.description or None,
        method_key=body.method_key,
        method_version=given(body, "method_version", body.method_version),
        method_parameters=given(body, "method_parameters", body.method_parameters),
        embedding_model=body.embedding_model,
        embedding_dimensions=given(body, "embedding_dimensions", body.embedding_dimensions),
        language_analyzer=body.language_analyzer,
    )
    with RunSubmission() as submission:
        try:
            created = await indexes.create_index(
                process.sql,
                request=new_index,
                catalog=settings.embeddings,
                max_indexes=settings.upload.search_max_indexes,
                created_by=principal.oid,
                trace=submission.trace,
            )
        except IndexSettingsRefusedError as refusal:
            raise settings_problem(refusal) from None
        except IngestionRefusedError as refusal:
            raise_refusal(refusal, CREATE_REFUSALS)
        except SqlUniqueViolation as violation:
            if is_display_name_taken(violation):
                raise DisplayNameTaken() from None
            raise
        submission.record(
            index_id=created.index.summary.id,
            run_id=created.run_id,
            kind=RunKind.INDEX_CREATE,
            item_count=1,
        )
        report_audit(request, created.events, admin=True)
        await submission.send(queue, created.run_id)
    return IndexRunStarted(index=index_detail(created.index), run_id=created.run_id)


# --- adminGetIndex --------------------------------------------------------------------------


@router.get(
    "/admin/indexes/{indexId}",
    operation_id="adminGetIndex",
    tags=[TAG],
    response_model=IndexDetail,
)
@access("admin", scoped_params=["indexId"], error_codes=["index_not_found"])
async def admin_get_index(
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")], process: Process
) -> IndexDetail:
    detail = await indexes.read_index(process.sql, index_id=index_id)
    if detail is None:
        raise IndexNotFound()
    return index_detail(detail)


# --- adminUpdateIndex -----------------------------------------------------------------------


class IndexUpdate(RequestModel):
    """Every field of `IndexCreate`, each optional; `description` also takes `null`, which
    clears it."""

    display_name: DisplayName = _optional()
    description: Description | None = None
    method_key: MethodKey = _optional()
    method_version: MethodVersion = _optional()
    method_parameters: dict[str, Any] = _optional()
    embedding_model: EmbeddingModelName = _optional()
    embedding_dimensions: Dimensions = _optional()
    language_analyzer: Analyzer = _optional()

    def changes(self) -> IndexChanges:
        fields = frozenset(
            cast("IndexField", IndexUpdate.model_fields[name].alias)
            for name in self.model_fields_set
        )
        return IndexChanges(
            fields=fields,
            display_name=given(self, "display_name", self.display_name),
            # An empty description is stored as null.
            description=self.description or None,
            method_key=given(self, "method_key", self.method_key),
            method_version=given(self, "method_version", self.method_version),
            method_parameters=given(self, "method_parameters", self.method_parameters),
            embedding_model=given(self, "embedding_model", self.embedding_model),
            embedding_dimensions=given(self, "embedding_dimensions", self.embedding_dimensions),
            language_analyzer=given(self, "language_analyzer", self.language_analyzer),
        )


UPDATE_EXAMPLE: Final = {"description": "Shared policy and procedure documents, reviewed"}


@router.patch(
    "/admin/indexes/{indexId}",
    operation_id="adminUpdateIndex",
    tags=[TAG],
    response_model=IndexDetail,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": UPDATE_EXAMPLE}}}},
)
@access(
    "admin",
    scoped_params=["indexId"],
    error_codes=[
        "index_not_found",
        "display_name_taken",
        "index_busy",
        "empty_update",
        *CATALOGUE_CODES,
    ],
    if_match=True,
)
async def admin_update_index(
    *,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    body: IndexUpdate,
    if_match: IfMatch,
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
) -> IndexDetail:
    if not body.model_fields_set:
        raise EmptyUpdate(errors=[FieldIssue("body", "required")])
    try:
        updated = await indexes.update_index(
            process.sql,
            index_id=index_id,
            changes=body.changes(),
            check_version=if_match.require_row_version,
            catalog=process.settings.embeddings,
            updated_by=principal.oid,
        )
    except IndexSettingsRefusedError as refusal:
        raise settings_problem(refusal) from None
    except IngestionRefusedError as refusal:
        raise_refusal(refusal, UPDATE_REFUSALS)
    except SqlUniqueViolation as violation:
        if is_display_name_taken(violation):
            raise DisplayNameTaken() from None
        raise
    report_audit(request, updated.events, admin=True)
    return index_detail(updated.index)


# --- adminDeleteIndex -----------------------------------------------------------------------


@router.delete(
    "/admin/indexes/{indexId}",
    operation_id="adminDeleteIndex",
    tags=[TAG],
    response_model=IndexRunStarted,
    status_code=202,
)
@access(
    "admin",
    scoped_params=["indexId"],
    error_codes=["index_not_found", "index_mapped", "index_busy"],
    if_match=True,
)
async def admin_delete_index(
    *,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    if_match: IfMatch,
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
    queue: Queue,
) -> IndexRunStarted:
    with RunSubmission() as submission:
        try:
            deleted = await indexes.delete_index(
                process.sql,
                index_id=index_id,
                check_version=if_match.require_row_version,
                requested_by=principal.oid,
                trace=submission.trace,
            )
        except IngestionRefusedError as refusal:
            raise_refusal(refusal, DELETE_REFUSALS)
        submission.record(
            index_id=index_id,
            run_id=deleted.run_id,
            kind=RunKind.INDEX_DELETE,
            item_count=deleted.item_count,
        )
        report_audit(request, deleted.events, admin=True)
        await submission.send(queue, deleted.run_id)
    return IndexRunStarted(index=index_detail(deleted.index), run_id=deleted.run_id)


# --- adminStartReingest ---------------------------------------------------------------------


class IndexReingestStarted(ResponseModel):
    index: IndexDetail
    run_id: UUID
    mode: ReingestModeValue
    item_count: Annotated[int, Field(ge=0, le=2_147_483_647)]


@router.post(
    "/admin/indexes/{indexId}/reingest",
    operation_id="adminStartReingest",
    tags=[TAG],
    response_model=IndexReingestStarted,
    status_code=202,
)
@access(
    "admin",
    scoped_params=["indexId"],
    error_codes=["index_not_found", "index_busy", "search_index_capacity"],
    if_match=True,
)
async def admin_start_reingest(
    *,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    body: ReingestRequest,
    if_match: IfMatch,
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
    queue: Queue,
) -> IndexReingestStarted:
    settings = process.settings
    with RunSubmission() as submission:
        try:
            started = await indexes.start_reingest(
                process.sql,
                index_id=index_id,
                check_version=if_match.require_row_version,
                requested_by=principal.oid,
                trace=submission.trace,
                max_indexes=settings.upload.search_max_indexes,
                catalog=settings.embeddings,
            )
        except IngestionRefusedError as refusal:
            raise_refusal(refusal, REINGEST_REFUSALS)
        rebuild = started.mode is ReingestMode.NEW_PHYSICAL_INDEX
        submission.record(
            index_id=index_id,
            run_id=started.run_id,
            kind=RunKind.INDEX_REBUILD if rebuild else RunKind.INDEX_REINGEST,
            # A rebuild's create item comes before its ingest items.
            item_count=started.item_count + (1 if rebuild else 0),
        )
        report_audit(request, started.events, admin=True)
        await submission.send(queue, started.run_id)
    return IndexReingestStarted(
        index=index_detail(started.index),
        run_id=started.run_id,
        mode=started.mode,
        item_count=started.item_count,
    )
