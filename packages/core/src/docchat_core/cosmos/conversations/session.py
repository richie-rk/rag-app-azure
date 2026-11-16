"""The `session` document (`80-data-model.md` section 3.4; `13-conversations-and-memory.md`
sections 1.5, 2.3 and 6; ADR 0071).

One per session, `id` = `sessionId`. Besides the session list's fields it carries the turn guard
`activeTurn`, the unsummarised-history counters, the rolling summary and the summary job's lease
and backoff state, so any API instance can finish work another one started.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from docchat_core.cosmos.base import CosmosDocument, CosmosModel
from docchat_core.cosmos.conversations.turn import (
    SCHEMA_VERSION,
    SEQ_MAX,
    InternalErrorCode,
    ModelCallUsage,
    Name,
)
from docchat_core.models import CanonicalUuid, Count, UtcDateTime

TITLE_MAX_CODE_POINTS: Final = 80
SUMMARY_MAX_CODE_POINTS: Final = 6_000
SUMMARY_JOB_FAILURES_MAX: Final = 1_000
SESSION_MAX_BYTES: Final = 64_000


class ActiveTurn(CosmosModel):
    """The turn in progress; a new turn starts only after it ends or its deadline passes."""

    turn_id: CanonicalUuid
    deadline_at: UtcDateTime


class SessionHistory(CosmosModel):
    """`historyTokens` summed, and counted, over the `completed` turns the summary doesn't cover."""

    unsummarised_tokens: Count = 0
    unsummarised_turns: Count = 0


class SessionSummary(CosmosModel):
    """The rolling summary; the defaults are its state before the first rebuild."""

    text: (
        Annotated[str, StringConstraints(min_length=1, max_length=SUMMARY_MAX_CODE_POINTS)] | None
    ) = None
    tokens: Count = 0
    covers_through_seq: Annotated[int, Field(ge=0, le=SEQ_MAX)] = 0
    version: Count = 0
    updated_at: UtcDateTime | None = None
    deployment: Name | None = None
    last_usage: ModelCallUsage | None = None


class SummaryJob(CosmosModel):
    """The summary rebuild's lease and backoff state; every field starts null or 0."""

    lease_expires_at: UtcDateTime | None = None
    consecutive_failures: Annotated[int, Field(ge=0, le=SUMMARY_JOB_FAILURES_MAX)] = 0
    not_before: UtcDateTime | None = None
    last_error_code: InternalErrorCode | None = None
    last_attempt_at: UtcDateTime | None = None
    last_succeeded_at: UtcDateTime | None = None


class SessionDocument(CosmosDocument):
    """One session; `projectId` and `title` never change."""

    max_bytes: ClassVar[int] = SESSION_MAX_BYTES

    id: CanonicalUuid
    user_id: CanonicalUuid
    session_id: CanonicalUuid
    type: Literal["session"] = "session"
    schema_version: Literal[1] = SCHEMA_VERSION
    project_id: CanonicalUuid
    title: Annotated[str, StringConstraints(min_length=1, max_length=TITLE_MAX_CODE_POINTS)]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    last_turn_at: UtcDateTime
    turn_count: Annotated[int, Field(ge=1, le=SEQ_MAX)]
    active_turn: ActiveTurn | None = None
    history: SessionHistory = Field(default_factory=SessionHistory)
    summary: SessionSummary = Field(default_factory=SessionSummary)
    summary_job: SummaryJob = Field(default_factory=SummaryJob)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.id != self.session_id:
            raise ValueError("a session's id is its sessionId")
        if self.summary.covers_through_seq > self.turn_count:
            raise ValueError("the summary covers only turns that exist")
        return self
