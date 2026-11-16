"""`createChatTurn` — `POST /v1/chat/turns`, and its checks before the stream starts
(`12-chat-pipeline.md` sections 1.2, 2, 3 and 4; `11-api.md` section 3.3.1).

`DocChatRoute` runs P1 and P2 (token, role and provisioning; the 32,768-byte body, its media type
and `ChatTurnRequest`); this route declares no burst or write limit, because P3 to P5 are its
limits. The endpoint then records the receipt time, takes the turn's one ops-config snapshot and
runs, in order, the first failure winning:

- P3 the request rate, P4 the token quota, P5 the stream lease (the `usage` container; each
  fails closed with `503 dependency_unavailable`);
- P6 `authorize_project_for_chat` and P7 `resolve_chat_index` (SQL);
- P8 `ConversationStore.begin_turn`, whose refusals carry `sessionId` (and `turnId`);
- P9 the turn task and the NDJSON response.

A lease acquired at P5 is released before the answer to any later refusal and before an
unexpected fault becomes `500 internal_error`. No model, search or screening call happens before
`200`; after it every failure is an `error` event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from docchat_api.chat.models import REQUEST_EXAMPLE, ChatTurnRequest
from docchat_api.chat.stream import NDJSON_MEDIA_TYPE, ChatStreamEvent
from docchat_api.chat.turn import ChatTurn, ReplayTurn, TurnRequest
from docchat_api.conversations.store import ConversationError, Replay, TurnStart
from docchat_api.http.access import access
from docchat_api.http.context import correlation_id
from docchat_api.http.problems import DependencyUnavailable, InternalError
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute, route_options
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_api.openapi import ndjson_stream
from docchat_api.usage import Lease
from docchat_core.access import AccessibleProject, ResolvedIndex
from docchat_core.auth import Principal
from docchat_core.cosmos.conversations import new_session_id, turn_id
from docchat_core.errors import DocChatProblem
from docchat_core.opsconfig import OpsConfig
from docchat_core.sql.engine import SqlTimeout, SqlUnavailable
from docchat_core.telemetry import set_attributes, start_span

router = APIRouter(route_class=DocChatRoute)

CHAT_BODY_LIMIT: Final = 32_768
CHAT_ERROR_CODES: Final = (
    "project_not_found",
    "session_not_found",
    "project_index_unavailable",
    "turn_in_progress",
    "turn_already_ended",
    "client_turn_id_conflict",
    "session_turn_limit",
    "rate_limited",
    "too_many_streams",
    "token_quota_exceeded",
)
ACCEPTED: Final = "accepted"


@dataclass(frozen=True, slots=True)
class Accepted:
    """What P3 to P8 established."""

    lease: Lease
    project: AccessibleProject
    index: ResolvedIndex
    start: TurnStart


async def _prestream(
    process: ApiProcess,
    principal: Principal,
    body: ChatTurnRequest,
    ops: OpsConfig,
    *,
    session_id: UUID,
    received_at: datetime,
    correlation: str,
) -> Accepted:
    """P3 to P8; a lease taken at P5 is released before any later refusal leaves."""
    await process.usage.check_chat_rate(principal.oid, ops.ratelimit)
    await process.usage.check_quota(principal.oid, ops.quota)
    lease = await process.usage.acquire_stream_lease(
        principal.oid,
        session_id=session_id,
        turn_id=turn_id(session_id, body.client_turn_id),
        ratelimit=ops.ratelimit,
        chat=ops.chat,
    )
    try:
        project = await process.access.authorize_project_for_chat(principal, body.project_id)
        index = await process.access.resolve_chat_index(principal, project)
        try:
            start = await process.conversations.begin_turn(
                principal,
                project_id=project.id,
                session_id=body.session_id,
                client_turn_id=body.client_turn_id,
                question=body.question,
                received_at=received_at,
                trace_id=correlation,
                ops=ops,
            )
        except ConversationError as error:
            raise error.problem() from error
    except BaseException:
        await lease.release()
        raise
    return Accepted(lease, project, index, start)


def _refusal(failure: BaseException) -> str | None:
    """The prestream span's outcome for a refusal: its code."""
    if isinstance(failure, DocChatProblem):
        return failure.entry.code
    if isinstance(failure, SqlUnavailable | SqlTimeout):
        return DependencyUnavailable.code
    if isinstance(failure, Exception):
        return InternalError.code
    return None


@router.post(
    "/chat/turns",
    operation_id="createChatTurn",
    tags=["chat"],
    response_class=StreamingResponse,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": REQUEST_EXAMPLE}}}},
)
@route_options(body_limit=CHAT_BODY_LIMIT, burst_limited=False, write_limited=False)
@access(
    "user",
    scoped_params=["body:/projectId", "body:/sessionId"],
    error_codes=CHAT_ERROR_CODES,
)
@ndjson_stream("ChatStreamEvent", ChatStreamEvent)
async def create_chat_turn(
    request: Request,
    body: ChatTurnRequest,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
) -> StreamingResponse:
    chat = process.chat
    received_at = chat.clock.now()
    received_monotonic = chat.clock.monotonic()
    ops = await request_ops(request)
    session_id = body.session_id or new_session_id(principal.oid, body.client_turn_id)
    correlation = correlation_id()
    with start_span(
        "docchat.chat.prestream",
        **{"docchat.project_id": str(body.project_id), "docchat.session_id": str(session_id)},
    ) as span:
        try:
            accepted = await _prestream(
                process,
                principal,
                body,
                ops,
                session_id=session_id,
                received_at=received_at,
                correlation=correlation,
            )
        except BaseException as failure:
            refusal = _refusal(failure)
            if refusal is not None:
                set_attributes(span, **{"docchat.outcome": refusal})
            raise
        set_attributes(span, **{"docchat.outcome": ACCEPTED})
    start = accepted.start
    if isinstance(start, Replay):
        replay = ReplayTurn(
            chat,
            turn=start.turn,
            lease=accepted.lease,
            correlation_id=correlation,
            received_monotonic=received_monotonic,
        )
        replay.start()
        return StreamingResponse(replay.writer.lines(), media_type=NDJSON_MEDIA_TYPE)
    turn = ChatTurn(
        chat,
        TurnRequest(
            principal=principal,
            project=accepted.project,
            index=accepted.index,
            question=body.question,
            received_at=received_at,
            received_monotonic=received_monotonic,
            correlation_id=correlation,
            ops=ops,
            lease=accepted.lease,
        ),
        start,
    )
    turn.start()
    return StreamingResponse(turn.writer.lines(), media_type=NDJSON_MEDIA_TYPE)
