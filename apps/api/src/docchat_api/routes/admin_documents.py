"""The administrator document operations (`11-api.md` sections 1.8, 1.9, 3.11.1 to 3.11.4 and
4.7; `14-ingestion.md` sections 7.1 and 8.11).

`adminListDocuments`, `adminGetDocument`, `adminDeleteDocument` and `adminReingestDocument`.
Every route is `admin`. A document is addressed under its logical index: a document of another
index, a deleted document and a random ID all answer `404 document_not_found`, after the index's
own `404 index_not_found`. Deleted documents are never listed.

The two writes read the live document under an update lock in their transaction and answer, in
order, `404`, then the `If-Match` check on the document's `rowVersion` (`428`, then `412`), then
their state rules (`409`). A write that creates a run commits first and then sends one queue
message, inside the span `docchat.ingestion.submit`; `adminReingestDocument` creates none when an
item for the current version and target is already open, and answers `200` with that item's
newest open run.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi import Path as PathParam
from pydantic import BeforeValidator, Field
from pydantic.alias_generators import to_camel

from docchat_api.http.access import access
from docchat_api.http.audit import report_audit
from docchat_api.http.models import ResponseModel
from docchat_api.http.paging import CursorModel, decode_cursor
from docchat_api.http.preconditions import IfMatchHeader, if_match_header
from docchat_api.http.queue import IngestionQueuePort, RunSubmission, ingestion_queue
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.routes.admin_common import DOCUMENT_NAME_MAX_LENGTH, PageQuery, keyset_page
from docchat_api.routes.admin_models import (
    DocumentDetail,
    DocumentSummary,
    DocumentSummaryPage,
    ReingestRequest,
    document_detail,
    document_summary,
)
from docchat_api.routes.ingestion_problems import raise_refusal
from docchat_core.ingestion.models import IngestionRefusedError, RunKind
from docchat_core.models import CanonicalUuid
from docchat_core.sql.repositories import documents
from docchat_core.sql.repositories.documents import DocumentFilters, DocumentPosition

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-documents"
DOCUMENT_ID: Final = to_camel("document_id")
"""The document path parameter, named as the models' alias generator names every member; the
chunk index has a field of the same spelling, whose name only `docchat_core.search.schema`
writes as a literal (T-107)."""

LIST_REFUSALS: Final = frozenset({"index_not_found"})
READ_REFUSALS: Final = frozenset({"index_not_found", "document_not_found"})
DELETE_REFUSALS: Final = frozenset(
    {"index_not_found", "document_not_found", "precondition_failed", "index_busy"}
)
REINGEST_REFUSALS: Final = frozenset(
    {
        "index_not_found",
        "document_not_found",
        "precondition_failed",
        "no_current_version",
        "index_busy",
    }
)

Process = Annotated[ApiProcess, Depends(current_process)]
Queue = Annotated[IngestionQueuePort, Depends(ingestion_queue)]
IfMatch = Annotated[IfMatchHeader, Depends(if_match_header)]

DisplayStateFilter = Literal["ingested", "in_progress", "failed"]


_WHITESPACE_RE: Final = re.compile(r"\s+")


def normalise_prefix(value: object) -> object:
    """A name prefix normalised as document names are (section 3.11.5): NFC, trimmed, each run
    of whitespace one space, case-folded. Unlike a name it isn't refused for its characters: a
    prefix no stored name can start with just matches nothing."""
    if isinstance(value, str):
        return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", value).strip()).casefold()
    return value


NamePrefix = Annotated[
    str,
    BeforeValidator(normalise_prefix),
    Field(min_length=1, max_length=DOCUMENT_NAME_MAX_LENGTH),
]


# --- adminListDocuments ---------------------------------------------------------------------


class ListDocumentsQuery(PageQuery):
    state: DisplayStateFilter | None = None
    q: NamePrefix | None = None


class DocumentsCursor(CursorModel):
    """`{"v":1,"n":"<normalised name>","i":"<documentId>","s":"<state>" or null,
    "q":"<normalised q>" or null}`; `s` and `q` are the filters it was issued for."""

    n: Annotated[str, Field(min_length=1, max_length=DOCUMENT_NAME_MAX_LENGTH)]
    i: CanonicalUuid
    s: str | None
    q: str | None


@router.get(
    "/admin/indexes/{indexId}/documents",
    operation_id="adminListDocuments",
    tags=[TAG],
    response_model=DocumentSummaryPage,
)
@access("admin", scoped_params=["indexId"], error_codes=["index_not_found"])
async def admin_list_documents(
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    query: Annotated[ListDocumentsQuery, Query()],
    process: Process,
) -> DocumentSummaryPage:
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(
            query.cursor, DocumentsCursor, filters={"s": query.state, "q": query.q}
        )
        after = DocumentPosition(normalized_name=cursor.n, document_id=cursor.i)
    try:
        rows = await documents.read_documents(
            process.sql,
            index_id=index_id,
            filters=DocumentFilters(display_state=query.state, name_prefix=query.q),
            after=after,
            limit_plus_one=query.limit + 1,
        )
    except IngestionRefusedError as refusal:
        raise_refusal(refusal, LIST_REFUSALS)
    page, cursor_text = keyset_page(
        rows,
        query.limit,
        lambda row: DocumentsCursor(n=row.normalized_name, i=row.id, s=query.state, q=query.q),
    )
    return DocumentSummaryPage(
        items=[document_summary(row) for row in page], next_cursor=cursor_text
    )


# --- adminGetDocument -----------------------------------------------------------------------


@router.get(
    "/admin/indexes/{indexId}/documents/{documentId}",  # noqa: FAST003 - alias DOCUMENT_ID
    operation_id="adminGetDocument",
    tags=[TAG],
    response_model=DocumentDetail,
)
@access(
    "admin",
    scoped_params=["indexId", DOCUMENT_ID],
    error_codes=["index_not_found", "document_not_found"],
)
async def admin_get_document(
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    document_id: Annotated[CanonicalUuid, PathParam(alias=DOCUMENT_ID)],
    process: Process,
) -> DocumentDetail:
    try:
        detail = await documents.read_document(
            process.sql, index_id=index_id, document_id=document_id
        )
    except IngestionRefusedError as refusal:
        raise_refusal(refusal, READ_REFUSALS)
    return document_detail(detail)


# --- adminDeleteDocument --------------------------------------------------------------------


class DocumentDeleteStarted(ResponseModel):
    run_id: UUID


@router.delete(
    "/admin/indexes/{indexId}/documents/{documentId}",  # noqa: FAST003 - alias DOCUMENT_ID
    operation_id="adminDeleteDocument",
    tags=[TAG],
    response_model=DocumentDeleteStarted,
    status_code=202,
)
@access(
    "admin",
    scoped_params=["indexId", DOCUMENT_ID],
    error_codes=["index_not_found", "document_not_found", "index_busy"],
    if_match=True,
)
async def admin_delete_document(
    *,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    document_id: Annotated[CanonicalUuid, PathParam(alias=DOCUMENT_ID)],
    if_match: IfMatch,
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
    queue: Queue,
) -> DocumentDeleteStarted:
    with RunSubmission() as submission:
        try:
            deleted = await documents.delete_document(
                process.sql,
                index_id=index_id,
                document_id=document_id,
                check_version=if_match.require_row_version,
                requested_by=principal.oid,
                trace=submission.trace,
            )
        except IngestionRefusedError as refusal:
            raise_refusal(refusal, DELETE_REFUSALS)
        submission.record(
            index_id=index_id, run_id=deleted.run_id, kind=RunKind.DOCUMENT_DELETE, item_count=1
        )
        report_audit(request, deleted.events, admin=True)
        await submission.send(queue, deleted.run_id)
    return DocumentDeleteStarted(run_id=deleted.run_id)


# --- adminReingestDocument ------------------------------------------------------------------


class DocumentReingestStarted(ResponseModel):
    """`runId` is the new run (`202`) or the newest open run that already holds an item for
    the current version and target (`200`); `itemCount` 1 or 0 accordingly."""

    document: DocumentSummary
    run_id: UUID
    item_count: Annotated[int, Field(ge=0, le=1)]


@router.post(
    "/admin/indexes/{indexId}/documents/{documentId}/reingest",  # noqa: FAST003 - see above
    operation_id="adminReingestDocument",
    tags=[TAG],
    response_model=DocumentReingestStarted,
    status_code=202,
    responses={200: {"model": DocumentReingestStarted, "description": "OK"}},
)
@access(
    "admin",
    scoped_params=["indexId", DOCUMENT_ID],
    error_codes=["index_not_found", "document_not_found", "no_current_version", "index_busy"],
    if_match=True,
)
async def admin_reingest_document(
    *,
    index_id: Annotated[CanonicalUuid, PathParam(alias="indexId")],
    document_id: Annotated[CanonicalUuid, PathParam(alias=DOCUMENT_ID)],
    body: ReingestRequest,
    if_match: IfMatch,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    process: Process,
    queue: Queue,
) -> DocumentReingestStarted:
    with RunSubmission() as submission:
        try:
            reingested = await documents.reingest_document(
                process.sql,
                index_id=index_id,
                document_id=document_id,
                check_version=if_match.require_row_version,
                requested_by=principal.oid,
                trace=submission.trace,
            )
        except IngestionRefusedError as refusal:
            raise_refusal(refusal, REINGEST_REFUSALS)
        report_audit(request, reingested.events, admin=True)
        if reingested.created:
            submission.record(
                index_id=index_id,
                run_id=reingested.run_id,
                kind=RunKind.DOCUMENT_REINGEST,
                item_count=reingested.item_count,
            )
            await submission.send(queue, reingested.run_id)
        else:
            response.status_code = 200
    return DocumentReingestStarted(
        document=document_summary(reingested.document),
        run_id=reingested.run_id,
        item_count=reingested.item_count,
    )
