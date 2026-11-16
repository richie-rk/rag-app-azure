"""What the document activities share (`14-ingestion.md` sections 4.4 to 4.9, 6.2 and 6.10; ADRs
0034, 0050 and 0144): the build plan Validate returns and every later step receives, the
services one worker process holds for them, the run wrapper (failure boundary, deadline, span
and stage duration), the fenced reads every step starts with, and the Blob and Search helpers
whose failures map to item codes.

An expected failure is raised inside a step as `ItemFailure` (or the method's
`MethodFailedError`) and leaves the step as a typed result with its catalogue code; a lost claim
is `LeaseLost`; SQL out of reach after the data-access layer's retries is
`database_unavailable`. Anything else is unexpected and reaches the failure boundary, which
records it once by type and returns the retryable `internal_error`.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Final, Literal, Protocol, Self
from uuid import UUID

import azure.durable_functions as df
from azure.core.exceptions import AzureError, ResourceNotFoundError
from opentelemetry.trace import Span
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Connection, select, update

from docchat_core.ingestion.methods import (
    MethodContext,
    MethodFailedError,
    MethodRegistry,
    MethodSnapshot,
    RegisteredMethod,
)
from docchat_core.ingestion.models import (
    LEASE_LOST,
    ActivityResult,
    ContentType,
    ItemEnvelope,
    ItemStage,
)
from docchat_core.ingestion.names import (
    TITLE_MAX_LENGTH,
    BlobContainer,
    converted_path,
    rendition_document_path,
    rendition_marker_path,
    validate_blob_path,
    validate_blob_prefix,
    work_converted_path,
    work_prefix,
)
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.models import Clock, LowerCaseUuid, Sha256Hex
from docchat_core.search.schema import CHUNK_INDEX_SCHEMA_VERSION, ChunkIndexFields, chunk_keys
from docchat_core.sql.engine import SqlUnavailable
from docchat_core.sql.tables.ingestion import chunk_sets, document_versions, physical_indexes
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion import telemetry
from docchat_ingestion.activities.runtime import SqlRunner
from docchat_ingestion.activities.support import (
    WireModel,
    fence_lease,
    item_span,
    literal,
    literals,
    record_outcome,
    wire,
)
from docchat_ingestion.boundary import INTERNAL_ERROR, guard, run_activity
from docchat_ingestion.isolation.runner import ParsePool

DEADLINES: Final = {
    "validate_item": 120,
    "extract_document": 2700,
    "enrich_document": 1560,
    "chunk_document": 660,
    "embed_chunks": 1560,
    "index_chunks": 1200,
    "render_pages": 2400,
    "rerender_pages": 2400,
    "flip_chunk_set": 120,
    "clean_up_item": 120,
    "record_item_outcome": 120,
    "purge_document_artifacts": 1560,
}
"""The deadline of each document activity (section 4.6), every one below `functionTimeout`."""

MAX_RESUMABLE_CALLS: Final = 20
"""Enrich, Embed and `purge_document_artifacts` are called at most 20 times (section 4.6)."""

CHUNK_LIMIT: Final = 100_000
BLOB_DELETE_BATCH: Final = 256
CHUNK_DELETE_BATCH: Final = 500
DELETED_STATUSES: Final = frozenset({200, 202, 404})
"""A batch-delete sub-response that counts as deleted, an absent blob included."""

PDF_CONTENT_TYPE: Final = "application/pdf"
DATABASE_UNAVAILABLE: Final = "database_unavailable"
STORAGE_UNAVAILABLE: Final = "storage_unavailable"
SOURCE_MISSING: Final = "source_missing"
SOURCE_CHANGED: Final = "source_changed"
OFFICE_TYPES: Final = frozenset({ContentType.DOCX, ContentType.PPTX})

type ActivityOutput = dict[str, Any]


# --- Expected failures ----------------------------------------------------------------------------


class ItemFailure(Exception):  # noqa: N818 - an expected outcome, returned as a typed result
    """An expected failure of a step, with its item code and the service's `Retry-After`. The
    text is the code alone, so it can never carry a value."""

    def __init__(self, code: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class LeaseLost(Exception):  # noqa: N818 - the internal result of section 17.3
    """A fenced write matched no row: the item belongs to a later claim or to the sweep."""

    def __init__(self) -> None:
        super().__init__(LEASE_LOST)


# --- The build plan -------------------------------------------------------------------------------


class ConversionPlan(WireModel):
    """How a DOCX or PPTX version reaches its PDF (section 6.2 decision 8)."""

    convert: bool
    converted_blob_path: str
    converted_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        validate_blob_path(BlobContainer.DOCUMENTS, self.converted_blob_path)
        if self.convert == (self.converted_sha256 is not None):
            raise ValueError("a reused PDF names its SHA-256; a conversion names none")
        return self


class PhysicalIndexPlan(WireModel):
    """The target physical index as Validate read it."""

    id: LowerCaseUuid
    name: str
    is_active: bool
    schema_version: int = Field(ge=1)
    dimensions: int = Field(ge=1)
    encoding: TokenEncoding
    max_input_tokens: int = Field(ge=1)
    ingestion_deployment: str = Field(min_length=1, max_length=64)


class BuildPlan(WireModel):
    """`ValidatePlan` with action `build`: every step after Validate receives it, so one item
    builds with one consistent snapshot of the rows it depends on."""

    action: Literal["build"] = "build"
    skip_code: None = None
    chunk_set_id: LowerCaseUuid
    index_id: LowerCaseUuid
    document_id: LowerCaseUuid
    version_id: LowerCaseUuid
    version_number: int = Field(ge=1)
    content_type: ContentType
    source_blob_path: str
    source_sha256: Sha256Hex
    conversion: ConversionPlan | None
    title: str = Field(max_length=TITLE_MAX_LENGTH)
    work_prefix: str
    physical_index: PhysicalIndexPlan
    method: MethodSnapshot

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        validate_blob_path(BlobContainer.DOCUMENTS, self.source_blob_path)
        validate_blob_prefix(BlobContainer.INGESTION_WORK, self.work_prefix)
        if (self.conversion is not None) != self.is_office:
            raise ValueError("a DOCX or PPTX version, and only one, has a conversion plan")
        if self.conversion is not None and self.conversion.converted_blob_path != converted_path(
            self.index_id, self.document_id, self.version_number
        ):
            raise ValueError("the converted PDF belongs to this version")
        return self

    @property
    def is_office(self) -> bool:
        return self.content_type in OFFICE_TYPES

    def ids(self) -> dict[str, UUID]:
        """The plan's identifiers as span attributes."""
        return {
            "docchat.document_id": self.document_id,
            "docchat.version_id": self.version_id,
            "docchat.chunk_set_id": self.chunk_set_id,
            "docchat.physical_index_id": self.physical_index.id,
        }

    def work_pdf_path(self) -> str:
        return work_converted_path(self.work_prefix)

    def marker_path(self) -> str:
        return rendition_marker_path(self.index_id, self.document_id, self.version_number)

    def document_pdf_path(self) -> str:
        return rendition_document_path(self.index_id, self.document_id, self.version_number)


def check_work_prefix(run_id: UUID, plan: BuildPlan) -> None:
    """A plan's work prefix belongs to the run that carries it (`work_prefix()`), so two runs
    never share a work file."""
    if plan.work_prefix != work_prefix(run_id, plan.document_id, plan.version_number):
        raise ValueError("the plan's work prefix belongs to another run")


class PlanInput(ItemEnvelope):
    """The input of a step after Validate: the item envelope and the plan."""

    plan: BuildPlan


class PartRange(WireModel):
    """`{firstPage, lastPage}`: pages of the version's PDF, 1-based and inclusive."""

    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last_page < self.first_page:
            raise ValueError("a part runs from its first page to its last")
        return self


# --- Ports ----------------------------------------------------------------------------------------


class Downloader(Protocol):
    """A started download (`StorageStreamDownloader`)."""

    def chunks(self) -> AsyncIterator[bytes]: ...

    async def readall(self) -> bytes: ...


class BlobItem(Protocol):
    @property
    def name(self) -> str: ...


class BlobContainerPort(Protocol):
    """A container of the data account, seen through the calls the document steps make."""

    def upload_blob(self, name: str, data: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def download_blob(self, blob: str, **kwargs: Any) -> Awaitable[Downloader]: ...

    def delete_blob(self, blob: str, **kwargs: Any) -> Awaitable[None]: ...

    def list_blobs(self, **kwargs: Any) -> AsyncIterator[BlobItem]: ...

    def delete_blobs(self, *blobs: str, **kwargs: Any) -> Awaitable[AsyncIterator[Any]]: ...


class SearchDocumentsPort(Protocol):
    """One physical index's `SearchClient`, seen through its document calls."""

    def upload_documents(
        self, documents: list[dict[str, Any]], **kwargs: Any
    ) -> Awaitable[Any]: ...

    def delete_documents(
        self, documents: list[dict[str, Any]], **kwargs: Any
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentServices:
    """One worker process's resources for the document steps, created once at the first
    invocation (section 4.4 rule 4); per-document facts travel only in inputs and results."""

    sql: SqlRunner
    documents: BlobContainerPort
    renditions: BlobContainerPort
    work: BlobContainerPort
    search: Callable[[str], SearchDocumentsPort]
    openai: Any
    parse_pool: ParsePool
    methods: MethodRegistry
    clock: Clock
    rendition_rules_version: int
    converter_version: str
    workload_principal_id: UUID | None = field(default=None)


type ServicesProvider = Callable[[], Awaitable[DocumentServices]]


@dataclass(slots=True)
class _ServicesSlot:
    services: DocumentServices | None = None
    lock: asyncio.Lock | None = None


_SLOT: Final = _ServicesSlot()


async def _build_services() -> DocumentServices:
    # The process's registry, SQL engine and containers belong to the shared runtime, so a
    # serving process holds one credential and one SQL pool (section 4.4 rule 4).
    from docchat_core.clients import ClientRegistry  # noqa: PLC0415
    from docchat_core.ingestion.settings import IngestionAppSettings  # noqa: PLC0415
    from docchat_core.settings import load_settings  # noqa: PLC0415
    from docchat_ingestion.activities.runtime import method_registry, runtime  # noqa: PLC0415
    from docchat_ingestion.isolation.convert import CONVERTER_VERSION  # noqa: PLC0415

    process = await runtime()
    registry = ClientRegistry.current()
    settings = load_settings(IngestionAppSettings)
    pool = ParsePool(
        slots_per_process=settings.ingestion_parse_slots_per_process,
        memory_limit_mb=settings.ingestion_parse_memory_limit_mb,
        parse_timeout_seconds=settings.ingestion_parse_timeout_seconds,
    )
    return DocumentServices(
        sql=process.sql,
        documents=registry.blob_container(BlobContainer.DOCUMENTS),
        renditions=registry.blob_container(BlobContainer.RENDITIONS),
        work=registry.blob_container(BlobContainer.INGESTION_WORK),
        search=registry.search_client,
        openai=registry.openai,
        parse_pool=pool,
        methods=method_registry(),
        clock=process.clock,
        rendition_rules_version=process.rendition_rules_version,
        converter_version=CONVERTER_VERSION,
        workload_principal_id=process.workload_principal_id,
    )


async def document_services() -> DocumentServices:
    """The process's document services, created by the first invocation that asks."""
    if _SLOT.lock is None:
        _SLOT.lock = asyncio.Lock()
    async with _SLOT.lock:
        if _SLOT.services is None:
            _SLOT.services = await _build_services()
        return _SLOT.services


# --- Running a step -------------------------------------------------------------------------------


def parse_input[M: BaseModel](model: type[M], payload: object) -> M:
    """An activity's input, read by its camelCase names only."""
    return model.model_validate(payload, by_alias=True, by_name=False)


def method_key_of(model: BaseModel) -> str | None:
    plan = getattr(model, "plan", None)
    return plan.method.key if isinstance(plan, BuildPlan) else None


async def run_document_activity[M: ItemEnvelope](
    name: str,
    payload: object,
    model: type[M],
    body: Callable[[DocumentServices, M, Span], Awaitable[ActivityResult]],
    *,
    stage: ItemStage,
    provider: ServicesProvider,
    ids: Callable[[M], dict[str, UUID]] | None = None,
    failure: Callable[[str, int | None], ActivityResult] | None = None,
) -> ActivityOutput:
    """One document step inside the failure boundary, its deadline and its span
    `docchat.ingestion.activity`, parented to the run's trace (sections 4.5, 4.6 and 4.9).

    It is `support.run_item_activity` for inputs that extend the item envelope (the plan, a part,
    a Flip's output), with two additions: `failure` builds the step's own result type for a
    failure code, so every output carries the step's fields as well as the four `ActivityResult`
    fields, and each call records `docchat.ingestion.stage.duration`. The boundary and the
    deadline are `boundary.run_activity`.
    """
    make_failure = failure or (lambda code, after: ActivityResult.failure(code))
    parsed: M | None = None
    async with guard(name):
        parsed = parse_input(model, payload)
    if parsed is None:
        return wire(make_failure(INTERNAL_ERROR, None))
    item = parsed
    attributes: dict[str, Any] = {} if ids is None else ids(item)
    with item_span(name, item, **attributes) as span:

        async def run_body() -> ActivityResult:
            services = await provider()
            started = services.clock.monotonic()
            try:
                return await _expected(lambda: body(services, item, span), make_failure)
            finally:
                elapsed = services.clock.monotonic() - started
                telemetry.record_stage(stage, method_key_of(item), elapsed)

        result = await run_activity(
            name, DEADLINES[name], run_body, lambda code: make_failure(code, None)
        )
        record_outcome(span, result)
        return wire(result)


async def _expected(
    work: Callable[[], Awaitable[ActivityResult]],
    make_failure: Callable[[str, int | None], ActivityResult],
) -> ActivityResult:
    """The step's result, with its expected failures turned into typed results."""
    try:
        return await work()
    except LeaseLost:
        return make_failure(LEASE_LOST, None)
    except ItemFailure as failure:
        return make_failure(failure.code, failure.retry_after_seconds)
    except MethodFailedError as failure:
        return make_failure(failure.code, failure.retry_after_seconds)
    except SqlUnavailable:
        return make_failure(DATABASE_UNAVAILABLE, None)


def blueprint(function: Callable[[object], Awaitable[ActivityOutput]]) -> df.Blueprint:
    """The Durable blueprint that registers one activity under its function's name."""
    bp = df.Blueprint()
    bp.activity_trigger(input_name="payload")(function)
    return bp


# --- Bounded concurrency --------------------------------------------------------------------------


def _first_failure(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The exception a failed task group stands for: its first expected failure, or else its
    first exception, so the boundary records one type and never a group."""
    leaves: list[BaseException] = []
    pending: list[BaseException] = [group]
    while pending:
        current = pending.pop(0)
        if isinstance(current, BaseExceptionGroup):
            pending[:0] = list(current.exceptions)
        else:
            leaves.append(current)
    expected = (ItemFailure, LeaseLost, MethodFailedError, SqlUnavailable)
    for leaf in leaves:
        if isinstance(leaf, expected):
            return leaf
    return leaves[0]


async def run_bounded[T](
    items: Iterable[T], limit: int, work: Callable[[T], Awaitable[None]]
) -> None:
    """Run `work` for every item with at most `limit` in flight; the first failure cancels the
    others and is raised alone."""
    gate = asyncio.Semaphore(limit)

    async def one(value: T) -> None:
        async with gate:
            await work(value)

    try:
        async with asyncio.TaskGroup() as group:
            for value in items:
                group.create_task(one(value))
    except BaseExceptionGroup as failures:
        raise _first_failure(failures) from None


# --- Fenced reads ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildFacts:
    """What a step reads about its build after the fenced lease update."""

    page_count: int | None
    pdf_sha256: str | None
    chunk_count: int | None
    parse_seconds_used: int
    chunk_set_state: str


def fence(connection: Connection, envelope: ItemEnvelope, stage: ItemStage, deadline: int) -> None:
    """The fenced lease update of section 4.8 rule 2; a lost claim raises `LeaseLost`."""
    if not fence_lease(connection, envelope.item_id, envelope.claim_number, stage, deadline):
        raise LeaseLost


def read_build(connection: Connection, plan: BuildPlan) -> BuildFacts:
    """The version's page count and the chunk set's PDF hash, count and parse budget."""
    row = connection.execute(
        select(
            document_versions.c.page_count,
            chunk_sets.c.pdf_sha256,
            chunk_sets.c.chunk_count,
            chunk_sets.c.parse_seconds_used,
            chunk_sets.c.state,
        )
        .select_from(chunk_sets)
        .join(document_versions, document_versions.c.id == chunk_sets.c.document_version_id)
        .where(chunk_sets.c.id == plan.chunk_set_id)
    ).one()
    sha = row.pdf_sha256
    return BuildFacts(
        page_count=row.page_count,
        pdf_sha256=None if sha is None else bytes(sha).hex(),
        chunk_count=row.chunk_count,
        parse_seconds_used=row.parse_seconds_used,
        chunk_set_state=row.state,
    )


async def fence_and_read(
    services: DocumentServices, item: PlanInput, stage: ItemStage, deadline: int
) -> BuildFacts:
    """Every step after Validate starts here: the fenced lease update, then its build facts."""

    def work(connection: Connection) -> BuildFacts:
        fence(connection, item, stage, deadline)
        return read_build(connection, item.plan)

    return await services.sql.run(work, name=f"{stage.value}_fence", retry="transient")


async def fence_only(
    services: DocumentServices, envelope: ItemEnvelope, stage: ItemStage, deadline: int
) -> None:
    def work(connection: Connection) -> None:
        fence(connection, envelope, stage, deadline)

    await services.sql.run(work, name=f"{stage.value}_fence", retry="transient")


def registered(services: DocumentServices, plan: BuildPlan) -> RegisteredMethod:
    """The implementation the plan's method snapshot names."""
    return services.methods.get(plan.method.key, plan.method.version)


def pdf_location(plan: BuildPlan, facts: BuildFacts) -> tuple[BlobContainer, str, str]:
    """Where the build's PDF is and the bytes expected there: a PDF version's source, or the work
    copy of the PDF converted from a DOCX or PPTX source (section 6.3 steps 1 and 4)."""
    if plan.is_office:
        if facts.pdf_sha256 is None:
            raise ValueError("a DOCX or PPTX build reads its PDF only after the conversion call")
        return BlobContainer.INGESTION_WORK, plan.work_pdf_path(), facts.pdf_sha256
    return BlobContainer.DOCUMENTS, plan.source_blob_path, plan.source_sha256


def method_context(
    item: PlanInput, facts: BuildFacts, *, page_count: int | None = None
) -> MethodContext:
    """What an implementation receives about the build: paths, IDs and settings only."""
    plan = item.plan
    container, path, sha = pdf_location(plan, facts)
    pages = page_count if page_count is not None else facts.page_count
    if pages is None:
        raise ValueError("the version's page count is recorded by Extract")
    return MethodContext(
        run_id=item.run_id,
        item_id=item.item_id,
        index_id=plan.index_id,
        document_id=plan.document_id,
        version_id=plan.version_id,
        version_number=plan.version_number,
        chunk_set_id=plan.chunk_set_id,
        work_prefix=plan.work_prefix,
        pdf_container=container,
        pdf_path=path,
        pdf_sha256=sha,
        page_count=pages,
        title=plan.title,
        encoding=plan.physical_index.encoding,
        settings=item.settings,
    )


# --- Blob helpers ---------------------------------------------------------------------------------


def _write_chunk(sinks: Sequence[IO[bytes]], hasher: Any, chunk: bytes) -> None:
    for sink in sinks:
        sink.write(chunk)
    hasher.update(chunk)


@dataclass(frozen=True, slots=True)
class Downloaded:
    sha256: str
    size: int
    data: bytes | None = None


async def download_into(
    container: BlobContainerPort,
    path: str,
    sinks: Sequence[IO[bytes]],
    *,
    expected_sha256: str,
    keep: bool = False,
) -> Downloaded:
    """Stream a blob into each sink chunk by chunk, each chunk's writes and hashing in one worker
    thread (section 6.3 step 1). A missing blob is `source_missing`, other bytes than expected
    `source_changed`, a storage failure `storage_unavailable`; `keep` also returns the bytes."""
    hasher = hashlib.sha256()
    size = 0
    kept: list[bytes] = []
    try:
        downloader = await container.download_blob(path)
        async for chunk in downloader.chunks():
            await asyncio.to_thread(_write_chunk, sinks, hasher, chunk)
            size += len(chunk)
            if keep:
                kept.append(chunk)
    except ResourceNotFoundError:
        raise ItemFailure(SOURCE_MISSING) from None
    except AzureError:
        raise ItemFailure(STORAGE_UNAVAILABLE) from None
    digest = hasher.hexdigest()
    if digest != expected_sha256:
        raise ItemFailure(SOURCE_CHANGED)
    return Downloaded(digest, size, b"".join(kept) if keep else None)


async def read_blob(container: BlobContainerPort, path: str) -> bytes | None:
    """A small blob read whole (a work file, a marker); `None` when it doesn't exist."""
    try:
        downloader = await container.download_blob(path)
        return await downloader.readall()
    except ResourceNotFoundError:
        return None
    except AzureError:
        raise ItemFailure(STORAGE_UNAVAILABLE) from None


async def write_blob(container: BlobContainerPort, path: str, data: bytes, **kwargs: Any) -> None:
    """`upload_blob(overwrite=True)`; a storage failure is `storage_unavailable`."""
    try:
        await container.upload_blob(path, data, overwrite=True, **kwargs)
    except AzureError:
        raise ItemFailure(STORAGE_UNAVAILABLE) from None


async def delete_blob(container: BlobContainerPort, path: str, *, code: str) -> None:
    """Delete one blob; an absent blob counts as deleted, any other failure is `code`."""
    try:
        await container.delete_blob(path)
    except ResourceNotFoundError:
        return
    except AzureError:
        raise ItemFailure(code) from None


async def list_names(container: BlobContainerPort, prefix: str, *, code: str) -> list[str]:
    try:
        return [blob.name async for blob in container.list_blobs(name_starts_with=prefix)]
    except AzureError:
        raise ItemFailure(code) from None


async def delete_names(container: BlobContainerPort, names: Iterable[str], *, code: str) -> int:
    """`delete_blobs` with at most 256 names per call; an absent blob counts as deleted."""
    pending = list(names)
    deleted = 0
    try:
        for start in range(0, len(pending), BLOB_DELETE_BATCH):
            batch = pending[start : start + BLOB_DELETE_BATCH]
            responses = await container.delete_blobs(*batch, raise_on_any_failure=False)
            async for response in responses:
                if getattr(response, "status_code", None) not in DELETED_STATUSES:
                    raise ItemFailure(code)
            deleted += len(batch)
    except AzureError:
        raise ItemFailure(code) from None
    return deleted


async def delete_prefix(container: BlobContainerPort, prefix: str, *, code: str) -> int:
    """Every blob under a prefix: listed, then deleted 256 at a time."""
    return await delete_names(container, await list_names(container, prefix, code=code), code=code)


# --- Chunk and version clean-up (sections 6.10 and 6.12) ------------------------------------------

CLEANUP_FAILED: Final = "cleanup_failed"


async def delete_chunks(
    search: SearchDocumentsPort, document_id: UUID, chunk_set_id: UUID, chunk_count: int
) -> int:
    """Delete a chunk set's keys `{documentId}_{chunkSetId}_{0 … count - 1}` in batches of 500;
    a key that doesn't exist counts as deleted. No search query is needed (section 13.4)."""
    keys = list(chunk_keys(document_id, chunk_set_id, chunk_count))
    # Every schema version keys its documents by the same field.
    key_field = ChunkIndexFields.for_version(CHUNK_INDEX_SCHEMA_VERSION).key
    try:
        for start in range(0, len(keys), CHUNK_DELETE_BATCH):
            batch = [{key_field: key} for key in keys[start : start + CHUNK_DELETE_BATCH]]
            await search.delete_documents(documents=batch)
    except ResourceNotFoundError:
        # The physical index is gone, and its chunks with it.
        return 0
    except AzureError:
        raise ItemFailure(CLEANUP_FAILED) from None
    return len(keys)


@dataclass(frozen=True, slots=True)
class RetiredSet:
    """A retired chunk set whose chunks are still in an `active` or `building` index."""

    id: UUID
    document_id: UUID
    chunk_count: int | None
    physical_index_name: str


def retired_sets_of(connection: Connection, chunk_set_ids: Sequence[UUID]) -> list[RetiredSet]:
    """The given chunk sets that are retired, still hold chunks and live in an `active` or
    `building` physical index; sets in a `retiring` or `deleted` index go with the index."""
    if not chunk_set_ids:
        return []
    rows = connection.execute(
        select(
            chunk_sets.c.id,
            chunk_sets.c.document_id,
            chunk_sets.c.chunk_count,
            physical_indexes.c.name,
        )
        .select_from(chunk_sets)
        .join(physical_indexes, physical_indexes.c.id == chunk_sets.c.physical_index_id)
        .where(
            chunk_sets.c.id.in_(list(chunk_set_ids)),
            chunk_sets.c.state == literal("retired"),
            chunk_sets.c.chunks_deleted_at.is_(None),
            physical_indexes.c.state.in_(literals(("active", "building"))),
        )
    ).all()
    return [RetiredSet(row.id, row.document_id, row.chunk_count, row.name) for row in rows]


def mark_chunks_deleted(connection: Connection, chunk_set_id: UUID) -> None:
    connection.execute(
        update(chunk_sets)
        .where(chunk_sets.c.id == chunk_set_id, chunk_sets.c.chunks_deleted_at.is_(None))
        .values(chunks_deleted_at=SYSUTCDATETIME)
    )


async def remove_retired_set(services: DocumentServices, chunk_set: RetiredSet) -> int:
    """Delete one retired set's chunks by key, then record `chunks_deleted_at`."""
    deleted = 0
    if chunk_set.chunk_count:
        deleted = await delete_chunks(
            services.search(chunk_set.physical_index_name),
            chunk_set.document_id,
            chunk_set.id,
            chunk_set.chunk_count,
        )

    def mark(connection: Connection) -> None:
        mark_chunks_deleted(connection, chunk_set.id)

    await services.sql.run(mark, name="chunks_deleted", retry="transient")
    return deleted


@dataclass(frozen=True, slots=True)
class VersionStorage:
    """A superseded or failed version whose stored files are still to delete."""

    id: UUID
    index_id: UUID
    document_id: UUID
    version_number: int
    blob_path: str


def version_storage(connection: Connection, version_id: UUID) -> VersionStorage | None:
    row = connection.execute(
        select(
            document_versions.c.id,
            document_versions.c.search_index_id,
            document_versions.c.document_id,
            document_versions.c.version_number,
            document_versions.c.blob_path,
        ).where(
            document_versions.c.id == version_id, document_versions.c.storage_purged_at.is_(None)
        )
    ).first()
    if row is None:
        return None
    return VersionStorage(
        row.id, row.search_index_id, row.document_id, row.version_number, row.blob_path
    )


async def purge_version_storage(services: DocumentServices, version: VersionStorage) -> None:
    """Delete a version's source, its converted PDF and every blob of its renditions, then
    record `storage_purged_at` (section 6.10 step 2); absent blobs count as deleted."""
    await delete_blob(
        services.documents,
        validate_blob_path(BlobContainer.DOCUMENTS, version.blob_path),
        code=CLEANUP_FAILED,
    )
    await delete_blob(
        services.documents,
        converted_path(version.index_id, version.document_id, version.version_number),
        code=CLEANUP_FAILED,
    )
    prefix = validate_blob_prefix(
        BlobContainer.RENDITIONS,
        f"{version.index_id}/{version.document_id}/{version.version_number}/",
    )
    await delete_prefix(services.renditions, prefix, code=CLEANUP_FAILED)

    def mark(connection: Connection) -> None:
        connection.execute(
            update(document_versions)
            .where(
                document_versions.c.id == version.id,
                document_versions.c.storage_purged_at.is_(None),
            )
            .values(storage_purged_at=SYSUTCDATETIME)
        )

    await services.sql.run(mark, name="version_purged", retry="transient")


def item_ids(item: ItemEnvelope) -> dict[str, UUID]:
    """The plan's identifiers, for steps whose input carries a plan."""
    plan = getattr(item, "plan", None)
    return plan.ids() if isinstance(plan, BuildPlan) else {}
