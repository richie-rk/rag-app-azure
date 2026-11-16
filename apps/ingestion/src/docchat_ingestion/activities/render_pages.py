"""`render_pages`, the Render step (`14-ingestion.md` sections 6.8 and 14; ADRs 0145 and 0152).

Every format gets a single-page PDF rendition per page of the version's PDF and the
whole-document rendition, built from the rendition object schema by the isolation runner's
`render`, every page checked by `verify-render` under a second parse user before it is uploaded.
The marker `complete.json` is deleted before the first page or `document.pdf` is written and
written last, so its presence always means a complete set of that PDF under that rules version;
a marker that already carries the chunk set's `pdf_sha256` and the deployed
`RENDITION_RULES_VERSION` makes the step only record its values (`skipped: true`).

For a DOCX or PPTX version Render also clears a stored `converted_sha256` that no longer names
the PDF Extract parsed, before any page is written, so a citation of the pages it replaces
answers `410 document_changed`; once its renditions are uploaded it stores the converted PDF in
`documents` (Cool tier) for the next re-ingest and records its hash and converter version.

Render runs before the Flip, so every current version has a rendition for every page a citation
can name. `rerender_pages` shares the helpers of this module.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from azure.storage.blob import ContentSettings, StandardBlobTier
from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import Connection, func, update

from docchat_core.ingestion.models import ActivityResult, ContentType, ItemStage
from docchat_core.ingestion.names import (
    BlobContainer,
    rendition_document_path,
    rendition_marker_path,
    rendition_page_path,
    validate_blob_prefix,
)
from docchat_core.sql.tables.ingestion import chunk_sets, document_versions
from docchat_ingestion.activities.documents import (
    DEADLINES,
    OFFICE_TYPES,
    STORAGE_UNAVAILABLE,
    ActivityOutput,
    BlobContainerPort,
    DocumentServices,
    ItemFailure,
    PlanInput,
    ServicesProvider,
    blueprint,
    delete_blob,
    delete_names,
    document_services,
    download_into,
    fence_and_read,
    item_ids,
    list_names,
    pdf_location,
    read_blob,
    run_document_activity,
    write_blob,
)
from docchat_ingestion.activities.support import literal
from docchat_ingestion.isolation.results import SOURCE
from docchat_ingestion.isolation.runner import (
    RENDER_TIMEOUT_CAP_SECONDS,
    ParseUsersUnavailableError,
    RenditionOutcome,
)
from docchat_ingestion.workfiles import RenditionMarker, decode_marker

NAME: Final = "render_pages"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
RENDER_TOO_LARGE: Final = "render_too_large"
RENDER_FAILED: Final = "render_failed"
PDF_SETTINGS: Final = ContentSettings(content_type="application/pdf")
JSON_SETTINGS: Final = ContentSettings(content_type="application/json")
_PAGE_FILE: Final = re.compile(r"pages/([1-9][0-9]*)\.pdf$")

type DocumentPdf = Literal["built", "too_large"]


class RenderResult(ActivityResult):
    """`{pages, skipped, documentPdf}` with the `ActivityResult` fields."""

    pages: int | None = Field(default=None, ge=1)
    skipped: bool = False
    document_pdf: DocumentPdf | None = None


@dataclass(frozen=True, slots=True)
class RenditionTarget:
    """Where a version's renditions live and the PDF they are built from."""

    index_id: UUID
    document_id: UUID
    version_number: int
    version_id: UUID
    content_type: ContentType
    pdf_sha256: str
    page_count: int

    def page_path(self, number: int) -> str:
        return rendition_page_path(self.index_id, self.document_id, self.version_number, number)

    @property
    def document_path(self) -> str:
        return rendition_document_path(self.index_id, self.document_id, self.version_number)

    @property
    def marker_path(self) -> str:
        return rendition_marker_path(self.index_id, self.document_id, self.version_number)

    @property
    def pages_prefix(self) -> str:
        prefix = validate_blob_prefix(
            BlobContainer.RENDITIONS,
            f"{self.index_id}/{self.document_id}/{self.version_number}/",
        )
        return f"{prefix}pages/"

    @property
    def is_office(self) -> bool:
        return self.content_type in OFFICE_TYPES


@dataclass(slots=True)
class BlobSink:
    """The runner's `RenditionSink`: verified pages and `document.pdf` uploaded with content type
    `application/pdf`, overwriting; a storage failure is `storage_unavailable`."""

    container: BlobContainerPort
    target: RenditionTarget

    async def upload_page(self, number: int, data: bytes) -> None:
        await write_blob(
            self.container, self.target.page_path(number), data, content_settings=PDF_SETTINGS
        )

    async def upload_document(self, data: bytes) -> None:
        await write_blob(
            self.container, self.target.document_path, data, content_settings=PDF_SETTINGS
        )


# --- The marker -----------------------------------------------------------------------------------


async def current_marker(
    services: DocumentServices, target: RenditionTarget
) -> RenditionMarker | None:
    """The stored marker when it names this PDF under the deployed rules; otherwise `None`."""
    data = await read_blob(services.renditions, target.marker_path)
    marker = None if data is None else decode_marker(data)
    if marker is None or not marker.current(target.pdf_sha256, services.rendition_rules_version):
        return None
    return marker


async def delete_marker(services: DocumentServices, target: RenditionTarget) -> None:
    """Before the first page is written: an absent marker counts as deleted, a storage error is
    `storage_unavailable` with no page written (section 6.8 step 2)."""
    await delete_blob(services.renditions, target.marker_path, code=STORAGE_UNAVAILABLE)


async def write_marker(
    services: DocumentServices, target: RenditionTarget, document_pdf: DocumentPdf
) -> RenditionMarker:
    marker = RenditionMarker(
        page_count=target.page_count,
        pdf_sha256=target.pdf_sha256,
        rendition_rules_version=services.rendition_rules_version,
        document_pdf=document_pdf,
    )
    await write_blob(
        services.renditions, target.marker_path, marker.encode(), content_settings=JSON_SETTINGS
    )
    return marker


# --- SQL ------------------------------------------------------------------------------------------


def clear_stale_converted(connection: Connection, version_id: UUID, pdf_sha256: str) -> None:
    """A stored converted PDF other than the one Extract parsed stops naming the version's
    pages before any page changes (section 6.8 step 2)."""
    pdf = bytes.fromhex(pdf_sha256)
    connection.execute(
        update(document_versions)
        .where(
            document_versions.c.id == version_id,
            document_versions.c.converted_sha256.is_not(None),
            document_versions.c.converted_sha256 != pdf,
        )
        .values(converted_sha256=None)
    )


def record_completion(
    connection: Connection,
    target: RenditionTarget,
    marker: RenditionMarker,
    converter_version: str | None,
) -> None:
    """Section 6.8 step 6, one transaction: the version's `document_pdf_status` (and, for a DOCX
    or PPTX version, its converted PDF's hash and converter version), then
    `rendition_rules_version` on every live chunk set built on this PDF."""
    pdf = bytes.fromhex(marker.pdf_sha256)
    values: dict[str, object] = {"document_pdf_status": literal(marker.document_pdf)}
    if target.is_office:
        values["converted_sha256"] = pdf
        values["converter_version"] = func.coalesce(
            converter_version, document_versions.c.converter_version
        )
    connection.execute(
        update(document_versions)
        .where(document_versions.c.id == target.version_id)
        .values(**values)
    )
    connection.execute(
        update(chunk_sets)
        .where(
            chunk_sets.c.document_version_id == target.version_id,
            chunk_sets.c.pdf_sha256 == pdf,
            chunk_sets.c.state != literal("retired"),
        )
        .values(rendition_rules_version=marker.rendition_rules_version)
    )


def set_document_pdf_too_large(connection: Connection, version_id: UUID) -> None:
    connection.execute(
        update(document_versions)
        .where(document_versions.c.id == version_id)
        .values(document_pdf_status=literal("too_large"))
    )


# --- Pages ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rendered:
    outcome: RenditionOutcome
    pdf: bytes | None


def failure_code(outcome: RenditionOutcome) -> str:
    """Render's failures: the child's time and memory limits keep their parse codes, the size
    caps are `render_too_large`, anything else `render_failed` (section 6.8 step 7)."""
    code = outcome.code or RENDER_FAILED
    known = {RENDER_TOO_LARGE, RENDER_FAILED, "parse_timeout", "parse_memory_exceeded"}
    return code if code in known else RENDER_FAILED


async def render_from(
    services: DocumentServices,
    target: RenditionTarget,
    source: tuple[BlobContainerPort, str],
    *,
    max_pages: int,
    keep_pdf: bool = False,
) -> Rendered:
    """Section 6.8 step 3 and 4: download the PDF expecting its hash, delete the marker, render
    and verify every page, upload each and the whole-document rendition. `keep_pdf` also returns
    the PDF's bytes, which Render stores as a DOCX or PPTX version's converted PDF."""
    container, path = source
    pool = services.parse_pool
    try:
        async with pool.slot(min(pool.parse_timeout_seconds, RENDER_TIMEOUT_CAP_SECONDS)) as slot:
            directory = slot.scratch()
            with await asyncio.to_thread(directory.open_new, SOURCE) as sink:
                downloaded = await download_into(
                    container, path, (sink,), expected_sha256=target.pdf_sha256, keep=keep_pdf
                )
            outcome = await slot.render(
                directory,
                page_count=target.page_count,
                pdf_size=downloaded.size,
                max_pages=max_pages,
                sink=BlobSink(services.renditions, target),
            )
    except ParseUsersUnavailableError:
        raise ItemFailure("parse_timeout") from None
    return Rendered(outcome, downloaded.data)


async def delete_pages_above(services: DocumentServices, target: RenditionTarget) -> None:
    """Page blobs above the page count, left by an earlier conversion, are deleted."""
    names = await list_names(services.renditions, target.pages_prefix, code=STORAGE_UNAVAILABLE)
    above = [
        name
        for name in names
        if (match := _PAGE_FILE.search(name)) is not None
        and int(match.group(1)) > target.page_count
    ]
    if above:
        await delete_names(services.renditions, above, code=STORAGE_UNAVAILABLE)


async def delete_uploaded_pages(services: DocumentServices, target: RenditionTarget) -> None:
    """Above a size cap Render deletes every page it uploaded under the version's prefix."""
    names = await list_names(services.renditions, target.pages_prefix, code=STORAGE_UNAVAILABLE)
    await delete_names(services.renditions, names, code=STORAGE_UNAVAILABLE)


# --- The step -------------------------------------------------------------------------------------


async def render(services: DocumentServices, item: PlanInput, span: Span) -> RenderResult:
    del span
    plan = item.plan
    facts = await fence_and_read(services, item, ItemStage.RENDER, DEADLINE_SECONDS)
    if facts.pdf_sha256 is None or facts.page_count is None:
        raise ValueError("Render follows Extract, which records the PDF and its page count")
    target = RenditionTarget(
        index_id=plan.index_id,
        document_id=plan.document_id,
        version_number=plan.version_number,
        version_id=plan.version_id,
        content_type=plan.content_type,
        pdf_sha256=facts.pdf_sha256,
        page_count=facts.page_count,
    )
    conversion = plan.conversion
    converted_now = conversion is not None and facts.pdf_sha256 != conversion.converted_sha256
    converter_version = services.converter_version if converted_now else None
    marker = await current_marker(services, target)
    if marker is not None:
        await services.sql.run(
            lambda connection: record_completion(connection, target, marker, converter_version),
            name="render_complete",
            retry="transient",
        )
        return RenderResult.success(
            pages=marker.page_count, skipped=True, document_pdf=marker.document_pdf
        )
    await delete_marker(services, target)
    if plan.is_office:
        await services.sql.run(
            lambda connection: clear_stale_converted(
                connection, plan.version_id, target.pdf_sha256
            ),
            name="render_clear_converted",
            retry="transient",
        )
    container_name, path, _ = pdf_location(plan, facts)
    container = (
        services.work if container_name is BlobContainer.INGESTION_WORK else services.documents
    )
    rendered = await render_from(
        services,
        target,
        (container, path),
        max_pages=item.settings.max_pages_per_document,
        keep_pdf=converted_now,
    )
    outcome = rendered.outcome
    if outcome.code is not None:
        code = failure_code(outcome)
        if code == RENDER_TOO_LARGE:
            await delete_uploaded_pages(services, target)
        raise ItemFailure(code)
    document_pdf: DocumentPdf = outcome.document_pdf or "too_large"
    await delete_pages_above(services, target)
    if document_pdf == "too_large":
        await delete_blob(services.renditions, target.document_path, code=STORAGE_UNAVAILABLE)
    if conversion is not None and converted_now:
        if rendered.pdf is None:
            raise ValueError("the converted PDF's bytes were kept for its upload")
        await write_blob(
            services.documents,
            conversion.converted_blob_path,
            rendered.pdf,
            standard_blob_tier=StandardBlobTier.COOL,
            content_settings=PDF_SETTINGS,
        )
    written = await write_marker(services, target, document_pdf)
    await services.sql.run(
        lambda connection: record_completion(connection, target, written, converter_version),
        name="render_complete",
        retry="transient",
    )
    return RenderResult.success(pages=target.page_count, skipped=False, document_pdf=document_pdf)


def _failure(code: str, retry_after: int | None) -> RenderResult:
    return RenderResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PlanInput,
        render,
        stage=ItemStage.RENDER,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def render_pages(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(render_pages)
