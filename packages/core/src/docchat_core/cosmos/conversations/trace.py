"""The `turn_trace` document (`80-data-model.md` section 3.7; `13-conversations-and-memory.md`
section 1.8; `12-chat-pipeline.md` sections 5.4, 6.5, 7.3, 8.6, 9.10 and 12.3; ADR 0071).

One per finalised turn, created in the finalisation batch and never changed afterwards except by
the legal-removal redaction. No app operation returns a trace or any of its fields. The chat
pipeline produces every value as `TurnTraceData`; the document adds the trace's identity and its
two timestamps.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from docchat_core.cosmos.base import CosmosDocument, CosmosModel
from docchat_core.cosmos.conversations.ids import turn_trace_id
from docchat_core.cosmos.conversations.turn import (
    CITATIONS_MAX,
    SCHEMA_VERSION,
    ChunkKey,
    Name,
    NonNegativeInt,
    SourceId,
)
from docchat_core.models import CanonicalUuid, UtcDateTime

TRACE_MAX_BYTES: Final = 1_300_000
STANDALONE_QUERY_MAX_CODE_POINTS: Final = 1_200
SEARCH_QUERY_MAX_CODE_POINTS: Final = 4_000
SEARCH_PASSAGE_MAX_CODE_POINTS: Final = 1_200
SEARCH_PASS_MAX_RESULTS: Final = 50
PHYSICAL_INDEX_NAME_MAX_LENGTH: Final = 128
"""The AI Search limit on an index name."""
MARKER_PATTERN: Final = r"^[0-9a-f]{16}$"

NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class RewriteOutcome(StrEnum):
    SKIPPED = "skipped"
    REWRITTEN = "rewritten"
    FALLBACK = "fallback"


class IndexSource(StrEnum):
    PROJECT_DEFAULT = "project_default"
    USER_OVERRIDE = "user_override"


class BroadenOutcome(StrEnum):
    NOT_NEEDED = "not_needed"
    REPLACED = "replaced"
    FAILED = "failed"
    SKIPPED_PARTIAL = "skipped_partial"


class KeptOutcome(StrEnum):
    SENT = "sent"
    FLAGGED = "flagged"
    BUDGET_DROPPED = "budget_dropped"


class ScreeningOutcome(StrEnum):
    PASSED = "passed"
    FLAGGED = "flagged"
    UNAVAILABLE = "unavailable"


class ReasoningEffort(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SearchResultRecord(CosmosModel):
    """One result of a search pass, as the served-chunk-set filter judged it."""

    chunk_key: ChunkKey
    document_id: CanonicalUuid
    chunk_set_id: CanonicalUuid
    search_score: float
    reranker_score: float | None
    served: bool


class KeptSource(CosmosModel):
    """One kept result; a flagged source has no source ID."""

    rank: Annotated[int, Field(ge=1)]
    source_id: SourceId | None
    chunk_key: ChunkKey
    document_id: CanonicalUuid
    document_version_id: CanonicalUuid
    chunk_set_id: CanonicalUuid
    reranker_score: float | None
    outcome: KeptOutcome


class ScreeningRecord(CosmosModel):
    """Source screening: requests sent (retries included), segments, UTF-16 units and the
    number of flagged sources."""

    outcome: ScreeningOutcome
    calls: NonNegativeInt
    segments: NonNegativeInt
    characters: NonNegativeInt
    flagged: NonNegativeInt


SearchPass = Annotated[list[SearchResultRecord], Field(max_length=SEARCH_PASS_MAX_RESULTS)]


class RetrievalRecord(CosmosModel):
    """What retrieval searched, found and kept."""

    search_index_id: CanonicalUuid
    physical_index_id: CanonicalUuid
    physical_index_name: Annotated[
        str, StringConstraints(min_length=1, max_length=PHYSICAL_INDEX_NAME_MAX_LENGTH)
    ]
    index_source: IndexSource
    query_embedding_deployment: Name
    embedding_model: Name
    embedding_dimensions: Annotated[int, Field(ge=1)]
    candidate_count: NonNegativeInt
    keep_count: NonNegativeInt
    reranker_threshold: float
    min_confident_results: NonNegativeInt
    rerank_partial: bool
    broaden_attempted: bool
    broadened: bool
    broaden_outcome: BroadenOutcome
    search_passage: (
        Annotated[str, StringConstraints(max_length=SEARCH_PASSAGE_MAX_CODE_POINTS)] | None
    )
    first_pass: SearchPass
    broadened_pass: SearchPass | None
    kept: Annotated[list[KeptSource], Field(max_length=CITATIONS_MAX)]
    screening: ScreeningRecord | None


class PromptMessage(CosmosModel):
    """One message of the answer call, exactly as sent."""

    role: Literal["system", "user", "assistant"]
    content: str


class ModelInput(CosmosModel):
    """The answer call exactly as sent."""

    rules_version: NonEmpty
    marker: Annotated[str, StringConstraints(pattern=MARKER_PATTERN)]
    deployment: Name
    model: Name
    max_completion_tokens: Annotated[int, Field(ge=1)]
    reasoning_effort: ReasoningEffort
    messages: list[PromptMessage]
    prompt_tokens: NonNegativeInt
    dropped_turns: NonNegativeInt
    dropped_source_ids: list[SourceId]


class OmittedModelInput(CosmosModel):
    """What a trace above its size limit stores instead of the model input."""

    # No default: a full model input must never read as an omitted one.
    omitted: Literal["size_limit"]


class PromptBudget(CosmosModel):
    budget_tokens: NonNegativeInt
    completion_reserve_tokens: NonNegativeInt
    memory_facts_included: NonNegativeInt
    summary_included: bool


class ChatDeployment(CosmosModel):
    """A chat-completion deployment as the app settings name it."""

    deployment: Name
    model: Name
    model_version: NonEmpty
    reasoning_effort: ReasoningEffort


class EmbeddingDeployment(CosmosModel):
    """The query-embedding deployment as the physical index names it."""

    deployment: Name
    model: Name
    dimensions: Annotated[int, Field(ge=1)]


class TraceDeployments(CosmosModel):
    chat: ChatDeployment
    small: ChatDeployment
    query_embedding: EmbeddingDeployment


StepMs = NonNegativeInt | None


class TraceTimings(CosmosModel):
    """Step durations in ms; `null` for a step that didn't run."""

    rewrite_ms: StepMs = None
    query_embedding_ms: StepMs = None
    search_ms: StepMs = None
    served_filter_ms: StepMs = None
    search_passage_ms: StepMs = None
    broadened_search_ms: StepMs = None
    screening_ms: StepMs = None
    assembly_ms: StepMs = None
    time_to_first_token_ms: StepMs = None
    generation_ms: StepMs = None
    total_ms: StepMs = None


class TurnTraceData(CosmosModel):
    """Every trace field the chat pipeline supplies, from `rewriteOutcome` to
    `contentFilterDegraded`."""

    rewrite_outcome: RewriteOutcome
    standalone_query: (
        Annotated[str, StringConstraints(max_length=STANDALONE_QUERY_MAX_CODE_POINTS)] | None
    ) = None
    search_query: (
        Annotated[str, StringConstraints(max_length=SEARCH_QUERY_MAX_CODE_POINTS)] | None
    ) = None
    retrieval: RetrievalRecord | None = None
    model_input: ModelInput | OmittedModelInput | None = None
    prompt_budget: PromptBudget | None = None
    deployments: TraceDeployments | None = None
    timings: TraceTimings = Field(default_factory=TraceTimings)
    content_filter_degraded: bool = False


class TurnTraceDocument(TurnTraceData, CosmosDocument):
    """A finalised turn's write-once diagnostics; `redactedAt` is set only by the legal-removal
    command."""

    max_bytes: ClassVar[int] = TRACE_MAX_BYTES

    id: CanonicalUuid
    user_id: CanonicalUuid
    session_id: CanonicalUuid
    type: Literal["turn_trace"] = "turn_trace"
    schema_version: Literal[1] = SCHEMA_VERSION
    turn_id: CanonicalUuid
    created_at: UtcDateTime
    redacted_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.id != turn_trace_id(self.turn_id):
            raise ValueError("a trace's id derives from its turnId")
        return self

    def with_model_input_omitted(self) -> Self:
        """The trace as it is written when its JSON is above the size limit."""
        return self.model_copy(update={"model_input": OmittedModelInput(omitted="size_limit")})
