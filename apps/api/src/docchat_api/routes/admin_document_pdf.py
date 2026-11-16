"""`adminGetDocumentPdf` — `GET /v1/admin/indexes/{indexId}/documents/{documentId}/pdf`
(`11-api.md` sections 1.3, 1.10, 1.11 and 3.11.10; `12-chat-pipeline.md` sections 3.1 and 16.5;
`82-security.md` SEC-54, SEC-60 and SEC-94).

An administrator downloads the whole-document rendition of a document's current version, the same
verified file a reader gets from `getCitationDocument`, for every format; no operation serves an
uploaded source or a converted PDF to anyone, so the container `documents` is never read. After the
route class's pre-body checks (the administrator check included) and path validation, in order:

1. the download window and a download lease, in the administrator's own `usage` partition and
   shared with the two citation file operations, the lease's `sessionId` and `turnId` null; both
   fail closed with `503 dependency_unavailable`, and every later refusal releases the lease;
2. one SQL statement: the logical index (`404 index_not_found` when missing or `deleted`), the
   live document of that index (`404 document_not_found` for a deleted document, one of another
   index or a random ID), and its current version: none, or a `document_pdf_status` other than
   `built`, answers `409 document_pdf_unavailable` with no blob read;
3. the blob: a storage `404` answers `500 document_pdf_missing` and logs
   `document_pdf_blob_missing`;
4. the stream, through `CitationFileResponse`, with the file name `<stem>.pdf`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from pydantic.alias_generators import to_camel

from docchat_api.chat.citations import (
    PDF_RESPONSES,
    BlobDownload,
    CitationFileResponse,
    DocumentPdfUnavailable,
    RenditionMissing,
    RenditionsContainer,
    document_file_name,
    download_clock,
    hold_download,
    open_rendition,
    renditions_container,
)
from docchat_api.http.access import access
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_core.errors import DocChatProblem
from docchat_core.ingestion.models import DocumentPdfStatus
from docchat_core.ingestion.names import rendition_document_path
from docchat_core.models import CanonicalUuid
from docchat_core.models.clock import Clock
from docchat_core.sql.repositories.citations import read_document_pdf
from docchat_core.telemetry import LOGGER_NAME, log_event

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-documents"
DOCUMENT_ID: Final = to_camel("document_id")
"""The document path parameter, built as the models' aliases are: the chunk index has a field of
the same spelling, whose name only `docchat_core.search.schema` writes as a literal (T-107)."""
BLOB_MISSING: Final = "document_pdf_blob_missing"

_logger: Final = logging.getLogger(LOGGER_NAME)


class IndexNotFound(DocChatProblem):
    code = "index_not_found"


class DocumentNotFound(DocChatProblem):
    """`404`: no live document with this ID in this index."""

    code = "document_not_found"


class DocumentPdfMissing(DocChatProblem):
    """`500`: the storage service has no whole-document rendition the version records as built."""

    code = "document_pdf_missing"


IndexIdPath = Annotated[CanonicalUuid, PathParam(alias="indexId")]
DocumentIdPath = Annotated[CanonicalUuid, PathParam(alias=DOCUMENT_ID)]


@dataclass(frozen=True, slots=True)
class DocumentFile:
    """The whole-document rendition to serve: its blob, already open, and its file name."""

    download: BlobDownload
    file_name: str


async def open_document_pdf(
    process: ApiProcess, storage: RenditionsContainer, *, index_id: UUID, document_id: UUID
) -> DocumentFile:
    """Steps 4 to 6: the index, the live document and its current version's rendition, then the
    blob."""
    lookup = await read_document_pdf(process.sql, index_id=index_id, document_id=document_id)
    if lookup is None:
        raise IndexNotFound()
    document = lookup.document
    if document is None:
        raise DocumentNotFound()
    version = document.current_version
    if version is None or version.document_pdf_status is not DocumentPdfStatus.BUILT:
        raise DocumentPdfUnavailable()
    path = rendition_document_path(index_id, document.id, version.version_number)
    try:
        download = await open_rendition(storage, path)
    except RenditionMissing:
        log_event(
            _logger,
            BLOB_MISSING,
            logging.ERROR,
            **{
                "docchat.index_id": str(index_id),
                "docchat.document_id": str(document.id),
                "docchat.version_id": str(version.id),
            },
        )
        raise DocumentPdfMissing() from None
    return DocumentFile(download=download, file_name=document_file_name(document.source_name))


@router.get(
    "/admin/indexes/{indexId}/documents/{documentId}/pdf",
    operation_id="adminGetDocumentPdf",
    tags=[TAG],
    status_code=200,
    response_class=CitationFileResponse,
    responses=PDF_RESPONSES,
)
@access(
    "admin",
    scoped_params=["indexId", DOCUMENT_ID],
    error_codes=[
        "index_not_found",
        "document_not_found",
        "document_pdf_unavailable",
        "rate_limited",
        "too_many_downloads",
        "document_pdf_missing",
    ],
)
async def admin_get_document_pdf(
    *,
    request: Request,
    index_id: IndexIdPath,
    document_id: DocumentIdPath,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
    storage: Annotated[RenditionsContainer, Depends(renditions_container)],
    clock: Annotated[Clock, Depends(download_clock)],
) -> CitationFileResponse:
    ops = await request_ops(request)
    held = await hold_download(
        process.usage, principal.oid, ops.ratelimit, session_id=None, turn_id=None, clock=clock
    )
    try:
        document_file = await open_document_pdf(
            process, storage, index_id=index_id, document_id=document_id
        )
    except BaseException:
        await held.lease.release()
        raise
    return CitationFileResponse(
        document_file.download, file_name=document_file.file_name, held=held, clock=clock
    )
