"""The ingestion ledger's closed sets and the typed values both apps exchange (`14-ingestion.md`
sections 1.3, 3.2, 4.4, 5.1 and 16; `80-data-model.md` sections 2.12 to 2.17, 5.2 and 8).

Every enum mirrors one closed set of the ledger, so a value the API or the ingestion app writes is
always one the table's `CHECK` constraint admits (the unit tests compare each enum with the table
module's tuple). The queue message, the trace context of a run, the dispatcher's and an item's
orchestration inputs, the claim result, an item's warnings and the `ActivityResult` every activity
returns are Pydantic models with camelCase aliases and unknown fields forbidden, because they cross
a process or a Durable history boundary as JSON. The row models read whole rows of the five ledger
tables both apps read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from docchat_core.errors import CODES, ITEM_CODES, ItemCodeKind
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.models import LowerCaseUuid, to_utc

# --- Closed sets ----------------------------------------------------------------------------------


class IndexStatus(StrEnum):
    """`search_indexes.status` (`80-data-model.md` section 8.4)."""

    PROVISIONING = "provisioning"
    READY = "ready"
    REINGEST_REQUIRED = "reingest_required"
    REINGESTING = "reingesting"
    REBUILDING = "rebuilding"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


MAPPABLE_INDEX_STATUSES: Final = frozenset(
    {
        IndexStatus.PROVISIONING,
        IndexStatus.READY,
        IndexStatus.REINGEST_REQUIRED,
        IndexStatus.REINGESTING,
        IndexStatus.REBUILDING,
    }
)
"""The statuses a project can map (`80-data-model.md` section 7.2 rule 3)."""

SETTINGS_CHANGE_STATUSES: Final = frozenset(
    {IndexStatus.READY, IndexStatus.REINGEST_REQUIRED, IndexStatus.FAILED}
)
"""The statuses in which a re-ingest starts, an index is deleted or its settings change
(`14-ingestion.md` sections 7.2, 8.4 and 8.5)."""


class PhysicalIndexState(StrEnum):
    """`physical_indexes.state` (`80-data-model.md` section 8.3)."""

    BUILDING = "building"
    ACTIVE = "active"
    RETIRING = "retiring"
    DELETED = "deleted"


class DocumentVersionState(StrEnum):
    """`document_versions.state` (`80-data-model.md` section 8.1)."""

    UPLOADING = "uploading"
    PENDING = "pending"
    SUPERSEDED = "superseded"
    INGESTING = "ingesting"
    INGESTED = "ingested"
    FAILED = "failed"


ACTIVE_VERSION_STATES: Final = (
    DocumentVersionState.PENDING,
    DocumentVersionState.INGESTING,
    DocumentVersionState.INGESTED,
)
"""The states the two SHA-256 indexes cover, and the versions a delete supersedes."""


class ScanStatus(StrEnum):
    """`document_versions.scan_status` (`80-data-model.md` section 8.1)."""

    NOT_REQUIRED = "not_required"
    AWAITING = "awaiting"
    CLEAN = "clean"
    MALICIOUS = "malicious"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class DocumentPdfStatus(StrEnum):
    """`document_versions.document_pdf_status`: whether `document.pdf` exists."""

    BUILT = "built"
    TOO_LARGE = "too_large"


class ContentType(StrEnum):
    """The three media types a version can have (`CK_document_versions_content_type`)."""

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


CONTENT_TYPE_BY_EXTENSION: Final[Mapping[str, ContentType]] = {
    ".pdf": ContentType.PDF,
    ".docx": ContentType.DOCX,
    ".pptx": ContentType.PPTX,
}
"""A version's media type comes from its name's extension, compared case-insensitively
(`14-ingestion.md` section 2.2 item 3)."""


class ChunkSetState(StrEnum):
    """`chunk_sets.state` (`80-data-model.md` section 8.2)."""

    BUILDING = "building"
    READY = "ready"
    SERVED = "served"
    RETIRED = "retired"


LIVE_CHUNK_SET_STATES: Final = (ChunkSetState.BUILDING, ChunkSetState.READY, ChunkSetState.SERVED)
"""The states `UX_chunk_sets_build_key` covers; a retired set is never reused."""


class RunKind(StrEnum):
    """`ingestion_runs.kind` (`14-ingestion.md` section 2.8)."""

    UPLOAD = "upload"
    DOCUMENT_REINGEST = "document_reingest"
    DOCUMENT_DELETE = "document_delete"
    INDEX_CREATE = "index_create"
    INDEX_REINGEST = "index_reingest"
    INDEX_REBUILD = "index_rebuild"
    INDEX_DELETE = "index_delete"
    MAINTENANCE = "maintenance"


class RunState(StrEnum):
    """`ingestion_runs.state` (`80-data-model.md` section 8.5)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class RunPriority(IntEnum):
    """`ingestion_runs.priority`, copied to each item: shown as `interactive` and `bulk`."""

    INTERACTIVE = 0
    BULK = 1

    @property
    def label(self) -> Literal["interactive", "bulk"]:
        """The name the API and the dispatcher gauge's `priority` dimension use."""
        return "interactive" if self is RunPriority.INTERACTIVE else "bulk"


class ItemOperation(StrEnum):
    """`ingestion_run_items.operation` (`80-data-model.md` section 2.17)."""

    INGEST = "ingest"
    CLEANUP_DOCUMENT = "cleanup_document"
    RERENDER = "rerender"
    CREATE_PHYSICAL_INDEX = "create_physical_index"
    DELETE_PHYSICAL_INDEX = "delete_physical_index"
    PURGE_INDEX_BLOBS = "purge_index_blobs"


DOCUMENT_OPERATIONS: Final = frozenset(
    {ItemOperation.INGEST, ItemOperation.CLEANUP_DOCUMENT, ItemOperation.RERENDER}
)
"""The operations `document_item_orchestrator` runs; the others go to `index_item_orchestrator`."""


class ItemState(StrEnum):
    """`ingestion_run_items.state` (`80-data-model.md` section 8.6)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemStage(StrEnum):
    """`ingestion_run_items.stage`: the last stage an item reached."""

    VALIDATE = "validate"
    EXTRACT = "extract"
    ENRICH = "enrich"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    RENDER = "render"
    FLIP = "flip"
    CLEAN_UP = "clean_up"
    PURGE_DOCUMENT = "purge_document"
    CREATE_INDEX = "create_index"
    DELETE_INDEX = "delete_index"
    PURGE_BLOBS = "purge_blobs"


class ReingestMode(StrEnum):
    """How `adminStartReingest` and `docchat-ops reindex start` rebuild an index (section 8.5)."""

    SAME_INDEX = "same_index"
    NEW_PHYSICAL_INDEX = "new_physical_index"


class CanaryResult(StrEnum):
    """`scan_canaries.result`, and the `result` of the scan-result counter and event."""

    CLEAN = "clean"
    MALICIOUS = "malicious"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class PreflightStatus(StrEnum):
    """A file's status in `adminPreflightUploads` (`14-ingestion.md` section 2.3 item 4)."""

    NEW = "new"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate"
    UPDATE = "update"
    REJECTED = "rejected"


class PreflightRejection(StrEnum):
    """Why preflight rejects a file (the `reason` of a `rejected` result)."""

    INVALID_NAME = "invalid_name"
    UNSUPPORTED_TYPE = "unsupported_type"
    EMPTY_FILE = "empty_file"
    TOO_LARGE = "too_large"
    DUPLICATE_IN_BATCH = "duplicate_in_batch"


class SubmitOutcome(StrEnum):
    """Each requested version's outcome in `adminSubmitUploads` (section 2.6 step 2.3)."""

    QUEUED = "queued"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"
    ALREADY_QUEUED = "already_queued"


# --- Identifiers of the Durable app ---------------------------------------------------------------

DISPATCHER_INSTANCE_ID: Final = "docchat-ingestion-dispatcher"
"""The singleton dispatcher's instance ID, kept by every generation (section 5.1)."""

ITEM_INSTANCE_PATTERN: Final = (
    r"^docchat-item-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[1-9][0-9]{0,9}$"
)

ItemInstanceId = Annotated[str, StringConstraints(pattern=ITEM_INSTANCE_PATTERN)]


def item_instance_id(item_id: UUID, claim_number: int) -> str:
    """`docchat-item-{itemId}-{claimNumber}`: the sub-orchestration's instance ID, which carries
    the claim number so a re-claim never collides with an orphan of an earlier claim."""
    if not isinstance(item_id, UUID):
        raise TypeError("an item ID is a UUID")
    if type(claim_number) is not int or claim_number < 1:
        raise ValueError("a claim number is a positive integer")
    return f"docchat-item-{item_id}-{claim_number}"


# --- Trace context --------------------------------------------------------------------------------

TRACEPARENT_PATTERN: Final = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
TRACESTATE_MAX_LENGTH: Final = 512
TRACESTATE_MEMBER_MAX_LENGTH: Final = 128
"""The W3C cut rule: list-members longer than 128 characters go first (section 3.3 item 2)."""

_TRACEPARENT_RE: Final = re.compile(TRACEPARENT_PATTERN)

Traceparent = Annotated[str, StringConstraints(pattern=TRACEPARENT_PATTERN)]
"""A W3C `traceparent` of version 00: always 55 characters, as `CK_ingestion_runs_traceparent`
requires."""


def truncate_tracestate(value: str | None) -> str | None:
    """Cut a `tracestate` to at most 512 characters by whole list-members, removing members
    longer than 128 characters first and then members from the end; `None` when nothing is
    left.

    Removal stops as soon as the header fits, so a long member that a shorter cut already made
    room for stays. Members are taken as written between commas (empty ones dropped), so the
    result is a valid list whenever the input was.
    """
    if value is None:
        return None
    members = [member.strip() for member in value.split(",")]
    members = [member for member in members if member]

    def size(candidate: list[str]) -> int:
        return len(",".join(candidate))

    # Right-most long members first, so the cut matches "from the end" among the long ones too.
    index = len(members) - 1
    while size(members) > TRACESTATE_MAX_LENGTH and index >= 0:
        if len(members[index]) > TRACESTATE_MEMBER_MAX_LENGTH:
            del members[index]
        index -= 1
    while size(members) > TRACESTATE_MAX_LENGTH:
        members.pop()
    return ",".join(members) or None


@dataclass(frozen=True, slots=True)
class TraceContext:
    """The W3C trace context a run-creating request stores on its run and sends in its message
    (section 3.3); runs created by the sweep or an operator command carry none."""

    traceparent: str
    tracestate: str | None = None

    def __post_init__(self) -> None:
        if not _TRACEPARENT_RE.fullmatch(self.traceparent):
            raise ValueError("not a W3C traceparent of version 00")
        if self.tracestate is not None and not 1 <= len(self.tracestate) <= TRACESTATE_MAX_LENGTH:
            raise ValueError("a stored tracestate holds 1 to 512 characters")

    @classmethod
    def from_carrier(cls, carrier: Mapping[str, str]) -> TraceContext | None:
        """The context a propagator wrote into `carrier`, its `tracestate` cut by the W3C rule;
        `None` without a valid `traceparent`."""
        traceparent = carrier.get("traceparent")
        if traceparent is None or not _TRACEPARENT_RE.fullmatch(traceparent):
            return None
        return cls(traceparent, truncate_tracestate(carrier.get("tracestate")))

    @classmethod
    def current(cls) -> TraceContext | None:
        """The current span's context, injected with `TraceContextTextMapPropagator`, so the
        message's parent is a span of the request's trace; `None` outside a valid span."""
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        return cls.from_carrier(carrier)


# --- Models shared by both apps -------------------------------------------------------------------


class _WireModel(BaseModel):
    """A value that crosses a queue or a Durable history as JSON: camelCase names on the wire,
    unknown fields refused, immutable. Code builds it by field name; JSON from outside the
    process is read by alias only (`validate_wire`)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    @classmethod
    def validate_wire(cls, data: str | bytes) -> Self:
        """Parse JSON text that crossed a queue or a history, accepting camelCase names only,
        so a snake_case key counts as unknown; raises `pydantic.ValidationError`."""
        return cls.model_validate_json(data, by_name=False)


class IngestionQueueMessage(_WireModel):
    """The hint the API sends to `ingestion-requests` after it commits a run (section 3.2;
    ADR 0037). It carries the run ID and trace context only: no name, path, index name, content
    or credential, so no message can choose what work runs."""

    run_id: LowerCaseUuid
    traceparent: Traceparent
    tracestate: Annotated[str, StringConstraints(max_length=TRACESTATE_MAX_LENGTH)] | None = None

    @field_validator("tracestate", mode="before")
    @classmethod
    def _empty_is_absent(cls, value: object) -> object:
        return None if value == "" else value

    @classmethod
    def for_run(cls, run_id: UUID, trace: TraceContext) -> Self:
        """The message for a run the sender just committed with this trace context."""
        return cls(run_id=run_id, traceparent=trace.traceparent, tracestate=trace.tracestate)

    def encode(self) -> str:
        """The compact JSON text the `QueueClient` base64-encodes, `tracestate` left out when
        empty."""
        return self.model_dump_json(exclude_none=True)


MAX_ITEM_WARNINGS: Final = 50
"""`record_item_outcome` keeps the first 50 warnings (`80-data-model.md` section 2.17)."""

_WARNING_CODES: Final = frozenset(
    code for code, entry in ITEM_CODES.items() if entry.kind is ItemCodeKind.WARNING
)


def _warning_code(value: str) -> str:
    if value not in _WARNING_CODES:
        raise ValueError("not an ingestion warning code")
    return value


class ItemWarning(_WireModel):
    """One `{code, page}` entry of an item's `warnings`; it never changes the item's state."""

    code: Annotated[str, AfterValidator(_warning_code)]
    page: Annotated[int, Field(ge=1)] | None = None


_WARNINGS_ADAPTER: Final = TypeAdapter(list[ItemWarning])


def encode_item_warnings(warnings: Sequence[ItemWarning]) -> str:
    """The `ingestion_run_items.warnings` JSON array: the first 50 entries, in order."""
    kept = [warning.model_dump(mode="json") for warning in warnings[:MAX_ITEM_WARNINGS]]
    return json.dumps(kept, separators=(",", ":"))


def decode_item_warnings(text: str | None) -> tuple[ItemWarning, ...]:
    """The warnings a stored `warnings` column holds; `NULL` reads as none."""
    if text is None:
        return ()
    return tuple(_WARNINGS_ADAPTER.validate_json(text))


# --- The typed activity result --------------------------------------------------------------------

LEASE_LOST: Final = "lease_lost"
"""An internal result: a fenced write matched no row because the item was claimed again. It is
never stored or shown, and never retried (`14-ingestion.md` section 17.3)."""

_FAILURE_KINDS: Final = frozenset({ItemCodeKind.FAILURE, ItemCodeKind.FAILURE_AFTER_RETRIES})


def _activity_failure_code(value: str | None) -> str | None:
    if value is None or value == LEASE_LOST:
        return value
    entry = ITEM_CODES.get(value)
    if entry is None or entry.kind not in _FAILURE_KINDS:
        raise ValueError("an activity fails only with an item failure code or lease_lost")
    return value


class ActivityResult(_WireModel):
    """The four fields every activity output holds (section 4.4 rule 2). An expected failure is
    returned with its item code, never raised; `retryable` comes from that code's catalogue row.
    Each activity's output model extends this one with its own fields."""

    ok: bool
    error_code: Annotated[str | None, AfterValidator(_activity_failure_code)] = None
    retryable: bool = False
    retry_after_seconds: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.ok:
            if self.error_code is not None or self.retryable or self.retry_after_seconds:
                raise ValueError("a successful result carries no failure fields")
        elif self.error_code is None:
            raise ValueError("a failed result names its code")
        elif self.retryable != retryable(self.error_code):
            raise ValueError("retryable must follow the code's catalogue row")
        return self

    @classmethod
    def success(cls, **fields: Any) -> Self:
        """An `ok` result with the activity's own fields."""
        return cls(ok=True, **fields)

    @classmethod
    def failure(cls, code: str, *, retry_after_seconds: int | None = None, **fields: Any) -> Self:
        """A failed result for an item failure code (or `lease_lost`), its `retryable` taken
        from the catalogue, with the `Retry-After` the service gave, when it gave one."""
        return cls(
            ok=False,
            error_code=code,
            retryable=retryable(code),
            retry_after_seconds=retry_after_seconds,
            **fields,
        )


def retryable(code: str) -> bool:
    """Whether `call_with_retry` retries a failure code inside the item (section 17.2)."""
    if code == LEASE_LOST:
        return False
    entry = ITEM_CODES.get(code)
    if entry is None or entry.kind not in _FAILURE_KINDS or entry.retried is None:
        raise ValueError("not an item failure code")
    return entry.retried


# --- Orchestration inputs -------------------------------------------------------------------------


class ItemSettings(_WireModel):
    """The item-relevant values of the dispatcher's settings snapshot, taken when the item is
    claimed, so one item runs with one consistent set (section 5.1)."""

    activity_time_budget_seconds: Annotated[int, Field(ge=60, le=1200)]
    max_pages_per_document: Annotated[int, Field(ge=1, le=2000)]
    pagewise_pages_per_extract: Annotated[int, Field(ge=1, le=200)]
    max_parse_seconds_per_document: Annotated[int, Field(ge=900, le=43_200)]
    embedding_batch_max_inputs: Annotated[int, Field(ge=1, le=2048)]
    embedding_batch_max_tokens: Annotated[int, Field(ge=8192, le=300_000)]
    embedding_concurrency: Annotated[int, Field(ge=1, le=8)]
    index_batch_max_documents: Annotated[int, Field(ge=1, le=1000)]
    index_batch_max_bytes: Annotated[int, Field(ge=1_000_000, le=15_000_000)]
    table_summary_concurrency: Annotated[int, Field(ge=1, le=16)]
    table_summary_max_input_tokens: Annotated[int, Field(ge=500, le=20_000)]
    table_summary_max_output_tokens: Annotated[int, Field(ge=50, le=500)]

    @classmethod
    def from_ops_config(cls, config: IngestionOpsConfig) -> Self:
        """The same-named values of the `ingestion` area snapshot."""
        return cls.model_validate({name: getattr(config, name) for name in cls.model_fields})


class RetrySettings(_WireModel):
    """`ItemInput.retry`: the attempts and backoff of `call_with_retry` (section 4.7)."""

    max_attempts: Annotated[int, Field(ge=1, le=10)]
    first_delay_seconds: Annotated[int, Field(ge=1, le=300)]
    max_delay_seconds: Annotated[int, Field(ge=10, le=540)]

    @classmethod
    def from_ops_config(cls, config: IngestionOpsConfig) -> Self:
        return cls(
            max_attempts=config.retry_max_attempts,
            first_delay_seconds=config.retry_first_delay_seconds,
            max_delay_seconds=config.retry_max_delay_seconds,
        )


class ClaimedItem(_WireModel):
    """One item `claim_ingestion_items` claimed (section 4.2), with its run's trace context."""

    item_id: LowerCaseUuid
    claim_number: Annotated[int, Field(ge=1)]
    instance_id: ItemInstanceId
    operation: ItemOperation
    priority: RunPriority
    run_id: LowerCaseUuid
    traceparent: Traceparent | None = None
    tracestate: Annotated[str, StringConstraints(max_length=TRACESTATE_MAX_LENGTH)] | None = None

    @model_validator(mode="after")
    def _instance_matches(self) -> Self:
        if self.instance_id != item_instance_id(self.item_id, self.claim_number):
            raise ValueError("the instance ID names the item and its claim number")
        return self


class ItemInput(_WireModel):
    """The input of a claimed item's sub-orchestration (section 5.1)."""

    item_id: LowerCaseUuid
    claim_number: Annotated[int, Field(ge=1)]
    operation: ItemOperation
    run_id: LowerCaseUuid
    traceparent: Traceparent | None = None
    tracestate: Annotated[str, StringConstraints(max_length=TRACESTATE_MAX_LENGTH)] | None = None
    retry: RetrySettings
    settings: ItemSettings

    @classmethod
    def from_claim(cls, claim: ClaimedItem, settings: IngestionOpsConfig) -> Self:
        """The input built from a claim and the snapshot the dispatcher holds at that wake."""
        return cls(
            item_id=claim.item_id,
            claim_number=claim.claim_number,
            operation=claim.operation,
            run_id=claim.run_id,
            traceparent=claim.traceparent,
            tracestate=claim.tracestate,
            retry=RetrySettings.from_ops_config(settings),
            settings=ItemSettings.from_ops_config(settings),
        )

    def envelope(self) -> ItemEnvelope:
        """The envelope every activity of this item receives beside its own fields."""
        return ItemEnvelope(
            item_id=self.item_id,
            claim_number=self.claim_number,
            run_id=self.run_id,
            traceparent=self.traceparent,
            tracestate=self.tracestate,
            settings=self.settings,
        )


class ItemEnvelope(_WireModel):
    """The fields every activity of an item receives (section 4.4 rule 3); an activity's input
    model extends it with its own fields."""

    item_id: LowerCaseUuid
    claim_number: Annotated[int, Field(ge=1)]
    run_id: LowerCaseUuid
    traceparent: Traceparent | None = None
    tracestate: Annotated[str, StringConstraints(max_length=TRACESTATE_MAX_LENGTH)] | None = None
    settings: ItemSettings


class DispatcherInput(_WireModel):
    """`DispatcherInput {generation, settings}`: the last valid `ingestion` snapshot, `null` for
    generation 0, carried across continue-as-new (section 5.1)."""

    generation: Annotated[int, Field(ge=0)]
    settings: IngestionOpsConfig | None = None


# --- Row models -----------------------------------------------------------------------------------


def _stored_utc(value: object) -> object:
    # `DATETIME2(3)` columns hold UTC without an offset, so a read value is naive.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


StoredTime = Annotated[datetime, BeforeValidator(_stored_utc), AfterValidator(to_utc)]
"""A `DATETIME2(3)` value read from SQL, as an aware UTC instant."""


class _RowModel(BaseModel):
    """A whole row of one ledger table, read with `select(<table>)`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        """The model of a row mapping (`Row._mapping`)."""
        return cls.model_validate(dict(row))


class IngestionRun(_RowModel):
    """A row of `app.ingestion_runs`."""

    id: UUID
    kind: RunKind
    search_index_id: UUID
    priority: RunPriority
    state: RunState
    requested_by: UUID | None
    traceparent: str | None
    tracestate: str | None
    total_items: int | None
    succeeded_items: int | None
    skipped_items: int | None
    failed_items: int | None
    cancelled_items: int | None
    created_at: StoredTime
    started_at: StoredTime | None
    completed_at: StoredTime | None


class RunItem(_RowModel):
    """A row of `app.ingestion_run_items`."""

    id: UUID
    run_id: UUID
    queue_seq: int
    operation: ItemOperation
    search_index_id: UUID
    physical_index_id: UUID | None
    document_id: UUID | None
    document_version_id: UUID | None
    priority: RunPriority
    state: ItemState
    stage: ItemStage | None
    claim_count: int
    instance_id: str | None
    lease_expires_at: StoredTime | None
    not_before: StoredTime | None
    chunk_set_id: UUID | None
    outcome_code: str | None
    outcome_message: str | None
    warnings: str | None
    embedding_tokens: int | None
    summary_input_tokens: int | None
    summary_output_tokens: int | None
    docintel_pages: int | None
    created_at: StoredTime
    started_at: StoredTime | None
    completed_at: StoredTime | None

    def item_warnings(self) -> tuple[ItemWarning, ...]:
        """The parsed `warnings` column."""
        return decode_item_warnings(self.warnings)


class Document(_RowModel):
    """A row of `app.documents`."""

    id: UUID
    search_index_id: UUID
    source_name: str
    normalized_name: str
    current_version_id: UUID | None
    next_version_number: int
    is_deleted: bool
    deleted_at: StoredTime | None
    deleted_by: UUID | None
    created_at: StoredTime
    updated_at: StoredTime
    created_by: UUID
    updated_by: UUID
    row_version: bytes


class DocumentVersion(_RowModel):
    """A row of `app.document_versions`."""

    id: UUID
    document_id: UUID
    search_index_id: UUID
    version_number: int
    sha256: bytes
    size_bytes: int
    content_type: ContentType
    source_name: str
    blob_path: str
    blob_last_modified: StoredTime | None
    page_count: int | None
    uploaded_by: UUID
    uploaded_via_project_id: UUID
    state: DocumentVersionState
    scan_required: bool
    scan_status: ScanStatus
    scan_raw_result: str | None
    scan_time: StoredTime | None
    converter_version: str | None
    converted_sha256: bytes | None
    document_pdf_status: DocumentPdfStatus | None
    failure_code: str | None
    reserved_at: StoredTime
    committed_at: StoredTime | None
    ingest_started_at: StoredTime | None
    ingested_at: StoredTime | None
    superseded_at: StoredTime | None
    failed_at: StoredTime | None
    storage_purged_at: StoredTime | None
    created_at: StoredTime


class ChunkSet(_RowModel):
    """A row of `app.chunk_sets`."""

    id: UUID
    document_id: UUID
    document_version_id: UUID
    physical_index_id: UUID
    method_key: str
    method_version: int
    parameter_hash: bytes
    method_parameters: str
    embedding_model: str
    embedding_dimensions: int
    chunk_count: int | None
    parse_seconds_used: int
    pdf_sha256: bytes | None
    state: ChunkSetState
    rendition_rules_version: int | None
    build_item_id: UUID | None
    created_at: StoredTime
    ready_at: StoredTime | None
    served_at: StoredTime | None
    retired_at: StoredTime | None
    chunks_deleted_at: StoredTime | None


# --- Refusals -------------------------------------------------------------------------------------

type RefusalCode = Literal[
    "index_not_found",
    "document_not_found",
    "precondition_failed",
    "index_busy",
    "index_mapped",
    "index_not_accepting_uploads",
    "search_index_capacity",
    "no_current_version",
    "method_not_found",
    "method_inactive",
    "unsupported_embedding_model",
]


class IngestionRefusedError(Exception):
    """A ledger operation refused by a state the caller can see: the API answers the problem
    of `code` (a code of `docchat_core.errors.CODES`), an operator command its exit code. Raised
    inside a unit of work, it rolls the transaction back, so nothing is half-done."""

    def __init__(self, code: RefusalCode) -> None:
        if code not in CODES:
            raise ValueError("a refusal names a registered problem code")
        # The text is the code alone, so logging the exception can't leak a value.
        super().__init__(code)
        self.code: RefusalCode = code
