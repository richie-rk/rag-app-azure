"""`listSessions`, `getSession` and `listSessionTurns` (`13-conversations-and-memory.md` sections
4.1 to 4.6 and 5; `11-api.md` sections 1.8, 3.4.1 to 3.4.3 and 4.5; `82-security.md` SEC-15 and
SEC-18).

Every read names the caller's own `oid` as the partition key, so another user's session ID
addresses nothing; and every read follows project reachability now, so a session whose project
the caller can't reach (a removed membership, an archived project) is hidden: absent from the
list, which filters by the reachable projects at query time, and `404 session_not_found` on the
two reads, with the same body as a session that doesn't exist. Nothing is changed, so a hidden
session reappears unchanged when access returns.

- `listSessions`: the reachable projects (or the one `projectId` asked for, `404
  project_not_found` when unreachable), then one query on the caller's `[oid]` prefix, keyset
  paged on (`lastTurnAt`, `sessionId`) descending; an empty filter answers an empty page without
  a Cosmos DB call.
- `getSession`: the session point read and the project check, then the newest `turnsLimit`
  turns, ascending, with a cursor for the earlier ones.
- `listSessionTurns`: the same checks, then one page of older turns, `seq` below the cursor's
  `b` (an integer from 2 to `turnCount + 1`).

A turn leaves the server only as `TurnView`, 13 keys: never document, version, chunk-set or index
IDs, queries, retrieval results, the trace, usage, the finish reason, the extraction state, the
client turn ID, the trace ID or the deadline. A turn still `streaming` past its deadline belongs
to a process that died and reads as `failed` with `turn_abandoned` before anything stores that.
Titles are derived from the first question when the session is created and never change
(`ConversationStore`, section 5).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam
from opentelemetry.trace import Span
from pydantic import Field, ValidationError

from docchat_api.chat.models import Citation
from docchat_api.chat.stream import HttpsLink
from docchat_api.conversations.feedback_routes import FeedbackView
from docchat_api.conversations.store import (
    TURN_ABANDONED,
    SessionNotFound,
    problem_on_failure,
)
from docchat_api.conversations.telemetry import ChargeOperation, Charges
from docchat_api.http.access import access
from docchat_api.http.models import QueryModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor, encode_cursor
from docchat_api.http.problems import InternalError, ValidationFailed
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_core.access import ProjectNotFound
from docchat_core.auth import Principal
from docchat_core.cosmos.base import CosmosModel
from docchat_core.cosmos.conversations import (
    ANSWER_MAX_CODE_POINTS,
    QUESTION_MAX_CODE_POINTS,
    SEQ_MAX,
    TITLE_MAX_CODE_POINTS,
    Feedback,
    SessionDocument,
    StoredCitation,
    TurnError,
    TurnStatus,
)
from docchat_core.errors import FieldIssue
from docchat_core.models import CanonicalUuid, UtcDateTime
from docchat_core.telemetry import set_attributes, start_span

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "sessions"
PAGE_LIMIT_MAX: Final = 50
DEFAULT_PAGE_LIMIT: Final = 20
CITATIONS_MAX: Final = 16
ALLOWED_LINKS_MAX: Final = 50
FOLLOWUPS_MAX: Final = 3
FOLLOWUP_MAX_LENGTH: Final = 200
FIRST_EARLIER_PAGE: Final = 2
"""The smallest `b` a turn cursor can hold: a page of the turns below `seq` 2."""

TurnErrorCode = Literal[
    "conversation_unavailable",
    "retrieval_unavailable",
    "source_screening_unavailable",
    "prompt_too_large",
    "question_filtered",
    "answer_filtered",
    "model_busy",
    "model_unavailable",
    "model_request_rejected",
    "answer_incomplete",
    "turn_timeout",
    "turn_save_failed",
    "service_restarting",
    "internal_error",
    "turn_abandoned",
]
"""`TurnView.errorCode`: a stream code of `11-api.md` section 2.3 (the codes a failed turn is
stored with) or `turn_abandoned`; one enumeration, so the client's generated schema names every
value."""

PageLimit = Annotated[int, Field(ge=1, le=PAGE_LIMIT_MAX)]
SessionIdPath = Annotated[CanonicalUuid, PathParam(alias="sessionId")]


# --- Request models and cursors ----------------------------------------------------------------


class ListSessionsQuery(QueryModel):
    limit: PageLimit = DEFAULT_PAGE_LIMIT
    cursor: CursorText | None = None
    project_id: CanonicalUuid | None = None


class GetSessionQuery(QueryModel):
    turns_limit: PageLimit = DEFAULT_PAGE_LIMIT


class ListSessionTurnsQuery(QueryModel):
    limit: PageLimit = DEFAULT_PAGE_LIMIT
    cursor: CursorText | None = None


class SessionsCursor(CursorModel):
    """`{"v":1,"t":"<lastTurnAt>","s":"<sessionId>","p":"<projectId>" or null}`: the last
    session returned and the `projectId` filter the cursor was issued for."""

    t: UtcDateTime
    s: CanonicalUuid
    p: CanonicalUuid | None


class TurnsCursor(CursorModel):
    """`{"v":1,"b":<seq>}`: the next page holds the turns with `seq < b`."""

    b: Annotated[int, Field(ge=FIRST_EARLIER_PAGE, le=SEQ_MAX + 1)]


# --- Response models ---------------------------------------------------------------------------


class SessionSummary(ResponseModel):
    session_id: UUID
    project_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=TITLE_MAX_CODE_POINTS)]
    created_at: UtcDateTime
    last_turn_at: UtcDateTime
    turn_count: Annotated[int, Field(ge=1, le=SEQ_MAX)]


class SessionPage(ResponseModel):
    items: Annotated[list[SessionSummary], Field(max_length=PAGE_LIMIT_MAX)]
    next_cursor: CursorText | None


class TurnView(ResponseModel):
    """What a stored turn shows: exactly these 13 keys."""

    turn_id: UUID
    seq: Annotated[int, Field(ge=1, le=SEQ_MAX)]
    status: TurnStatus
    question: Annotated[str, Field(min_length=1, max_length=QUESTION_MAX_CODE_POINTS)]
    answer: Annotated[str, Field(max_length=ANSWER_MAX_CODE_POINTS)]
    answer_truncated: bool
    error_code: TurnErrorCode | None
    citations: Annotated[list[Citation], Field(max_length=CITATIONS_MAX)]
    allowed_links: Annotated[list[HttpsLink], Field(max_length=ALLOWED_LINKS_MAX)]
    followups: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=FOLLOWUP_MAX_LENGTH)]],
        Field(max_length=FOLLOWUPS_MAX),
    ]
    feedback: FeedbackView | None
    created_at: UtcDateTime
    ended_at: UtcDateTime | None


class SessionDetail(ResponseModel):
    session_id: UUID
    project_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=TITLE_MAX_CODE_POINTS)]
    created_at: UtcDateTime
    last_turn_at: UtcDateTime
    turn_count: Annotated[int, Field(ge=1, le=SEQ_MAX)]
    turns: Annotated[list[TurnView], Field(max_length=PAGE_LIMIT_MAX)]
    earlier_turns_cursor: CursorText | None


class TurnPage(ResponseModel):
    items: Annotated[list[TurnView], Field(max_length=PAGE_LIMIT_MAX)]
    next_cursor: CursorText | None


# --- Turn rows and the turn view ---------------------------------------------------------------


class TurnRow(CosmosModel):
    """One row of the turn query: the stored fields the view is built from."""

    id: CanonicalUuid
    seq: int
    status: TurnStatus
    question: str
    answer: str
    answer_truncated: bool
    error: TurnError | None
    citations: list[StoredCitation]
    allowed_links: list[str]
    followups: list[str]
    feedback: Feedback | None
    created_at: UtcDateTime
    ended_at: UtcDateTime | None
    deadline_at: UtcDateTime

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> TurnRow:
        return cls.model_validate(row, extra="ignore")


def turn_view(row: TurnRow, now: datetime) -> TurnView:
    """The client's view of a stored turn; a `streaming` turn past its deadline reads as
    `failed` with `turn_abandoned`."""
    status = row.status
    error_code: str | None = None if row.error is None else row.error.code
    if status is TurnStatus.STREAMING and row.deadline_at <= now:
        status = TurnStatus.FAILED
        error_code = TURN_ABANDONED
    return TurnView.model_validate(
        {
            "turn_id": row.id,
            "seq": row.seq,
            "status": status,
            "question": row.question,
            "answer": row.answer,
            "answer_truncated": row.answer_truncated,
            "error_code": error_code,
            "citations": [Citation.of(citation.view()) for citation in row.citations],
            "allowed_links": row.allowed_links,
            "followups": row.followups,
            "feedback": None if row.feedback is None else FeedbackView.of(row.feedback),
            "created_at": row.created_at,
            "ended_at": row.ended_at,
        }
    )


def turn_views(rows: Sequence[Mapping[str, Any]], limit: int, now: datetime) -> list[TurnView]:
    """The first `limit` rows of a newest-first query, oldest first, as views; a stored turn
    the models refuse is a defect, answered `500 internal_error`."""
    try:
        return [turn_view(TurnRow.of(row), now) for row in reversed(rows[:limit])]
    except ValidationError as exc:
        raise InternalError() from exc


def earlier_cursor(views: Sequence[TurnView]) -> str:
    """The cursor of the page before the oldest of `views`."""
    return encode_cursor(TurnsCursor(b=views[0].seq))


# --- Shared steps ------------------------------------------------------------------------------


@contextmanager
def _read_span(
    name: str, session_id: UUID, charges: Charges, operation: ChargeOperation
) -> Iterator[Span]:
    with start_span(name, **{"docchat.session_id": str(session_id)}) as span:
        try:
            yield span
        except SessionNotFound:
            set_attributes(span, **{"docchat.outcome": "not_found"})
            raise
        else:
            set_attributes(span, **{"docchat.outcome": "ok"})
        finally:
            set_attributes(span, **{"docchat.cosmos.request_charge": charges.record(operation)})


async def readable_session(
    process: ApiProcess, principal: Principal, session_id: UUID, charges: Charges
) -> SessionDocument:
    """The session in the caller's partition, in a project the caller reaches now; otherwise
    `404 session_not_found`, one body for every cause."""
    with problem_on_failure():
        session = await process.conversations.read_session(principal, session_id, charges)
    if session is None:
        raise SessionNotFound()
    try:
        await process.access.authorize_project_for_chat(principal, session.project_id)
    except ProjectNotFound:
        raise SessionNotFound() from None
    return session


async def turn_page(
    process: ApiProcess,
    principal: Principal,
    session: SessionDocument,
    *,
    before: int,
    limit: int,
    charges: Charges,
) -> tuple[list[TurnView], str | None]:
    """The `limit` turns below `seq` `before`, oldest first, and the cursor of the page before
    them, `None` when they include the oldest turn."""
    with problem_on_failure():
        rows = await process.conversations.list_turn_rows(
            principal, session.session_id, before=before, take=limit + 1, charges=charges
        )
    views = turn_views(rows, limit, process.conversations.clock.now())
    cursor = earlier_cursor(views) if len(rows) > limit else None
    return views, cursor


def _invalid_cursor() -> ValidationFailed:
    return ValidationFailed(errors=[FieldIssue("query.cursor", "invalid_cursor")])


# --- The operations ----------------------------------------------------------------------------


@router.get(
    "/sessions",
    operation_id="listSessions",
    tags=[TAG],
    response_model=SessionPage,
)
@access("user", scoped_params=("projectId",), error_codes=["project_not_found"])
async def list_sessions(
    principal: CurrentPrincipal,
    query: Annotated[ListSessionsQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> SessionPage:
    charges = Charges()
    with start_span(
        "docchat.sessions.list",
        **{
            "docchat.page.limit": query.limit,
            "docchat.filter.project": query.project_id is not None,
        },
    ) as span:
        try:
            page = await _session_page(process, principal, query, charges, span)
        finally:
            set_attributes(
                span, **{"docchat.cosmos.request_charge": charges.record("list_sessions")}
            )
        set_attributes(span, **{"docchat.page.returned": len(page.items)})
    return page


async def _session_page(
    process: ApiProcess,
    principal: Principal,
    query: ListSessionsQuery,
    charges: Charges,
    span: Span,
) -> SessionPage:
    after = None
    if query.cursor is not None:
        cursor = decode_cursor(query.cursor, SessionsCursor, filters={"p": query.project_id})
        after = (cursor.t, cursor.s)
    reachable = await process.access.list_reachable_project_ids(principal)
    set_attributes(span, **{"docchat.projects.reachable": len(reachable)})
    if query.project_id is not None:
        if query.project_id not in reachable:
            raise ProjectNotFound()
        project_ids: list[UUID] = [query.project_id]
    else:
        project_ids = sorted(reachable)
    if not project_ids:
        return SessionPage(items=[], next_cursor=None)
    with problem_on_failure():
        rows = await process.conversations.list_session_rows(
            principal, project_ids=project_ids, take=query.limit + 1, after=after, charges=charges
        )
    try:
        items = [SessionSummary.model_validate(row) for row in rows[: query.limit]]
    except ValidationError as exc:
        raise InternalError() from exc
    next_cursor = None
    if len(rows) > query.limit:
        last = items[-1]
        next_cursor = encode_cursor(
            SessionsCursor(t=last.last_turn_at, s=last.session_id, p=query.project_id)
        )
    return SessionPage(items=items, next_cursor=next_cursor)


@router.get(
    "/sessions/{sessionId}",
    operation_id="getSession",
    tags=[TAG],
    response_model=SessionDetail,
)
@access("user", scoped_params=("sessionId",), error_codes=["session_not_found"])
async def get_session(
    *,
    session_id: SessionIdPath,
    principal: CurrentPrincipal,
    query: Annotated[GetSessionQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> SessionDetail:
    charges = Charges()
    with _read_span("docchat.sessions.get", session_id, charges, "get_session") as span:
        session = await readable_session(process, principal, session_id, charges)
        turns, cursor = await turn_page(
            process,
            principal,
            session,
            before=session.turn_count + 1,
            limit=query.turns_limit,
            charges=charges,
        )
        set_attributes(span, **{"docchat.page.returned": len(turns)})
    return SessionDetail(
        session_id=session.session_id,
        project_id=session.project_id,
        title=session.title,
        created_at=session.created_at,
        last_turn_at=session.last_turn_at,
        turn_count=session.turn_count,
        turns=turns,
        earlier_turns_cursor=cursor,
    )


@router.get(
    "/sessions/{sessionId}/turns",
    operation_id="listSessionTurns",
    tags=[TAG],
    response_model=TurnPage,
)
@access("user", scoped_params=("sessionId",), error_codes=["session_not_found"])
async def list_session_turns(
    *,
    session_id: SessionIdPath,
    principal: CurrentPrincipal,
    query: Annotated[ListSessionTurnsQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> TurnPage:
    charges = Charges()
    before: int | None = None
    if query.cursor is not None:
        before = decode_cursor(query.cursor, TurnsCursor).b
    with _read_span("docchat.sessions.list_turns", session_id, charges, "list_turns") as span:
        session = await readable_session(process, principal, session_id, charges)
        if before is None:
            before = session.turn_count + 1
        elif before > session.turn_count + 1:
            raise _invalid_cursor()
        turns, cursor = await turn_page(
            process, principal, session, before=before, limit=query.limit, charges=charges
        )
        set_attributes(span, **{"docchat.page.returned": len(turns)})
    return TurnPage(items=turns, next_cursor=cursor)
