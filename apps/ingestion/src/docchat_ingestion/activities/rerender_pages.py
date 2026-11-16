"""`rerender_pages`, the step of a `rerender` item (`14-ingestion.md` sections 6.15, 5.3 duty 12
and 14.8 item 6; ADR 0145).

A deploy that raised `RENDITION_RULES_VERSION` re-renders every served chunk set recorded under a
lower value, from the same PDF: the source of a PDF version, or the stored converted PDF of a
DOCX or PPTX version, whose bytes are the PDF the chunk set parsed. Readers keep the stored
renditions until each is overwritten, and both show the pages of one `pdf_sha256`, so no
citation changes its answer while it runs; `converted_sha256` is never touched.

The step makes its own checks (it has no Validate): a deleted document or a logical index being
deleted skips with `document_deleted`; a chunk set no longer `served`, or already recording the
deployed value, skips with `renditions_current`, and so does a marker that already names the
chunk set's PDF under the deployed value, whose values are recorded first. On `render_too_large`
no page is deleted: the pages already overwritten and those still under the earlier rules both
stay served, every one of them verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import Connection, select

from docchat_core.ingestion.models import ActivityResult, ContentType, ItemEnvelope, ItemStage
from docchat_core.ingestion.names import BlobContainer, converted_path, validate_blob_path
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    search_indexes,
)
from docchat_core.telemetry import set_attributes
from docchat_ingestion.activities.documents import (
    DEADLINES,
    OFFICE_TYPES,
    STORAGE_UNAVAILABLE,
    ActivityOutput,
    DocumentServices,
    ItemFailure,
    ServicesProvider,
    blueprint,
    delete_blob,
    document_services,
    fence,
    run_document_activity,
)
from docchat_ingestion.activities.render_pages import (
    DocumentPdf,
    RenditionTarget,
    current_marker,
    delete_marker,
    delete_pages_above,
    failure_code,
    record_completion,
    render_from,
    set_document_pdf_too_large,
    write_marker,
)

NAME: Final = "rerender_pages"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
DOCUMENT_DELETED: Final = "document_deleted"
RENDITIONS_CURRENT: Final = "renditions_current"

type RerenderSkip = Literal["document_deleted", "renditions_current"]


class RerenderResult(ActivityResult):
    """`{outcome, code, pages, documentPdf}` with the `ActivityResult` fields."""

    outcome: Literal["rendered", "skipped"] | None = None
    code: RerenderSkip | None = None
    pages: int | None = Field(default=None, ge=1)
    document_pdf: DocumentPdf | None = None


@dataclass(frozen=True, slots=True)
class ServedSet:
    """What step 1 reads: the item's chunk set, its version and its document."""

    chunk_set_id: UUID
    chunk_set_state: str
    pdf_sha256: str | None
    rendition_rules_version: int | None
    index_id: UUID
    document_id: UUID
    version_id: UUID
    version_number: int
    content_type: ContentType
    blob_path: str
    page_count: int | None
    document_deleted: bool
    index_status: str


def read_served_set(connection: Connection, envelope: ItemEnvelope) -> ServedSet:
    """The fenced lease update, then one query over the item's chunk set, version, document and
    logical index."""
    fence(connection, envelope, ItemStage.RENDER, DEADLINE_SECONDS)
    item = ingestion_run_items
    row = connection.execute(
        select(
            chunk_sets.c.id,
            chunk_sets.c.state,
            chunk_sets.c.pdf_sha256,
            chunk_sets.c.rendition_rules_version,
            document_versions.c.search_index_id,
            document_versions.c.document_id,
            document_versions.c.id.label("version_id"),
            document_versions.c.version_number,
            document_versions.c.content_type,
            document_versions.c.blob_path,
            document_versions.c.page_count,
            documents.c.is_deleted,
            search_indexes.c.status,
        )
        .select_from(item)
        .join(chunk_sets, chunk_sets.c.id == item.c.chunk_set_id)
        .join(document_versions, document_versions.c.id == chunk_sets.c.document_version_id)
        .join(documents, documents.c.id == chunk_sets.c.document_id)
        .join(search_indexes, search_indexes.c.id == documents.c.search_index_id)
        .where(item.c.id == envelope.item_id)
    ).one()
    sha = row.pdf_sha256
    return ServedSet(
        chunk_set_id=row.id,
        chunk_set_state=row.state,
        pdf_sha256=None if sha is None else bytes(sha).hex(),
        rendition_rules_version=row.rendition_rules_version,
        index_id=row.search_index_id,
        document_id=row.document_id,
        version_id=row.version_id,
        version_number=row.version_number,
        content_type=ContentType(row.content_type),
        blob_path=row.blob_path,
        page_count=row.page_count,
        document_deleted=bool(row.is_deleted),
        index_status=row.status,
    )


def still_due(served: ServedSet, rules_version: int) -> RerenderSkip | None:
    """Section 6.15 step 1: why the item has nothing to do, or `None` when it renders."""
    if served.document_deleted or served.index_status in {"deleting", "deleted"}:
        return DOCUMENT_DELETED
    if served.chunk_set_state != "served" or served.rendition_rules_version == rules_version:
        return RENDITIONS_CURRENT
    return None


def stored_pdf_path(served: ServedSet) -> str:
    """The PDF the chunk set parsed: the version's source, or its stored converted PDF."""
    if served.content_type in OFFICE_TYPES:
        return converted_path(served.index_id, served.document_id, served.version_number)
    return validate_blob_path(BlobContainer.DOCUMENTS, served.blob_path)


async def rerender(services: DocumentServices, item: ItemEnvelope, span: Span) -> RerenderResult:
    served = await services.sql.run(
        lambda connection: read_served_set(connection, item),
        name="rerender_read",
        retry="transient",
    )
    set_attributes(
        span,
        **{
            "docchat.document_id": str(served.document_id),
            "docchat.version_id": str(served.version_id),
            "docchat.chunk_set_id": str(served.chunk_set_id),
        },
    )
    skip = still_due(served, services.rendition_rules_version)
    if skip is not None:
        return RerenderResult.success(outcome="skipped", code=skip)
    if served.pdf_sha256 is None or served.page_count is None:
        raise ValueError("a served chunk set records its PDF and the version its page count")
    target = RenditionTarget(
        index_id=served.index_id,
        document_id=served.document_id,
        version_number=served.version_number,
        version_id=served.version_id,
        content_type=served.content_type,
        pdf_sha256=served.pdf_sha256,
        page_count=served.page_count,
    )
    marker = await current_marker(services, target)
    if marker is not None:
        await services.sql.run(
            lambda connection: record_completion(connection, target, marker, None),
            name="rerender_complete",
            retry="transient",
        )
        return RerenderResult.success(outcome="skipped", code=RENDITIONS_CURRENT)
    await delete_marker(services, target)
    rendered = await render_from(
        services,
        target,
        (services.documents, stored_pdf_path(served)),
        max_pages=item.settings.max_pages_per_document,
    )
    outcome = rendered.outcome
    if outcome.code is not None:
        # No page is deleted: every page left, overwritten or not, was verified.
        raise ItemFailure(failure_code(outcome))
    document_pdf: DocumentPdf = outcome.document_pdf or "too_large"
    await delete_pages_above(services, target)
    if document_pdf == "too_large":
        # The status first, so a reader gets `409 document_pdf_unavailable`, never a missing file.
        await services.sql.run(
            lambda connection: set_document_pdf_too_large(connection, target.version_id),
            name="rerender_too_large",
            retry="transient",
        )
        await delete_blob(services.renditions, target.document_path, code=STORAGE_UNAVAILABLE)
    written = await write_marker(services, target, document_pdf)
    await services.sql.run(
        lambda connection: record_completion(connection, target, written, None),
        name="rerender_complete",
        retry="transient",
    )
    return RerenderResult.success(
        outcome="rendered", pages=target.page_count, document_pdf=document_pdf
    )


def _failure(code: str, retry_after: int | None) -> RerenderResult:
    return RerenderResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        ItemEnvelope,
        rerender,
        stage=ItemStage.RENDER,
        provider=provider,
        failure=_failure,
    )


async def rerender_pages(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(rerender_pages)
