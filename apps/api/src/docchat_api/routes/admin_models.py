"""The shared schemas of the administrator index, document, upload and run operations
(`11-api.md` sections 4.3 and 4.7; `14-ingestion.md` section 17) and their builders from the
repository rows. One definition of each, because two models with one name break the OpenAPI
schema names; the section 4.3 references and `VersionSummary`, which the upload operations
share, are `admin_common`'s.

Every model is a `ResponseModel`: camelCase members, every field present, `null` where empty.
Closed sets use the ledger's own enums, published inline as the enumerations of section 4.8 so
the document names no schema beyond the models of sections 3 and 4. A row's storage-only values
never leave: no physical index name, no blob path, no trace context, no chunk-set identifier.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import Field, WithJsonSchema

from docchat_api.http.models import RequestModel, ResponseModel
from docchat_api.http.paging import CursorText
from docchat_api.routes.admin_common import (
    DOCUMENT_NAME_MAX_LENGTH,
    INDEX_DISPLAY_NAME_MAX_LENGTH,
    INT_MAX,
    Code,
    ContentTypeValue,
    DocumentRef,
    IndexRef,
    Message,
    ProjectRef,
    VersionSummary,
    inline_enum,
    version_summary,
)
from docchat_core.access import MAX_PAGE_LIMIT
from docchat_core.ingestion.models import (
    IndexStatus,
    ItemOperation,
    ItemStage,
    ItemState,
    PhysicalIndexState,
    ReingestMode,
    RunKind,
    RunState,
)
from docchat_core.models import Count, RowVersion, UtcDateTime, encode_row_version
from docchat_core.search.schema import ANALYZER_ALLOWLIST
from docchat_core.sql.repositories.documents import DocumentDetailRow, DocumentRow, VersionRow
from docchat_core.sql.repositories.indexes import (
    IndexDetailRow,
    IndexSummaryRow,
    PhysicalIndexRow,
)
from docchat_core.sql.repositories.runs import RunItemRow, RunRow

DESCRIPTION_MAX_LENGTH: Final = 1200

IndexStatusValue = Annotated[str, inline_enum(IndexStatus)]
PhysicalIndexStateValue = Annotated[str, inline_enum(PhysicalIndexState)]
RunKindValue = Annotated[str, inline_enum(RunKind)]
RunStateValue = Annotated[str, inline_enum(RunState)]
ItemOperationValue = Annotated[str, inline_enum(ItemOperation)]
ItemStateValue = Annotated[str, inline_enum(ItemState)]
ItemStageValue = Annotated[str, inline_enum(ItemStage)]
ReingestModeValue = Annotated[str, inline_enum(ReingestMode)]

IndexStatusFilter = Literal[
    "provisioning",
    "ready",
    "reingest_required",
    "reingesting",
    "rebuilding",
    "failed",
    "deleting",
    "deleted",
]
RunStateFilter = Literal["queued", "running", "completed", "completed_with_errors", "failed"]
RunKindFilter = Literal[
    "upload",
    "document_reingest",
    "document_delete",
    "index_create",
    "index_reingest",
    "index_rebuild",
    "index_delete",
    "maintenance",
]
ItemStateFilter = Literal["queued", "running", "succeeded", "skipped", "failed", "cancelled"]
"""The query filters: closed sets a request names, validated as enumerations."""
AnalyzerValue = Annotated[
    str,
    WithJsonSchema({"type": "string", "enum": [option.value for option in ANALYZER_ALLOWLIST]}),
]
"""A value of the language analyzer allowlist (section 4.8)."""
DimensionsValue = Annotated[int, WithJsonSchema({"type": "integer", "enum": [256, 1024, 3072]})]
DisplayStateValue = Literal["ingested", "in_progress", "failed", "no_content"]
PriorityValue = Literal["interactive", "bulk"]
DisplayName = Annotated[str, Field(min_length=1, max_length=INDEX_DISPLAY_NAME_MAX_LENGTH)]


class ReingestRequest(RequestModel):
    """The body of `adminStartReingest` and `adminReingestDocument`: exactly `{}`, required and
    holding no member (section 1.7)."""


# --- Indexes --------------------------------------------------------------------------------


class MethodSummary(ResponseModel):
    key: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[int, Field(ge=1, le=1000)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]


class MethodDetail(ResponseModel):
    key: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[int, Field(ge=1, le=1000)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    parameters: dict[str, Any]
    """The validated parameters with defaults filled in."""


class EmbeddingSummary(ResponseModel):
    model: Annotated[str, Field(min_length=1, max_length=64)]
    dimensions: DimensionsValue


class IndexSummary(ResponseModel):
    index_id: UUID
    display_name: DisplayName
    status: IndexStatusValue
    reingest_required: bool
    method: MethodSummary
    embedding: EmbeddingSummary
    language_analyzer: AnalyzerValue
    live_document_count: Count
    updated_at: UtcDateTime
    row_version: RowVersion


class IndexSummaryPage(ResponseModel):
    items: Annotated[list[IndexSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


class PhysicalIndexSummary(ResponseModel):
    generation: Annotated[int, Field(ge=1, le=99_999)]
    schema_version: Annotated[int, Field(ge=1, le=1000)]
    embedding_model: Annotated[str, Field(min_length=1, max_length=64)]
    embedding_dimensions: DimensionsValue
    language_analyzer: AnalyzerValue
    state: PhysicalIndexStateValue
    created_at: UtcDateTime
    activated_at: UtcDateTime | None


class DocumentCounts(ResponseModel):
    live: Count
    ingested: Count
    in_progress: Count
    failed: Count


class RebuildProgress(ResponseModel):
    served_current: Count
    live_documents: Count


class OpenRun(ResponseModel):
    run_id: UUID
    kind: RunKindValue
    state: RunStateValue


class IndexDetail(ResponseModel):
    index_id: UUID
    display_name: DisplayName
    status: IndexStatusValue
    reingest_required: bool
    method: MethodDetail
    embedding: EmbeddingSummary
    language_analyzer: AnalyzerValue
    live_document_count: Count
    updated_at: UtcDateTime
    row_version: RowVersion
    description: Annotated[str, Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)] | None
    active_physical_index: PhysicalIndexSummary | None
    building_physical_index: PhysicalIndexSummary | None
    document_counts: DocumentCounts
    rebuild_progress: RebuildProgress | None
    mapped_projects: Annotated[list[ProjectRef], Field(max_length=200)]
    open_run: OpenRun | None
    created_at: UtcDateTime
    created_by: UUID
    updated_by: UUID


class IndexRunStarted(ResponseModel):
    """`adminCreateIndex` and `adminDeleteIndex`: the index and the run the operation created."""

    index: IndexDetail
    run_id: UUID


def index_summary(row: IndexSummaryRow) -> IndexSummary:
    return IndexSummary(
        index_id=row.id,
        display_name=row.display_name,
        status=row.status,
        reingest_required=row.status is IndexStatus.REINGEST_REQUIRED,
        method=MethodSummary(
            key=row.method_key, version=row.method_version, display_name=row.method_display_name
        ),
        embedding=EmbeddingSummary(model=row.embedding_model, dimensions=row.embedding_dimensions),
        language_analyzer=row.language_analyzer,
        live_document_count=row.live_document_count,
        updated_at=row.updated_at,
        row_version=encode_row_version(row.row_version),
    )


def _physical(row: PhysicalIndexRow | None) -> PhysicalIndexSummary | None:
    if row is None:
        return None
    return PhysicalIndexSummary(
        generation=row.generation,
        schema_version=row.schema_version,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        language_analyzer=row.language_analyzer,
        state=row.state,
        created_at=row.created_at,
        activated_at=row.activated_at,
    )


def index_detail(row: IndexDetailRow) -> IndexDetail:
    summary = row.summary
    counts = row.document_counts
    progress = row.rebuild_progress
    return IndexDetail(
        index_id=summary.id,
        display_name=summary.display_name,
        status=summary.status,
        reingest_required=summary.status is IndexStatus.REINGEST_REQUIRED,
        method=MethodDetail(
            key=summary.method_key,
            version=summary.method_version,
            display_name=summary.method_display_name,
            parameters=row.method_parameters,
        ),
        embedding=EmbeddingSummary(
            model=summary.embedding_model, dimensions=summary.embedding_dimensions
        ),
        language_analyzer=summary.language_analyzer,
        live_document_count=summary.live_document_count,
        updated_at=summary.updated_at,
        row_version=encode_row_version(summary.row_version),
        description=row.description or None,
        active_physical_index=_physical(row.active),
        building_physical_index=_physical(row.building),
        document_counts=DocumentCounts(
            live=counts.live,
            ingested=counts.ingested,
            in_progress=counts.in_progress,
            failed=counts.failed,
        ),
        rebuild_progress=(
            None
            if progress is None
            else RebuildProgress(
                served_current=progress.served_current, live_documents=progress.live_documents
            )
        ),
        mapped_projects=[
            ProjectRef(project_id=project.id, name=project.name) for project in row.mapped_projects
        ],
        open_run=(
            None
            if row.open_run is None
            else OpenRun(run_id=row.open_run.id, kind=row.open_run.kind, state=row.open_run.state)
        ),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


# --- Documents ------------------------------------------------------------------------------


class DocumentSummary(ResponseModel):
    document_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=DOCUMENT_NAME_MAX_LENGTH)]
    content_type: ContentTypeValue | None
    display_state: DisplayStateValue
    current_version: VersionSummary | None
    latest_version: VersionSummary | None
    updated_at: UtcDateTime
    row_version: RowVersion


class DocumentSummaryPage(ResponseModel):
    items: Annotated[list[DocumentSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


def _optional_version(row: VersionRow | None) -> VersionSummary | None:
    return None if row is None else version_summary(row)


def document_summary(row: DocumentRow) -> DocumentSummary:
    latest = row.latest_version
    return DocumentSummary(
        document_id=row.id,
        name=row.source_name,
        content_type=None if latest is None else latest.content_type,
        display_state=row.display_state,
        current_version=_optional_version(row.current_version),
        latest_version=_optional_version(latest),
        updated_at=row.updated_at,
        row_version=encode_row_version(row.row_version),
    )


# --- Runs -----------------------------------------------------------------------------------


class RunCounts(ResponseModel):
    total: Count
    queued: Count
    running: Count
    succeeded: Count
    skipped: Count
    failed: Count
    cancelled: Count


class RunSummary(ResponseModel):
    run_id: UUID
    kind: RunKindValue
    state: RunStateValue
    index: IndexRef
    priority: PriorityValue
    requested_by: UUID | None
    requested_by_name: Annotated[str, Field(max_length=256)] | None
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    completed_at: UtcDateTime | None
    counts: RunCounts


class RunSummaryPage(ResponseModel):
    items: Annotated[list[RunSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


class RunItemTarget(ResponseModel):
    generation: Annotated[int, Field(ge=1, le=99_999)]
    state: PhysicalIndexStateValue


class RunItemWarning(ResponseModel):
    code: Code
    page: Annotated[int, Field(ge=1)] | None


class RunItemUsage(ResponseModel):
    embedding_tokens: Count | None
    summary_input_tokens: Count | None
    summary_output_tokens: Count | None
    doc_intel_pages: Count | None


class RunItem(ResponseModel):
    item_id: UUID
    operation: ItemOperationValue
    document: DocumentRef | None
    version_number: Annotated[int, Field(ge=1, le=INT_MAX)] | None
    target: RunItemTarget | None
    state: ItemStateValue
    stage: ItemStageValue | None
    attempts: Annotated[int, Field(ge=0, le=1000)]
    outcome_code: Code | None
    outcome_message: Message | None
    warnings: Annotated[list[RunItemWarning], Field(max_length=50)]
    usage: RunItemUsage
    created_at: UtcDateTime
    started_at: UtcDateTime | None
    completed_at: UtcDateTime | None


class DocumentDetail(ResponseModel):
    document_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=DOCUMENT_NAME_MAX_LENGTH)]
    content_type: ContentTypeValue | None
    display_state: DisplayStateValue
    current_version: VersionSummary | None
    latest_version: VersionSummary | None
    updated_at: UtcDateTime
    row_version: RowVersion
    versions: Annotated[list[VersionSummary], Field(max_length=100)]
    recent_items: Annotated[list[RunItem], Field(max_length=20)]


def run_summary(row: RunRow) -> RunSummary:
    counts = row.counts
    return RunSummary(
        run_id=row.id,
        kind=row.kind,
        state=row.state,
        index=IndexRef(index_id=row.index_id, display_name=row.index_display_name),
        priority=row.priority.label,
        requested_by=row.requested_by,
        requested_by_name=row.requested_by_name,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        counts=RunCounts(
            total=counts.total,
            queued=counts.queued,
            running=counts.running,
            succeeded=counts.succeeded,
            skipped=counts.skipped,
            failed=counts.failed,
            cancelled=counts.cancelled,
        ),
    )


def run_item(row: RunItemRow) -> RunItem:
    document = None
    if row.document_id is not None and row.document_name is not None:
        document = DocumentRef(document_id=row.document_id, name=row.document_name)
    return RunItem(
        item_id=row.id,
        operation=row.operation,
        document=document,
        version_number=row.version_number,
        target=(
            None
            if row.target is None
            else RunItemTarget(generation=row.target.generation, state=row.target.state)
        ),
        state=row.state,
        stage=row.stage,
        attempts=row.attempts,
        outcome_code=row.outcome_code,
        outcome_message=row.outcome_message,
        warnings=[RunItemWarning(code=warning.code, page=warning.page) for warning in row.warnings],
        usage=RunItemUsage(
            embedding_tokens=row.embedding_tokens,
            summary_input_tokens=row.summary_input_tokens,
            summary_output_tokens=row.summary_output_tokens,
            doc_intel_pages=row.docintel_pages,
        ),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def document_detail(row: DocumentDetailRow) -> DocumentDetail:
    summary = document_summary(row.document)
    return DocumentDetail(
        **summary.model_dump(),
        versions=[version_summary(version) for version in row.versions],
        recent_items=[run_item(item) for item in row.recent_items],
    )
