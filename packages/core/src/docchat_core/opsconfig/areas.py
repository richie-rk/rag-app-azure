"""The eleven area models the API reads (`81-config.md` sections 3.4 to 3.7; ADR 0028 item 4).

One Pydantic model per area: `ApiOps` (`11-api.md` section 1.11); `ChatOps`, `RetrievalOps`,
`FollowupsOps`, `RateLimitOps`, `QuotaOps` and `PricingOps` (`12-chat-pipeline.md` section 18);
`ConversationsOps`, `SummaryOps`, `MemoryOps` and `FeedbackOps` (`13-conversations-and-memory.md`
section 11). The twelfth area, `ingestion`, is `docchat_core.ingestion.settings.IngestionOpsConfig`.

Every field is aliased to its full dotted key, carries its range and has no code default, because
the database is the source. Every model forbids unknown fields, is frozen, and parses its values
strictly as typed JSON data: an integer key takes a JSON integer only, a boolean key `true` or
`false` only, so the string `"false"` never reads as a switch. A broken cross-field rule makes
the whole area invalid and raises `OpsRuleError`, which names the keys of the rule and never a
value.

`summary.reasoning_effort` and `memory.reasoning_effort` must also be in the chat-small model's row
of the capability table (`12-chat-pipeline.md` section 10.7). That row is the process's
configuration, not a stored value, so validation takes it from the Pydantic context
(`efforts_context`); a validation without it refuses both keys rather than accept an effort the
deployment may refuse.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    ValidationInfo,
    WrapSerializer,
    model_validator,
)

EFFORTS_CONTEXT_KEY: Final = "chat_small_reasoning_efforts"
"""The validation-context member holding the chat-small model's accepted reasoning efforts."""

ANSWER_PROMPT_MINIMUM_TOKENS: Final = 8000
"""The prompt the answer budget always keeps beside the completion reserve."""

SUMMARY_COMPLETION_MARGIN_TOKENS: Final = 200
LEASE_TIMEOUT_FACTOR: Final = 3

MODEL_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9.\-]{0,63}$"
CURRENCY_PATTERN: Final = r"^[A-Z]{3}$"
MAX_PRICED_MODELS: Final = 20

RATE_MAX: Final = Decimal(1000)
_RATE_RE: Final = re.compile(r"^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,6})?$")

type OpsReasoningEffort = Literal["none", "low", "medium"]
"""The efforts the conversation store's calls may use, before the capability row narrows them."""


class OpsRuleError(ValueError):
    """A broken cross-field rule of an area, naming the keys of the rule and never a value."""

    def __init__(self, keys: Collection[str]) -> None:
        self.keys = tuple(keys)
        super().__init__("a cross-field rule is broken: " + ", ".join(self.keys))


def efforts_context(chat_small_efforts: Collection[str]) -> dict[str, frozenset[str]]:
    """The validation context that gives the summary and memory areas the chat-small model's
    accepted reasoning efforts (its row of `MODEL_CAPABILITIES`)."""
    return {EFFORTS_CONTEXT_KEY: frozenset(chat_small_efforts)}


def _context_efforts(info: ValidationInfo) -> frozenset[str] | None:
    context: Any = info.context
    if not isinstance(context, Mapping):
        return None
    efforts = context.get(EFFORTS_CONTEXT_KEY)
    return efforts if isinstance(efforts, frozenset) else None


def _broken(*rules: tuple[bool, tuple[str, ...]]) -> None:
    keys = dict.fromkeys(key for broken, rule_keys in rules if broken for key in rule_keys)
    if keys:
        raise OpsRuleError(tuple(keys))


def _rate(value: object) -> Decimal:
    # Rates are money: read from decimal strings, never through binary floating point.
    text = str(value) if isinstance(value, Decimal) else value
    if not isinstance(text, str) or not _RATE_RE.fullmatch(text):
        raise ValueError("a rate is a decimal string with at most 6 decimal places")
    rate = Decimal(text)
    if rate > RATE_MAX:
        raise ValueError("a rate is at most 1000")
    return rate


def _frozen_rates(rates: Mapping[str, ModelRate]) -> Mapping[str, ModelRate]:
    # A snapshot is shared by every request of a worker, so its price table can't be changed.
    return MappingProxyType(dict(rates))


def _rates_as_dict(rates: Mapping[str, ModelRate], handler: SerializerFunctionWrapHandler) -> Any:
    return handler(dict(rates))


Rate = Annotated[Decimal, BeforeValidator(_rate)]
"""A price in currency units per 1,000,000 tokens: a decimal string from 0 to 1000 with at most
6 decimal places, read as `decimal.Decimal`."""

ModelName = Annotated[str, StringConstraints(pattern=MODEL_NAME_PATTERN)]


class OpsArea(BaseModel):
    """The base of the eleven area models: dotted keys only, unknown keys refused, frozen, typed
    data."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )


class ApiOps(OpsArea):
    """Area `api` (`81-config.md` section 3.4)."""

    write_requests_per_minute: int = Field(alias="api.write_requests_per_minute", ge=10, le=1200)


class ChatOps(OpsArea):
    """Area `chat`. No field bounds the history by turn count: the prompt budget does."""

    context_token_budget: int = Field(alias="chat.context_token_budget", ge=20_000, le=200_000)
    answer_max_completion_tokens: int = Field(
        alias="chat.answer_max_completion_tokens", ge=2000, le=40_000
    )
    small_call_max_completion_tokens: int = Field(
        alias="chat.small_call_max_completion_tokens", ge=200, le=8000
    )
    rewrite_history_turns: int = Field(alias="chat.rewrite_history_turns", ge=1, le=8)
    rewrite_timeout_seconds: int = Field(alias="chat.rewrite_timeout_seconds", ge=2, le=20)
    max_turn_seconds: int = Field(alias="chat.max_turn_seconds", ge=60, le=220)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        _broken(
            (
                self.answer_max_completion_tokens
                > self.context_token_budget - ANSWER_PROMPT_MINIMUM_TOKENS,
                ("chat.answer_max_completion_tokens", "chat.context_token_budget"),
            )
        )
        return self


class RetrievalOps(OpsArea):
    """Area `retrieval`."""

    candidate_count: int = Field(alias="retrieval.candidate_count", ge=10, le=50)
    keep_count: int = Field(alias="retrieval.keep_count", ge=1, le=16)
    reranker_threshold: float = Field(alias="retrieval.reranker_threshold", ge=0.0, le=4.0)
    # Its upper bound is `retrieval.keep_count`, a cross-field rule.
    min_confident_results: int = Field(alias="retrieval.min_confident_results", ge=1)
    semantic_max_wait_ms: int = Field(alias="retrieval.semantic_max_wait_ms", ge=700, le=10_000)
    broaden_timeout_seconds: int = Field(alias="retrieval.broaden_timeout_seconds", ge=2, le=20)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        _broken(
            (
                self.keep_count > self.candidate_count,
                ("retrieval.keep_count", "retrieval.candidate_count"),
            ),
            (
                self.min_confident_results > self.keep_count,
                ("retrieval.min_confident_results", "retrieval.keep_count"),
            ),
        )
        return self


class FollowupsOps(OpsArea):
    """Area `followups`."""

    enabled: bool = Field(alias="followups.enabled")
    timeout_seconds: int = Field(alias="followups.timeout_seconds", ge=2, le=10)
    max_count: int = Field(alias="followups.max_count", ge=1, le=3)
    source_count: int = Field(alias="followups.source_count", ge=1, le=8)
    source_excerpt_tokens: int = Field(alias="followups.source_excerpt_tokens", ge=100, le=800)


class RateLimitOps(OpsArea):
    """Area `ratelimit`. In `prod` the capacity invariant also bounds `chat_requests_per_minute`;
    `docchat-ops chat capacity-check` checks that, not this model (`81-config.md` section 3.6)."""

    chat_requests_per_minute: int = Field(alias="ratelimit.chat_requests_per_minute", ge=1, le=1200)
    max_concurrent_streams: int = Field(alias="ratelimit.max_concurrent_streams", ge=1, le=200)
    citation_downloads_per_minute: int = Field(
        alias="ratelimit.citation_downloads_per_minute", ge=1, le=500
    )
    max_concurrent_downloads: int = Field(alias="ratelimit.max_concurrent_downloads", ge=1, le=10)


class QuotaOps(OpsArea):
    """Area `quota`."""

    tokens_per_day: int = Field(alias="quota.tokens_per_day", ge=10_000, le=1_000_000_000)
    # Its lower bound is `quota.tokens_per_day`, a cross-field rule; the static bound is the
    # lowest daily quota.
    tokens_per_month: int = Field(alias="quota.tokens_per_month", ge=10_000, le=100_000_000_000)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        _broken(
            (
                self.tokens_per_month < self.tokens_per_day,
                ("quota.tokens_per_month", "quota.tokens_per_day"),
            )
        )
        return self


class ModelRate(BaseModel):
    """One model's token prices (`12-chat-pipeline.md` section 15.4)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    input_per_million: Rate = Field(alias="inputPerMillion")
    cached_input_per_million: Rate = Field(alias="cachedInputPerMillion")
    cache_write_per_million: Rate = Field(alias="cacheWritePerMillion")
    output_per_million: Rate = Field(alias="outputPerMillion")


class PricingOps(OpsArea):
    """Area `pricing`: the currency and the per-model price table of the cost estimates."""

    currency: Annotated[str, StringConstraints(pattern=CURRENCY_PATTERN)] = Field(
        alias="pricing.currency"
    )
    model_rates: Annotated[
        Mapping[ModelName, ModelRate],
        Field(alias="pricing.model_rates", max_length=MAX_PRICED_MODELS),
        AfterValidator(_frozen_rates),
        WrapSerializer(_rates_as_dict),
    ]


class ConversationsOps(OpsArea):
    """Area `conversations`."""

    max_turns_per_session: int = Field(alias="conversations.max_turns_per_session", ge=50, le=5000)


class SummaryOps(OpsArea):
    """Area `summary`: the rolling summary's trigger, sizes, call and lease."""

    trigger_tokens: int = Field(alias="summary.trigger_tokens", ge=1000, le=20_000)
    keep_recent_tokens: int = Field(alias="summary.keep_recent_tokens", ge=0, le=19_000)
    max_input_tokens: int = Field(alias="summary.max_input_tokens", ge=2000, le=60_000)
    max_summary_tokens: int = Field(alias="summary.max_summary_tokens", ge=200, le=2000)
    max_completion_tokens: int = Field(alias="summary.max_completion_tokens", ge=500, le=16_000)
    reasoning_effort: OpsReasoningEffort = Field(alias="summary.reasoning_effort")
    timeout_seconds: int = Field(alias="summary.timeout_seconds", ge=5, le=120)
    lease_seconds: int = Field(alias="summary.lease_seconds", ge=30, le=900)
    max_backoff_minutes: int = Field(alias="summary.max_backoff_minutes", ge=1, le=1440)

    @model_validator(mode="after")
    def _rules(self, info: ValidationInfo) -> Self:
        efforts = _context_efforts(info)
        _broken(
            (
                self.keep_recent_tokens >= self.trigger_tokens,
                ("summary.keep_recent_tokens", "summary.trigger_tokens"),
            ),
            (
                self.trigger_tokens > self.max_input_tokens,
                ("summary.trigger_tokens", "summary.max_input_tokens"),
            ),
            (
                self.max_completion_tokens
                < self.max_summary_tokens + SUMMARY_COMPLETION_MARGIN_TOKENS,
                ("summary.max_completion_tokens", "summary.max_summary_tokens"),
            ),
            (
                self.lease_seconds < LEASE_TIMEOUT_FACTOR * self.timeout_seconds,
                ("summary.lease_seconds", "summary.timeout_seconds"),
            ),
            (
                efforts is None or self.reasoning_effort not in efforts,
                ("summary.reasoning_effort",),
            ),
        )
        return self


class MemoryOps(OpsArea):
    """Area `memory`: the platform switch, the fact limits, the extraction call and its lease."""

    enabled: bool = Field(alias="memory.enabled")
    max_facts: int = Field(alias="memory.max_facts", ge=1, le=200)
    max_tokens: int = Field(alias="memory.max_tokens", ge=100, le=4000)
    prompt_max_tokens: int = Field(alias="memory.prompt_max_tokens", ge=200, le=5000)
    max_operations_per_turn: int = Field(alias="memory.max_operations_per_turn", ge=1, le=10)
    max_completion_tokens: int = Field(alias="memory.max_completion_tokens", ge=300, le=8000)
    reasoning_effort: OpsReasoningEffort = Field(alias="memory.reasoning_effort")
    timeout_seconds: int = Field(alias="memory.timeout_seconds", ge=5, le=60)
    lease_seconds: int = Field(alias="memory.lease_seconds", ge=30, le=900)
    max_attempts: int = Field(alias="memory.max_attempts", ge=1, le=5)
    retry_window_hours: int = Field(alias="memory.retry_window_hours", ge=1, le=168)

    @model_validator(mode="after")
    def _rules(self, info: ValidationInfo) -> Self:
        efforts = _context_efforts(info)
        _broken(
            (
                self.lease_seconds < LEASE_TIMEOUT_FACTOR * self.timeout_seconds,
                ("memory.lease_seconds", "memory.timeout_seconds"),
            ),
            (
                efforts is None or self.reasoning_effort not in efforts,
                ("memory.reasoning_effort",),
            ),
        )
        return self


class FeedbackOps(OpsArea):
    """Area `feedback`."""

    comments_enabled: bool = Field(alias="feedback.comments_enabled")
