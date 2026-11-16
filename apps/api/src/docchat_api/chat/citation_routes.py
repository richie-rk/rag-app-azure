"""`getCitation`, `getCitationContent` and `getCitationDocument` (`12-chat-pipeline.md` sections
16.1 to 16.6; `11-api.md` sections 1.3, 1.4.2 rule 4, 3.5 and 4.5; `82-security.md` SEC-19,
SEC-54, SEC-60 and SEC-94; ADR 0069).

A citation is addressed only by its session, turn and number; no document ID, storage path or URL
ever comes from the client, and none reaches it. After the route class's pre-body checks (token,
role, burst limit, provisioning) and path validation, every call re-runs the checks of section
16.2 in order, so revoking a membership, archiving a project, unmapping an index or replacing a
version hides or retires a citation from the next request on:

1. the two file operations take the caller's download window and a download lease (section
   16.5), and every later refusal releases the lease;
2. the turn, one point read in the caller's partition `[oid, sessionId]`: missing or still
   `streaming` → `404 citation_not_found`;
3. `authorize_project_for_chat` with the turn's project: refused → `404 citation_not_found`;
4. the stored citation with that number → `404 citation_not_found` when there is none;
5. one SQL statement on the document, the cited version, the cited chunk set and the project's
   mapping of the document's index: no row, a deleted document or another current version →
   `410 document_changed`; an unmapped index → `404 citation_not_found`;
6. the renditions must come from the PDF the cited chunk set parsed (`pdf_sha256` against the
   version's `sha256` for a PDF, `converted_sha256` for a DOCX or PPTX) → `410` otherwise;
7. `getCitationDocument` only: no whole-document rendition → `409 document_pdf_unavailable`.

`getCitation` returns metadata and reads no blob; the file operations stream the cited page's
rendition or the whole-document rendition through `CitationFileResponse`. Each call is one span
`docchat.citation.open` and one count in `docchat.citation.requests`, with the outcome the call
ended with, after its stream for a file.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from pydantic import BeforeValidator, Field
from pydantic_core import PydanticCustomError

from docchat_api.chat.citations import (
    PDF_RESPONSES,
    BlobDownload,
    CitationFileResponse,
    DocumentPdfUnavailable,
    DownloadLabels,
    RenditionMissing,
    RenditionsContainer,
    StreamOutcome,
    document_file_name,
    download_clock,
    hold_download,
    open_rendition,
    page_file_name,
    renditions_container,
)
from docchat_api.conversations.store import ConversationError
from docchat_api.http.access import access
from docchat_api.http.models import ResponseModel
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_core.access import ProjectNotFound
from docchat_core.auth import Principal
from docchat_core.cosmos.conversations import StoredCitation, TurnStatus
from docchat_core.errors import DocChatProblem
from docchat_core.ingestion.models import ContentType, DocumentPdfStatus
from docchat_core.ingestion.names import (
    HEADING_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    rendition_document_path,
    rendition_page_path,
)
from docchat_core.models import CanonicalUuid
from docchat_core.models.clock import Clock
from docchat_core.sql.engine import SqlTimeout, SqlUnavailable
from docchat_core.sql.repositories.citations import CitedDocument, read_cited_document
from docchat_core.telemetry import LOGGER_NAME, log_event, set_attributes, start_span
from docchat_core.telemetry.instruments import counter

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "chat"
CITATION_PATH: Final = "/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}"
SCOPED_PARAMS: Final = ("sessionId", "turnId", "citationNumber")
CITATION_NUMBER_MAX: Final = 16
PAGE_NUMBER_MAX: Final = 100_000
HEADING_PATH_MAX_ITEMS: Final = 8
FILE_NAME_MAX_LENGTH: Final = 255

OPEN_SPAN: Final = "docchat.citation.open"
BLOB_MISSING: Final = "citation_blob_missing"
CITATION_REQUESTS: Final = counter("docchat.citation.requests")

type CitationOperation = Literal["getCitation", "getCitationContent", "getCitationDocument"]
type OpenAs = Literal["pdf_page", "document_pdf"]
type CitationOutcome = Literal[
    "ok", "not_found", "gone", "unavailable", "missing", "limited", "aborted"
]

_OUTCOMES: Final[dict[str, CitationOutcome]] = {
    "citation_not_found": "not_found",
    "document_changed": "gone",
    "document_pdf_unavailable": "unavailable",
    "dependency_unavailable": "unavailable",
    "citation_content_missing": "missing",
    "rate_limited": "limited",
    "too_many_downloads": "limited",
}
"""The outcome each refusal of these operations records."""

_STREAM_OUTCOMES: Final[dict[StreamOutcome, CitationOutcome]] = {
    "completed": "ok",
    "disconnected": "ok",
    "aborted": "aborted",
    "failed": "unavailable",
}
"""A client that stops reading was served until it left; only the server's aborts count as
`aborted`, and a storage failure after the headers as `unavailable`."""

_logger: Final = logging.getLogger(LOGGER_NAME)


# --- Problems ----------------------------------------------------------------------------------


class CitationNotFound(DocChatProblem):
    """`404`: the turn, the project, the citation or the mapping isn't there for the caller now;
    one body for every cause."""

    code = "citation_not_found"


class DocumentChanged(DocChatProblem):
    """`410`: the cited version is no longer the document's current one, the document is deleted,
    or the renditions no longer show the PDF the cited page belongs to."""

    code = "document_changed"


class CitationContentMissing(DocChatProblem):
    """`500`: the storage service has no blob for a rendition of a current version."""

    code = "citation_content_missing"


# --- Request and response models ---------------------------------------------------------------


def _whole_number(value: object) -> object:
    """A path segment that isn't a decimal number is `invalid_type`; its range is checked after."""
    if not isinstance(value, str):
        return value
    if not (value.isascii() and value.isdigit()):
        raise PydanticCustomError("invalid_type", "a citation number is a whole number")
    try:
        return int(value)
    except ValueError:
        # Only a number too long to convert gets here, and it is far beyond the range.
        raise PydanticCustomError("out_of_range", "a citation number lies from 1 to 16") from None


SessionIdPath = Annotated[CanonicalUuid, PathParam(alias="sessionId")]
TurnIdPath = Annotated[CanonicalUuid, PathParam(alias="turnId")]
# The parameter's metadata comes first, so its bounds become the schema's `minimum` and
# `maximum`; after it, the validator turns a non-decimal segment into `invalid_type`.
CitationNumberPath = Annotated[
    int,
    PathParam(alias="citationNumber", ge=1, le=CITATION_NUMBER_MAX),
    BeforeValidator(_whole_number),
]


class CitationDetail(ResponseModel):
    """What the citation viewer needs: the citation as `Citation` shows it and the two file
    names, which cross-origin scripts can't read from `Content-Disposition`."""

    number: Annotated[int, Field(ge=1, le=CITATION_NUMBER_MAX)]
    title: Annotated[str, Field(min_length=1, max_length=TITLE_MAX_LENGTH)]
    page_number: Annotated[int, Field(ge=1, le=PAGE_NUMBER_MAX)]
    heading_path: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=HEADING_MAX_LENGTH)]],
        Field(max_length=HEADING_PATH_MAX_ITEMS),
    ]
    content_type: ContentType
    open_as: Literal["pdf_page"]
    file_name: Annotated[str, Field(min_length=1, max_length=FILE_NAME_MAX_LENGTH)]
    document_pdf_available: bool
    document_file_name: Annotated[str, Field(min_length=1, max_length=FILE_NAME_MAX_LENGTH)]


# --- Telemetry ---------------------------------------------------------------------------------


def outcome_of(error: BaseException) -> CitationOutcome | None:
    """The outcome a refusal records; `None` for a defect, whose `internal_error` the failure
    boundary records."""
    if isinstance(error, DocChatProblem):
        return _OUTCOMES.get(error.entry.code)
    if isinstance(error, SqlUnavailable | SqlTimeout):
        return "unavailable"
    return None


class CitationOpening:
    """The span `docchat.citation.open` and the count in `docchat.citation.requests` of one
    call. The span is current from the endpoint's first check to the end of the response, the
    stream included, and ends once with the call's outcome."""

    def __init__(self, operation: CitationOperation, open_as: OpenAs) -> None:
        self._operation: CitationOperation = operation
        self._stack = ExitStack()
        self._span = self._stack.enter_context(
            start_span(OPEN_SPAN, **{"docchat.operation": operation, "docchat.open_as": open_as})
        )
        self._ended = False

    def end(self, outcome: CitationOutcome | None) -> None:
        if self._ended:
            return
        self._ended = True
        if outcome is not None:
            set_attributes(self._span, **{"docchat.outcome": outcome})
            CITATION_REQUESTS.add(1, operation=self._operation, outcome=outcome)
        self._stack.close()

    def refused(self, error: BaseException) -> None:
        self.end(outcome_of(error))

    def streamed(self, outcome: StreamOutcome) -> None:
        self.end(_STREAM_OUTCOMES[outcome])


# --- The checks --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cited:
    """A citation that passed checks 2 to 6: the stored citation and its document's row."""

    citation: StoredCitation
    document: CitedDocument


async def check_citation(
    process: ApiProcess, principal: Principal, session_id: UUID, turn_id: UUID, number: int
) -> Cited:
    """Checks 2 to 6 of section 16.2, in order; the first failure answers."""
    try:
        turn = await process.conversations.get_turn(principal, session_id, turn_id)
    except ConversationError as error:
        raise error.problem() from error
    if turn is None or turn.status is TurnStatus.STREAMING:
        raise CitationNotFound()
    try:
        await process.access.authorize_project_for_chat(principal, turn.project_id)
    except ProjectNotFound:
        raise CitationNotFound() from None
    citation = next((stored for stored in turn.citations if stored.number == number), None)
    if citation is None:
        raise CitationNotFound()
    document = await read_cited_document(
        process.sql,
        document_id=citation.document_id,
        document_version_id=citation.document_version_id,
        chunk_set_id=citation.chunk_set_id,
        project_id=turn.project_id,
    )
    if document is None:
        raise DocumentChanged()
    if not document.index_mapped:
        raise CitationNotFound()
    if document.is_deleted or document.current_version_id != citation.document_version_id:
        raise DocumentChanged()
    rendered_from = document.rendition_pdf_sha256
    if document.pdf_sha256 is None or rendered_from is None or document.pdf_sha256 != rendered_from:
        raise DocumentChanged()
    return Cited(citation=citation, document=document)


def citation_detail(cited: Cited) -> CitationDetail:
    citation, document = cited.citation, cited.document
    return CitationDetail(
        number=citation.number,
        title=citation.title,
        page_number=citation.page_number,
        # An empty heading names nothing; ingestion leaves such headings out already.
        heading_path=[heading for heading in citation.heading_path if heading],
        content_type=document.content_type,
        open_as="pdf_page",
        file_name=page_file_name(document.source_name, citation.page_number),
        document_pdf_available=document.document_pdf_status is DocumentPdfStatus.BUILT,
        document_file_name=document_file_name(document.source_name),
    )


# --- The operations ----------------------------------------------------------------------------


@router.get(CITATION_PATH, operation_id="getCitation", tags=[TAG], response_model=CitationDetail)
@access("user", scoped_params=SCOPED_PARAMS, error_codes=["citation_not_found", "document_changed"])
async def get_citation(
    *,
    session_id: SessionIdPath,
    turn_id: TurnIdPath,
    citation_number: CitationNumberPath,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> CitationDetail:
    opening = CitationOpening("getCitation", "pdf_page")
    try:
        detail = citation_detail(
            await check_citation(process, principal, session_id, turn_id, citation_number)
        )
    except BaseException as error:
        opening.refused(error)
        raise
    opening.end("ok")
    return detail


@dataclass(frozen=True, slots=True)
class CitedFile:
    """The file a citation operation serves: its blob, already open, and its file name."""

    download: BlobDownload
    file_name: str


async def open_cited_file(
    process: ApiProcess,
    principal: Principal,
    storage: RenditionsContainer,
    labels: DownloadLabels,
    *,
    session_id: UUID,
    turn_id: UUID,
    number: int,
    whole_document: bool,
) -> CitedFile:
    """Checks 2 to 6, check 7 for the whole document, then the blob of the rendition; `labels`
    name the citation in the missing blob's log record."""
    cited = await check_citation(process, principal, session_id, turn_id, number)
    citation, document = cited.citation, cited.document
    if whole_document:
        if document.document_pdf_status is not DocumentPdfStatus.BUILT:
            raise DocumentPdfUnavailable()
        path = rendition_document_path(
            document.search_index_id, citation.document_id, document.version_number
        )
        file_name = document_file_name(document.source_name)
    else:
        path = rendition_page_path(
            document.search_index_id,
            citation.document_id,
            document.version_number,
            citation.page_number,
        )
        file_name = page_file_name(document.source_name, citation.page_number)
    try:
        download = await open_rendition(storage, path)
    except RenditionMissing:
        log_event(
            _logger,
            BLOB_MISSING,
            logging.ERROR,
            **labels.attributes(),
            **{
                "docchat.document_id": str(citation.document_id),
                "docchat.version_id": str(citation.document_version_id),
            },
        )
        raise CitationContentMissing() from None
    return CitedFile(download=download, file_name=file_name)


async def serve_citation_file(
    request: Request,
    *,
    whole_document: bool,
    session_id: UUID,
    turn_id: UUID,
    number: int,
    principal: Principal,
    process: ApiProcess,
    storage: RenditionsContainer,
    clock: Clock,
) -> CitationFileResponse:
    """The two file operations: the download limits, the checks, the blob, then the stream; a
    refusal after the lease releases it."""
    opening = (
        CitationOpening("getCitationDocument", "document_pdf")
        if whole_document
        else CitationOpening("getCitationContent", "pdf_page")
    )
    try:
        ops = await request_ops(request)
        held = await hold_download(
            process.usage,
            principal.oid,
            ops.ratelimit,
            session_id=session_id,
            turn_id=turn_id,
            clock=clock,
        )
    except BaseException as error:
        opening.refused(error)
        raise
    labels = DownloadLabels(session_id=session_id, turn_id=turn_id, citation_number=number)
    try:
        cited_file = await open_cited_file(
            process,
            principal,
            storage,
            labels,
            session_id=session_id,
            turn_id=turn_id,
            number=number,
            whole_document=whole_document,
        )
    except BaseException as error:
        try:
            await held.lease.release()
        finally:
            opening.refused(error)
        raise
    return CitationFileResponse(
        cited_file.download,
        file_name=cited_file.file_name,
        held=held,
        clock=clock,
        labels=labels,
        finished=opening.streamed,
    )


@router.get(
    f"{CITATION_PATH}/content",
    operation_id="getCitationContent",
    tags=[TAG],
    status_code=200,
    response_class=CitationFileResponse,
    responses=PDF_RESPONSES,
)
@access(
    "user",
    scoped_params=SCOPED_PARAMS,
    error_codes=[
        "citation_not_found",
        "document_changed",
        "rate_limited",
        "too_many_downloads",
        "citation_content_missing",
    ],
)
async def get_citation_content(
    *,
    request: Request,
    session_id: SessionIdPath,
    turn_id: TurnIdPath,
    citation_number: CitationNumberPath,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
    storage: Annotated[RenditionsContainer, Depends(renditions_container)],
    clock: Annotated[Clock, Depends(download_clock)],
) -> CitationFileResponse:
    return await serve_citation_file(
        request,
        whole_document=False,
        session_id=session_id,
        turn_id=turn_id,
        number=citation_number,
        principal=principal,
        process=process,
        storage=storage,
        clock=clock,
    )


@router.get(
    f"{CITATION_PATH}/document",
    operation_id="getCitationDocument",
    tags=[TAG],
    status_code=200,
    response_class=CitationFileResponse,
    responses=PDF_RESPONSES,
)
@access(
    "user",
    scoped_params=SCOPED_PARAMS,
    error_codes=[
        "citation_not_found",
        "document_changed",
        "document_pdf_unavailable",
        "rate_limited",
        "too_many_downloads",
        "citation_content_missing",
    ],
)
async def get_citation_document(
    *,
    request: Request,
    session_id: SessionIdPath,
    turn_id: TurnIdPath,
    citation_number: CitationNumberPath,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
    storage: Annotated[RenditionsContainer, Depends(renditions_container)],
    clock: Annotated[Clock, Depends(download_clock)],
) -> CitationFileResponse:
    return await serve_citation_file(
        request,
        whole_document=True,
        session_id=session_id,
        turn_id=turn_id,
        number=citation_number,
        principal=principal,
        process=process,
        storage=storage,
        clock=clock,
    )
