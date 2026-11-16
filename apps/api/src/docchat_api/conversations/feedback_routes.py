"""`putTurnFeedback` — `PUT /v1/sessions/{sessionId}/turns/{turnId}/feedback`
(`13-conversations-and-memory.md` sections 4.1, 4.6 and 8; `11-api.md` sections 3.4.4 and 4.5;
`82-security.md` SEC-18).

A rating is the turn owner's opinion of an answer, stored on the turn itself, so it shares the
turn's partition, retention and deletion. After the route class's checks (the burst and write
limits among them) and body validation, in order:

1. the body's own rules, `422 validation_failed`: a category with rating `up`
   (`category_requires_down` at `body.category`); the comment normalised (NFC, CR LF and lone CR to
   LF, Cc other than LF and TAB and every Cf, Co and Cs code point removed, trimmed, empty to
   null) and then above 2,000 code points (`too_long`), or present while
   `feedback.comments_enabled` is off (`comments_disabled`), at `body.comment`;
2. the turn, one point read in the caller's partition `[oid, sessionId]`, and its project's
   reachability: a missing session, a missing turn and a hidden session all answer
   `404 turn_not_found`;
3. feedback only on a `completed` turn or a `cancelled` one whose answer holds text, else
   `409 feedback_not_allowed`;
4. one patch of `/feedback` and `/updatedAt` conditioned on the ETag just read; a `412` (a
   follow-up attach or an extraction-state patch landed in between) goes back to step 2, at most
   3 attempts, then `503 dependency_unavailable`.

A `PUT` carries the whole value and the last write wins, keeping the first save's `createdAt`;
there is no `If-Match` and no audit row. After the commit: the event `turn_feedback_saved` and the
counter `docchat.feedback.saved`, which never carry the comment.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from opentelemetry.trace import Span
from pydantic import Field

from docchat_api.conversations.store import (
    ConversationStore,
    StoredTurn,
    problem_on_failure,
)
from docchat_api.conversations.telemetry import Charges
from docchat_api.http.access import access
from docchat_api.http.models import RequestModel, ResponseModel
from docchat_api.http.problems import DependencyUnavailable, ValidationFailed
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_core.access import ProjectNotFound
from docchat_core.auth import Principal
from docchat_core.cosmos.conversations import (
    Feedback,
    FeedbackCategory,
    FeedbackRating,
    TurnDocument,
    TurnStatus,
)
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.models import CanonicalUuid, UtcDateTime
from docchat_core.opsconfig import OpsConfig
from docchat_core.sql.engine import SqlTimeout, SqlUnavailable
from docchat_core.telemetry import emit_event, set_attributes, start_span
from docchat_core.telemetry.instruments import counter

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "sessions"
FEEDBACK_PATH: Final = "/sessions/{sessionId}/turns/{turnId}/feedback"
COMMENT_MAX_CHARACTERS: Final = 8_000
"""The comment as sent: four times the stored limit, room for what normalisation removes."""
COMMENT_MAX_CODE_POINTS: Final = 2_000
FEEDBACK_ATTEMPTS: Final = 3
NO_CATEGORY: Final = "none"
SPAN: Final = "docchat.feedback.put"
SAVED_EVENT: Final = "turn_feedback_saved"
REQUEST_EXAMPLE: Final[dict[str, Any]] = {
    "rating": "down",
    "category": "incomplete",
    "comment": "The answer left out the second approval step.",
}
"""The OpenAPI request example of `11-api.md` section 3.4.4."""

FEEDBACK_SAVED: Final = counter("docchat.feedback.saved")

_KEPT_CONTROLS: Final = frozenset("\n\t")
_REMOVED_CATEGORIES: Final = frozenset({"Cf", "Co", "Cs"})


class TurnNotFound(DocChatProblem):
    """`404 turn_not_found`: one body for a missing session, a missing turn and a session whose
    project the caller can't reach now."""

    code = "turn_not_found"


class FeedbackNotAllowed(DocChatProblem):
    """`409 feedback_not_allowed`: the turn is `streaming`, `failed`, or `cancelled` without an
    answer."""

    code = "feedback_not_allowed"


SessionIdPath = Annotated[CanonicalUuid, PathParam(alias="sessionId")]
TurnIdPath = Annotated[CanonicalUuid, PathParam(alias="turnId")]


class FeedbackRequest(RequestModel):
    """The whole feedback value: a `PUT` replaces what is stored."""

    rating: FeedbackRating
    category: FeedbackCategory | None = None
    comment: Annotated[str, Field(max_length=COMMENT_MAX_CHARACTERS)] | None = None


class FeedbackView(ResponseModel):
    """Stored feedback as the client sees it: no `createdAt`."""

    rating: FeedbackRating
    category: FeedbackCategory | None
    comment: Annotated[str, Field(min_length=1, max_length=COMMENT_MAX_CODE_POINTS)] | None
    updated_at: UtcDateTime

    @classmethod
    def of(cls, feedback: Feedback) -> FeedbackView:
        return cls(
            rating=feedback.rating,
            category=feedback.category,
            comment=feedback.comment,
            updated_at=feedback.updated_at,
        )


def normalise_comment(text: str | None) -> str | None:
    """NFC; CR LF and lone CR become LF; Cc other than LF and TAB, and Cf, Co and Cs, removed;
    leading and trailing whitespace trimmed; an empty result is `None`."""
    if text is None:
        return None
    unified = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    kept = "".join(
        character
        for character in unified
        if not (
            (unicodedata.category(character) == "Cc" and character not in _KEPT_CONTROLS)
            or unicodedata.category(character) in _REMOVED_CATEGORIES
        )
    ).strip()
    return kept or None


def checked_body(body: FeedbackRequest, ops: OpsConfig) -> str | None:
    """Step 1: the body's own rules; the normalised comment when they pass."""
    issues: list[FieldIssue] = []
    if body.category is not None and body.rating is not FeedbackRating.DOWN:
        issues.append(FieldIssue("body.category", "category_requires_down"))
    comment = normalise_comment(body.comment)
    if comment is not None and len(comment) > COMMENT_MAX_CODE_POINTS:
        issues.append(FieldIssue("body.comment", "too_long"))
    elif comment is not None and not ops.feedback.comments_enabled:
        issues.append(FieldIssue("body.comment", "comments_disabled"))
    if issues:
        raise ValidationFailed(errors=issues)
    return comment


def feedback_allowed(turn: TurnDocument) -> bool:
    """Feedback rates an answer: a `completed` turn, or a `cancelled` one with text."""
    return turn.status is TurnStatus.COMPLETED or (
        turn.status is TurnStatus.CANCELLED and bool(turn.answer)
    )


def _outcome_of(error: BaseException) -> str | None:
    if isinstance(error, DocChatProblem):
        return error.entry.code
    if isinstance(error, SqlUnavailable | SqlTimeout):
        return DependencyUnavailable.code
    return None


@contextmanager
def _feedback_span(
    session_id: UUID, turn_id: UUID, body: FeedbackRequest, charges: Charges
) -> Iterator[Span]:
    category = NO_CATEGORY if body.category is None else body.category.value
    with start_span(
        SPAN,
        **{
            "docchat.session_id": str(session_id),
            "docchat.turn_id": str(turn_id),
            "docchat.feedback.rating": body.rating.value,
            "docchat.feedback.category": category,
        },
    ) as span:
        try:
            yield span
        except BaseException as error:
            outcome = _outcome_of(error)
            if outcome is not None:
                set_attributes(span, **{"docchat.outcome": outcome})
            raise
        else:
            set_attributes(span, **{"docchat.outcome": "ok"})
        finally:
            charges.record("feedback")


async def _readable_turn(
    process: ApiProcess, principal: Principal, session_id: UUID, turn_id: UUID, charges: Charges
) -> StoredTurn:
    """Steps 2 and 3: the turn in the caller's partition, in a project reachable now, holding
    an answer."""
    with problem_on_failure():
        stored = await process.conversations.read_turn_entry(
            principal, session_id, turn_id, charges
        )
    if stored is None:
        raise TurnNotFound()
    try:
        await process.access.authorize_project_for_chat(principal, stored.turn.project_id)
    except ProjectNotFound:
        raise TurnNotFound() from None
    if not feedback_allowed(stored.turn):
        raise FeedbackNotAllowed()
    return stored


async def _save(
    store: ConversationStore,
    principal: Principal,
    stored: StoredTurn,
    body: FeedbackRequest,
    *,
    comment: str | None,
    charges: Charges,
) -> Feedback | None:
    """Step 4, one attempt: the saved feedback, or `None` after a `412`."""
    now = store.clock.now()
    previous = stored.turn.feedback
    feedback = Feedback(
        rating=body.rating,
        category=body.category,
        comment=comment,
        created_at=now if previous is None else previous.created_at,
        updated_at=now,
    )
    with problem_on_failure():
        saved = await store.patch_feedback(principal, stored, feedback, now, charges)
    return feedback if saved else None


def report_saved(turn: TurnDocument, feedback: Feedback, *, is_change: bool) -> None:
    """After the commit: the event with exactly its seven properties and the counter with only
    `rating` and `category`; never the comment."""
    category = NO_CATEGORY if feedback.category is None else feedback.category.value
    emit_event(
        SAVED_EVENT,
        rating=feedback.rating.value,
        category=category,
        projectId=str(turn.project_id),
        turnId=str(turn.id),
        sessionId=str(turn.session_id),
        isChange=is_change,
        hasComment=feedback.comment is not None,
    )
    FEEDBACK_SAVED.add(1, rating=feedback.rating.value, category=category)


@router.put(
    FEEDBACK_PATH,
    operation_id="putTurnFeedback",
    tags=[TAG],
    response_model=FeedbackView,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": REQUEST_EXAMPLE}}}},
)
@access(
    "user",
    scoped_params=("sessionId", "turnId"),
    error_codes=["turn_not_found", "feedback_not_allowed"],
)
async def put_turn_feedback(
    *,
    session_id: SessionIdPath,
    turn_id: TurnIdPath,
    body: FeedbackRequest,
    principal: CurrentPrincipal,
    process: Annotated[ApiProcess, Depends(current_process)],
    ops: Annotated[OpsConfig, Depends(request_ops)],
) -> FeedbackView:
    charges = Charges()
    with _feedback_span(session_id, turn_id, body, charges) as span:
        comment = checked_body(body, ops)
        for _ in range(FEEDBACK_ATTEMPTS):
            stored = await _readable_turn(process, principal, session_id, turn_id, charges)
            set_attributes(span, **{"docchat.project_id": str(stored.turn.project_id)})
            saved = await _save(
                process.conversations, principal, stored, body, comment=comment, charges=charges
            )
            if saved is not None:
                break
        else:
            raise DependencyUnavailable()
        is_change = stored.turn.feedback is not None
        set_attributes(span, **{"docchat.feedback.is_change": is_change})
    report_saved(stored.turn, saved, is_change=is_change)
    return FeedbackView.of(saved)
