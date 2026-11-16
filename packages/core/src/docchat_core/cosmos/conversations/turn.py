"""The `turn` document and the types nested in a turn (`80-data-model.md` sections 3.5, 3.6 and
3.12; `13-conversations-and-memory.md` sections 1.6, 1.7 and 1.10; `12-chat-pipeline.md` section
15; ADR 0071).

One `turn` per turn, `id` = the turn ID derived from the session and the request's
`clientTurnId`. It holds what the user sees and every field that changes after finalisation; the
bulky write-once diagnostics live in the turn's `turn_trace`, because Cosmos DB rewrites a whole
item on every patch.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from docchat_core.cosmos.base import CosmosDocument, CosmosModel
from docchat_core.cosmos.conversations.ids import turn_id
from docchat_core.errors import CODE_MAX_LENGTH, CODE_PATTERN
from docchat_core.models import CanonicalUuid, CorrelationId, Count, UtcDateTime

SCHEMA_VERSION: Final = 1
SEQ_MAX: Final = 5_000
"""The ceiling of `conversations.max_turns_per_session`; the store applies the configured value."""

QUESTION_MAX_CODE_POINTS: Final = 4_000
ANSWER_MAX_CODE_POINTS: Final = 100_000
CITATIONS_MAX: Final = 16
ALLOWED_LINKS_MAX: Final = 50
ALLOWED_LINK_MAX_LENGTH: Final = 2_048
FOLLOWUPS_MAX: Final = 3
FOLLOWUP_MAX_LENGTH: Final = 200
USAGE_CALLS_MAX: Final = 10
FEEDBACK_COMMENT_MAX_CODE_POINTS: Final = 2_000
CITATION_TITLE_MAX_LENGTH: Final = 300
CHUNK_KEY_MAX_LENGTH: Final = 256
HEADING_PATH_MAX_ITEMS: Final = 8
HEADING_MAX_LENGTH: Final = 200
NAME_MAX_LENGTH: Final = 64
"""Deployment and model names: the deployment-name and model-name settings allow 64 characters."""
EXTRACTION_ATTEMPTS_MAX: Final = 5
EXTRACTION_FACTS_MAX: Final = 10
EXTRACTION_SHIELD_CALLS_MAX: Final = 1_000
TURN_MAX_BYTES: Final = 350_000

Code = Annotated[str, StringConstraints(pattern=CODE_PATTERN, max_length=CODE_MAX_LENGTH)]
"""A stored error code: lower-case snake_case, at most 64 characters."""

Name = Annotated[str, StringConstraints(min_length=1, max_length=NAME_MAX_LENGTH)]
"""A deployment or model name."""

SourceId = Annotated[str, StringConstraints(pattern=r"^S(?:[1-9]|1[0-6])$")]
"""`S1` to `S16`: a source's ID in the answer prompt."""

ChunkKey = Annotated[str, StringConstraints(min_length=1, max_length=CHUNK_KEY_MAX_LENGTH)]
"""The AI Search key `{documentId}_{chunkSetId}_{ordinal}`."""

NonNegativeInt = Annotated[int, Field(ge=0)]


def _https_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        raise ValueError("an allowed link is an https URL with a host")
    return value


HttpsUrl = Annotated[
    str, StringConstraints(max_length=ALLOWED_LINK_MAX_LENGTH), AfterValidator(_https_url)
]

Followup = Annotated[str, StringConstraints(min_length=1, max_length=FOLLOWUP_MAX_LENGTH)]


class InternalErrorCode(StrEnum):
    """The internal codes stored in `summaryJob.lastErrorCode` and `memoryExtraction.errorCode`;
    never sent to a client (`13-conversations-and-memory.md` section 11.1)."""

    MODEL_UNAVAILABLE = "model_unavailable"
    CONTENT_FILTERED = "content_filtered"
    INVALID_OUTPUT = "invalid_output"
    STORE_CONFLICT = "store_conflict"


class TurnStatus(StrEnum):
    """A turn is created `streaming` and moves to exactly one terminal status."""

    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    ERROR = "error"


class CitationContentType(StrEnum):
    """The MIME type of a cited file."""

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class CitationView(CosmosModel):
    """The only citation shape that leaves the server: session reads, replays and the stream's
    `citations` event. It equals the chat pipeline's public `Citation` field for field."""

    number: Annotated[int, Field(ge=1, le=CITATIONS_MAX)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=CITATION_TITLE_MAX_LENGTH)]
    page_number: Annotated[int, Field(ge=1)]
    heading_path: Annotated[
        list[Annotated[str, StringConstraints(max_length=HEADING_MAX_LENGTH)]],
        Field(max_length=HEADING_PATH_MAX_ITEMS),
    ]
    content_type: CitationContentType
    open_as: Literal["pdf_page"]


class StoredCitation(CitationView):
    """One citation number of a turn, with the IDs that locate the cited chunk; the IDs never
    leave the server."""

    source_id: SourceId
    chunk_key: ChunkKey
    search_index_id: CanonicalUuid
    physical_index_id: CanonicalUuid
    document_id: CanonicalUuid
    document_version_id: CanonicalUuid
    chunk_set_id: CanonicalUuid

    def view(self) -> CitationView:
        """The citation as a client sees it."""
        return CitationView.model_validate(self.model_dump(include=set(CitationView.model_fields)))


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class FeedbackCategory(StrEnum):
    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    NOT_RELEVANT = "not_relevant"
    OTHER = "other"


class Feedback(CosmosModel):
    """The user's rating of a turn; `createdAt` is the first save, `updatedAt` the latest."""

    rating: FeedbackRating
    category: FeedbackCategory | None = None
    comment: (
        Annotated[str, StringConstraints(max_length=FEEDBACK_COMMENT_MAX_CODE_POINTS)] | None
    ) = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _category_only_when_down(self) -> Self:
        if self.category is not None and self.rating is not FeedbackRating.DOWN:
            raise ValueError("a feedback category is set only when the rating is down")
        return self


class ModelCall(StrEnum):
    """The model calls that produce a usage record."""

    REWRITE = "rewrite"
    SEARCH_PASSAGE = "search_passage"
    ANSWER = "answer"
    FOLLOWUPS = "followups"
    QUERY_EMBEDDING = "query_embedding"
    PASSAGE_EMBEDDING = "passage_embedding"
    SUMMARY = "summary"
    MEMORY = "memory"


class CallOutcome(StrEnum):
    OK = "ok"
    FILTERED = "filtered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ModelCallUsage(CosmosModel):
    """One Azure OpenAI call's usage, its retries included (`12-chat-pipeline.md` section 15.1).

    `completionTokens` includes reasoning tokens; `estimatedCostMicros` is `null` when the model
    has no rate.
    """

    call: ModelCall
    deployment: Name
    model: Name
    prompt_tokens: Count
    completion_tokens: Count
    reasoning_tokens: Count
    cached_tokens: Count
    cache_write_tokens: Count
    total_tokens: Count
    estimated: bool
    estimated_cost_micros: NonNegativeInt | None
    duration_ms: NonNegativeInt
    outcome: CallOutcome

    @model_validator(mode="after")
    def _total_is_prompt_plus_completion(self) -> Self:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("totalTokens is promptTokens + completionTokens")
        return self


class UsageTotals(CosmosModel):
    """A turn's totals over its usage records."""

    prompt_tokens: NonNegativeInt
    completion_tokens: NonNegativeInt
    reasoning_tokens: NonNegativeInt
    cached_tokens: NonNegativeInt
    cache_write_tokens: NonNegativeInt
    total_tokens: NonNegativeInt
    estimated_cost_micros: NonNegativeInt | None
    unpriced_calls: NonNegativeInt
    estimated: bool

    @classmethod
    def over(cls, calls: Sequence[ModelCallUsage]) -> UsageTotals:
        """The totals of `calls`: the cost is the sum, or `null` when any call had no rate."""
        unpriced = sum(1 for call in calls if call.estimated_cost_micros is None)
        return cls(
            prompt_tokens=sum(call.prompt_tokens for call in calls),
            completion_tokens=sum(call.completion_tokens for call in calls),
            reasoning_tokens=sum(call.reasoning_tokens for call in calls),
            cached_tokens=sum(call.cached_tokens for call in calls),
            cache_write_tokens=sum(call.cache_write_tokens for call in calls),
            total_tokens=sum(call.total_tokens for call in calls),
            estimated_cost_micros=None
            if unpriced
            else sum(call.estimated_cost_micros or 0 for call in calls),
            unpriced_calls=unpriced,
            estimated=any(call.estimated for call in calls),
        )


class BillableCounts(CosmosModel):
    """Counts a later cost model can price: semantic-ranker queries, Prompt Shields requests and
    the UTF-16 units they sent."""

    semantic_queries: NonNegativeInt = 0
    shield_calls: NonNegativeInt = 0
    shield_characters: NonNegativeInt = 0


class TurnUsage(CosmosModel):
    """The turn's usage records in call order, their totals and the billable counts."""

    calls: Annotated[list[ModelCallUsage], Field(max_length=USAGE_CALLS_MAX)]
    totals: UsageTotals
    billable: BillableCounts

    @classmethod
    def of(
        cls, calls: Sequence[ModelCallUsage], billable: BillableCounts | None = None
    ) -> TurnUsage:
        """Usage over `calls`, with the totals computed."""
        return cls(
            calls=list(calls),
            totals=UsageTotals.over(calls),
            billable=billable if billable is not None else BillableCounts(),
        )

    @classmethod
    def empty(cls) -> TurnUsage:
        """No calls and zero totals: a turn's usage at creation."""
        return cls.of(())

    def with_call(self, record: ModelCallUsage) -> TurnUsage:
        """This usage with one more record and the totals recomputed."""
        return TurnUsage.of([*self.calls, record], self.billable)

    @model_validator(mode="after")
    def _totals_match_calls(self) -> Self:
        if self.totals != UsageTotals.over(self.calls):
            raise ValueError("usage totals are computed over the calls")
        return self


class ExtractionState(StrEnum):
    """`memoryExtraction.state`; every state but `pending` and `running` is terminal."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED_PAUSED = "skipped_paused"
    SKIPPED_DISABLED = "skipped_disabled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FAILED = "failed"


class RejectedCounts(CosmosModel):
    """Extraction proposals rejected, counted by reason. The stored keys are the reason codes
    themselves, which are snake_case, so this type has no camelCase aliases."""

    model_config = ConfigDict(alias_generator=None)

    sensitivity: Count = 0
    invalid_target: Count = 0
    empty: Count = 0
    too_long: Count = 0
    contains_link: Count = 0
    contains_code: Count = 0
    contains_instruction: Count = 0
    contains_secret: Count = 0
    duplicate_fact: Count = 0
    cap_reached: Count = 0
    over_limit: Count = 0


class MemoryExtractionState(CosmosModel):
    """Whether and how memory was extracted from the turn (`13-conversations-and-memory.md`
    sections 7.11 and 7.12). At creation it is `{state: "not_applicable", reason: null,
    attempts: 0}`; every other field starts empty."""

    state: ExtractionState
    reason: Literal["suggested_question"] | None = None
    lease_expires_at: UtcDateTime | None = None
    attempts: Annotated[int, Field(ge=0, le=EXTRACTION_ATTEMPTS_MAX)] = 0
    facts_added: Annotated[int, Field(ge=0, le=EXTRACTION_FACTS_MAX)] = 0
    facts_replaced: Annotated[int, Field(ge=0, le=EXTRACTION_FACTS_MAX)] = 0
    rejected: RejectedCounts = Field(default_factory=RejectedCounts)
    shield_calls: Annotated[int, Field(ge=0, le=EXTRACTION_SHIELD_CALLS_MAX)] = 0
    error_code: InternalErrorCode | None = None
    usage: ModelCallUsage | None = None
    updated_at: UtcDateTime | None = None


class TurnError(CosmosModel):
    """The error of a `failed` turn: a stream error code of the chat pipeline, or
    `turn_abandoned`."""

    code: Code
    retryable: bool


def _not_applicable() -> MemoryExtractionState:
    return MemoryExtractionState(state=ExtractionState.NOT_APPLICABLE)


class TurnDocument(CosmosDocument):
    """One turn of a session; the defaults are the values a turn is created with."""

    max_bytes: ClassVar[int] = TURN_MAX_BYTES

    id: CanonicalUuid
    user_id: CanonicalUuid
    session_id: CanonicalUuid
    type: Literal["turn"] = "turn"
    schema_version: Literal[1] = SCHEMA_VERSION
    seq: Annotated[int, Field(ge=1, le=SEQ_MAX)]
    client_turn_id: CanonicalUuid
    project_id: CanonicalUuid
    status: TurnStatus
    question: Annotated[str, StringConstraints(min_length=1, max_length=QUESTION_MAX_CODE_POINTS)]
    answer: Annotated[str, StringConstraints(max_length=ANSWER_MAX_CODE_POINTS)] = ""
    answer_truncated: bool = False
    finish_reason: FinishReason | None = None
    error: TurnError | None = None
    citations: Annotated[list[StoredCitation], Field(max_length=CITATIONS_MAX)] = Field(
        default_factory=list
    )
    allowed_links: Annotated[list[HttpsUrl], Field(max_length=ALLOWED_LINKS_MAX)] = Field(
        default_factory=list
    )
    followups: Annotated[list[Followup], Field(max_length=FOLLOWUPS_MAX)] = Field(
        default_factory=list
    )
    followups_at: UtcDateTime | None = None
    feedback: Feedback | None = None
    history_tokens: Count | None = None
    usage: TurnUsage = Field(default_factory=TurnUsage.empty)
    memory_extraction: MemoryExtractionState = Field(default_factory=_not_applicable)
    trace_id: CorrelationId
    deadline_at: UtcDateTime
    created_at: UtcDateTime
    ended_at: UtcDateTime | None = None
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.id != turn_id(self.session_id, self.client_turn_id):
            raise ValueError("a turn's id derives from its sessionId and clientTurnId")
        numbers = [citation.number for citation in self.citations]
        if len(set(numbers)) != len(numbers):
            raise ValueError("each citation number appears once")
        return self
