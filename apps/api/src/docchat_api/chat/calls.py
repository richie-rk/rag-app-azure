"""The model calls of a turn other than the answer: the three chat-small calls (question rewrite,
search passage, follow-ups) and the two embedding calls, with the usage ledger every call records
into (`12-chat-pipeline.md` sections 5, 6.2, 7.2, 10.5, 10.8, 13, 15 and 17; ADR 0059).

Every call goes through the one `AsyncOpenAI` client of `docchat_core.clients` (`max_retries=0`,
so only the rules here retry). Every chat completion sets `max_completion_tokens` and
`reasoning_effort`, sends `safety_identifier`, and sends `extra_body={"prompt_cache_options":
{"mode": "explicit"}}` when its deployment's prompt cache is `disabled`; none sends a sampling
parameter, tools or a stop sequence. A chat-small call runs inside `asyncio.timeout` of its
whole-call limit with no retry, sends strict structured output, and takes its fallback on any
ending other than content its Pydantic model accepts. An embedding call has 2 attempts of 10 s.

Each call produces one `ModelCallUsage` as soon as it ends, a cancelled call included: usage the
response reported, or an estimate counted in a worker thread (the prompt as sent with the
deployment's encoding and the counting rule, the visible text received). The ledger builds it in a
task of its own, which cancellation can't reach, and starts its counter writes at once; the turn's
finaliser awaits them.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final, Literal, Protocol
from uuid import UUID

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, ValidationError

from docchat_api.chat import telemetry
from docchat_api.chat.prompts import Message, TextTokenizer
from docchat_api.tokens import count_sync
from docchat_api.usage import model_call_usage
from docchat_core.cosmos.conversations import (
    CallOutcome,
    ChatDeployment,
    ModelCall,
    ModelCallUsage,
    ReasoningEffort,
)
from docchat_core.models.clock import Clock
from docchat_core.opsconfig import PricingOps
from docchat_core.telemetry import set_attributes, start_span

type Sleep = Callable[[float], Awaitable[None]]
type PromptCache = Literal["disabled", "automatic"]
type FilterWhere = Literal["prompt", "completion"]
type Recorder = Callable[..., Awaitable[None]]

EXPLICIT_PROMPT_CACHE: Final[dict[str, Any]] = {"prompt_cache_options": {"mode": "explicit"}}
CONTENT_FILTER: Final = "content_filter"
EMBEDDING_ATTEMPTS: Final = 2
EMBEDDING_ATTEMPT_SECONDS: Final = 10.0
EMBEDDING_RETRY_AFTER_MAX_SECONDS: Final = 5.0
DEFAULT_RETRY_WAIT_SECONDS: Final = 1.0
MILLISECONDS: Final = 1000
_SUCCESS: Final = frozenset({"stop"})


def safety_identifier(tid: UUID, oid: UUID) -> str:
    """The first 32 hexadecimal characters of SHA-256 over `docchat:{tid}:{oid}`."""
    return hashlib.sha256(f"docchat:{tid}:{oid}".encode()).hexdigest()[:32]


def elapsed_ms(clock: Clock, started: float) -> int:
    return max(0, round((clock.monotonic() - started) * MILLISECONDS))


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatDeploymentSettings:
    """A chat-completion deployment as the app settings name it, with its encoding."""

    deployment: str
    model: str
    model_version: str
    reasoning_effort: ReasoningEffort
    prompt_cache: PromptCache
    tokenizer: TextTokenizer

    def trace(self) -> ChatDeployment:
        return ChatDeployment(
            deployment=self.deployment,
            model=self.model,
            model_version=self.model_version,
            reasoning_effort=self.reasoning_effort,
        )

    def parameters(self, max_completion_tokens: int, safety: str) -> dict[str, Any]:
        """The parameters every chat completion on this deployment sends."""
        parameters: dict[str, Any] = {
            "model": self.deployment,
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": self.reasoning_effort.value,
            "safety_identifier": safety,
        }
        if self.prompt_cache == "disabled":
            parameters["extra_body"] = EXPLICIT_PROMPT_CACHE
        return parameters


@dataclass(frozen=True, slots=True, kw_only=True)
class CallContext:
    """Who and what a turn's calls are made for."""

    oid: UUID
    project_id: UUID
    session_id: UUID
    turn_id: UUID
    safety_identifier: str


# --- Usage --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportedUsage:
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _member(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def reported_usage(usage: object) -> ReportedUsage | None:
    """The usage a response reported; a field it doesn't report counts 0."""
    if usage is None:
        return None
    prompt_details = _member(usage, "prompt_tokens_details")
    completion_details = _member(usage, "completion_tokens_details")
    return ReportedUsage(
        prompt_tokens=_count(_member(usage, "prompt_tokens")),
        completion_tokens=_count(_member(usage, "completion_tokens")),
        reasoning_tokens=_count(_member(completion_details, "reasoning_tokens")),
        cached_tokens=_count(_member(prompt_details, "cached_tokens")),
        cache_write_tokens=_count(_member(prompt_details, "cache_write_tokens")),
    )


def message_tokens(tokenizer: TextTokenizer, messages: Sequence[Message]) -> int:
    """The counting rule, synchronously: 3 per message plus its content, plus 3."""
    return count_sync(tokenizer, [message.content for message in messages]) + 3 * len(messages) + 3


class UsageRecorder(Protocol):
    def __call__(
        self,
        oid: UUID,
        record: ModelCallUsage,
        *,
        session_id: UUID | None = ...,
        turn_id: UUID | None = ...,
    ) -> Awaitable[None]: ...


@dataclass(slots=True)
class UsageLedger:
    """A turn's usage records in call order, and their counter writes."""

    context: CallContext
    pricing: PricingOps
    recorder: UsageRecorder
    cancel_outcome: CallOutcome = CallOutcome.CANCELLED
    """The outcome of a call the turn cancels: `cancelled` for a disconnect or the drain,
    `timed_out` for the turn deadline."""
    _builds: list[asyncio.Task[ModelCallUsage]] = field(default_factory=list)
    _writes: list[Awaitable[None]] = field(default_factory=list)

    def submit(
        self,
        *,
        call: ModelCall,
        deployment: str,
        model: str,
        duration_ms: int,
        outcome: CallOutcome,
        usage: ReportedUsage | None,
        estimate: Callable[[], ReportedUsage],
    ) -> asyncio.Task[ModelCallUsage]:
        """Build the record in a task of its own (the estimate counted in one worker-thread
        call when the response reported no usage) and start its two counter writes."""
        task = asyncio.get_running_loop().create_task(
            self._build(
                call=call,
                deployment=deployment,
                model=model,
                duration_ms=duration_ms,
                outcome=outcome,
                usage=usage,
                estimate=estimate,
            )
        )
        self._builds.append(task)
        return task

    async def _build(
        self,
        *,
        call: ModelCall,
        deployment: str,
        model: str,
        duration_ms: int,
        outcome: CallOutcome,
        usage: ReportedUsage | None,
        estimate: Callable[[], ReportedUsage],
    ) -> ModelCallUsage:
        counted = usage if usage is not None else await asyncio.to_thread(estimate)
        record = model_call_usage(
            self.pricing,
            call=call,
            deployment=deployment,
            model=model,
            prompt_tokens=counted.prompt_tokens,
            completion_tokens=counted.completion_tokens,
            reasoning_tokens=counted.reasoning_tokens,
            cached_tokens=counted.cached_tokens,
            cache_write_tokens=counted.cache_write_tokens,
            estimated=usage is None,
            duration_ms=duration_ms,
            outcome=outcome,
        )
        self._writes.append(
            self.recorder(
                self.context.oid,
                record,
                session_id=self.context.session_id,
                turn_id=self.context.turn_id,
            )
        )
        return record

    async def records(self) -> list[ModelCallUsage]:
        """Every record submitted so far, in order, once built; a build that failed is left
        out."""
        builds = list(self._builds)
        results = await asyncio.shield(asyncio.gather(*builds, return_exceptions=True))
        return [result for result in results if isinstance(result, ModelCallUsage)]

    async def settle(self) -> None:
        """Wait for every record's counter writes."""
        await self.records()
        await asyncio.gather(*self._writes, return_exceptions=True)


# --- Errors -------------------------------------------------------------------------------------


def stated_delay(exc: BaseException) -> float | None:
    """The `retry-after-ms` or `retry-after` delay of a status error, in seconds."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            return seconds
    return None


def status_of(exc: BaseException) -> int | None:
    return exc.status_code if isinstance(exc, openai.APIStatusError) else None


def is_prompt_filtered(exc: BaseException) -> bool:
    return isinstance(exc, openai.BadRequestError) and exc.code == CONTENT_FILTER


# --- Chat-small calls ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StandaloneQuery(StrictModel):
    query: str


class SearchPassage(StrictModel):
    passage: str


class FollowupQuestions(StrictModel):
    questions: list[str]


def _object_schema(name: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = dict(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


STANDALONE_QUERY_FORMAT: Final = _object_schema("standalone_query", {"query": {"type": "string"}})
SEARCH_PASSAGE_FORMAT: Final = _object_schema("search_passage", {"passage": {"type": "string"}})
FOLLOWUP_QUESTIONS_FORMAT: Final = _object_schema(
    "followup_questions", {"questions": {"type": "array", "items": {"type": "string"}}}
)

_SMALL_SPANS: Final[Mapping[ModelCall, str]] = {
    ModelCall.REWRITE: "docchat.chat.rewrite",
    ModelCall.SEARCH_PASSAGE: "docchat.chat.search_passage",
    ModelCall.FOLLOWUPS: "docchat.chat.followups",
}


@dataclass(frozen=True, slots=True)
class SmallReply[M: StrictModel]:
    """A chat-small call's accepted output, or `None` for its fallback."""

    value: M | None
    outcome: CallOutcome
    duration_ms: int
    record: asyncio.Task[ModelCallUsage]


def _content(response: object) -> tuple[str | None, str | None, object]:
    """The first choice's content and finish reason, and the refusal."""
    choices = _member(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return None, None, None
    choice = choices[0]
    message = _member(choice, "message")
    content = _member(message, "content")
    finish = _member(choice, "finish_reason")
    return (
        content if isinstance(content, str) else None,
        finish if isinstance(finish, str) else None,
        _member(message, "refusal"),
    )


class ModelCalls:
    """The chat-small and embedding calls of one process."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        small: ChatDeploymentSettings,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.client = client
        self.small = small
        self._clock = clock
        self._sleep = sleep

    def _report_filter(self, where: FilterWhere, call: ModelCall, context: CallContext) -> None:
        telemetry.report_content_filter(
            where=where,
            call=call.value,
            deployment=self.small.deployment,
            project_id=context.project_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
        )

    async def complete_json[M: StrictModel](
        self,
        *,
        call: ModelCall,
        messages: Sequence[Message],
        response_format: Mapping[str, Any],
        model_type: type[M],
        max_completion_tokens: int,
        timeout_seconds: float,
        ledger: UsageLedger,
    ) -> SmallReply[M]:
        """One chat-small call with strict structured output, inside its whole-call timeout and
        with no retry."""
        context = ledger.context
        parameters = self.small.parameters(max_completion_tokens, context.safety_identifier)
        parameters["messages"] = [message.payload() for message in messages]
        parameters["response_format"] = dict(response_format)
        parameters["stream"] = False
        started = self._clock.monotonic()
        response: Any = None
        outcome = CallOutcome.OK
        content: str | None = None
        value: M | None = None
        tokenizer = self.small.tokenizer
        with start_span(
            _SMALL_SPANS[call], **{"docchat.deployment": self.small.deployment}
        ) as span:
            try:
                try:
                    async with asyncio.timeout(timeout_seconds):
                        response = await self.client.chat.completions.create(**parameters)
                except TimeoutError:
                    outcome = CallOutcome.TIMED_OUT
                except openai.APIError as failure:
                    if is_prompt_filtered(failure):
                        outcome = CallOutcome.FILTERED
                        self._report_filter("prompt", call, context)
                    else:
                        outcome = CallOutcome.FAILED
                except asyncio.CancelledError:
                    outcome = ledger.cancel_outcome
                    raise
                if response is not None:
                    content, finish, refusal = _content(response)
                    if finish == CONTENT_FILTER:
                        outcome = CallOutcome.FILTERED
                        self._report_filter("completion", call, context)
                    elif finish in _SUCCESS and refusal is None and content is not None:
                        try:
                            value = model_type.model_validate_json(content)
                        except ValidationError:
                            value = None
            finally:
                duration = elapsed_ms(self._clock, started)
                usage = reported_usage(_member(response, "usage")) if response is not None else None
                received = content or ""

                def estimate() -> ReportedUsage:
                    return ReportedUsage(
                        prompt_tokens=message_tokens(tokenizer, messages),
                        completion_tokens=count_sync(tokenizer, [received]),
                    )

                record = ledger.submit(
                    call=call,
                    deployment=self.small.deployment,
                    model=self.small.model,
                    duration_ms=duration,
                    outcome=outcome,
                    usage=usage,
                    estimate=estimate,
                )
                set_attributes(span, **{"docchat.outcome": outcome.value})
                if usage is not None:
                    set_attributes(
                        span,
                        **{
                            "docchat.prompt_tokens": usage.prompt_tokens,
                            "docchat.completion_tokens": usage.completion_tokens,
                        },
                    )
        return SmallReply(value, outcome, duration, record)

    # --- Embeddings -----------------------------------------------------------------------------

    async def embed(
        self,
        *,
        call: ModelCall,
        deployment: str,
        model: str,
        dimensions: int,
        text: str,
        tokenizer: TextTokenizer,
        ledger: UsageLedger,
        search_pass: Literal["first", "broadened"],
    ) -> list[float] | None:
        """The vector of `text`, or `None` when the call failed or returned a vector of another
        length. 2 attempts of 10 s, retried on a connection error, `408`, a `429` stating at most
        5 s and any `5xx`, after the stated delay or 1 s."""
        started = self._clock.monotonic()
        response: Any = None
        outcome = CallOutcome.FAILED
        with start_span(
            "docchat.chat.embed",
            **{
                "docchat.deployment": deployment,
                "docchat.dimensions": dimensions,
                "docchat.pass": search_pass,
            },
        ):
            try:
                for attempt in range(EMBEDDING_ATTEMPTS):
                    wait: float | None = None
                    try:
                        async with asyncio.timeout(EMBEDDING_ATTEMPT_SECONDS):
                            response = await self.client.embeddings.create(
                                model=deployment, input=text, dimensions=dimensions
                            )
                        outcome = CallOutcome.OK
                        break
                    except TimeoutError:
                        outcome = CallOutcome.TIMED_OUT
                        wait = DEFAULT_RETRY_WAIT_SECONDS
                    except openai.APIConnectionError:
                        outcome = CallOutcome.FAILED
                        wait = DEFAULT_RETRY_WAIT_SECONDS
                    except openai.APIStatusError as failure:
                        outcome = CallOutcome.FAILED
                        wait = _embedding_wait(failure)
                    except asyncio.CancelledError:
                        outcome = ledger.cancel_outcome
                        raise
                    if wait is None or attempt + 1 == EMBEDDING_ATTEMPTS:
                        break
                    await self._sleep(wait)
            finally:
                usage = reported_usage(_member(response, "usage")) if response is not None else None

                def estimate() -> ReportedUsage:
                    return ReportedUsage(
                        prompt_tokens=count_sync(tokenizer, [text]), completion_tokens=0
                    )

                _ = ledger.submit(
                    call=call,
                    deployment=deployment,
                    model=model,
                    duration_ms=elapsed_ms(self._clock, started),
                    outcome=outcome,
                    usage=usage,
                    estimate=estimate,
                )
        return _vector(response, dimensions)


def _embedding_wait(failure: openai.APIStatusError) -> float | None:
    status = failure.status_code
    delay = stated_delay(failure)
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        if delay is not None and delay > EMBEDDING_RETRY_AFTER_MAX_SECONDS:
            return None
        return delay if delay is not None else DEFAULT_RETRY_WAIT_SECONDS
    if status == HTTPStatus.REQUEST_TIMEOUT or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return delay if delay is not None else DEFAULT_RETRY_WAIT_SECONDS
    return None


def _vector(response: object, dimensions: int) -> list[float] | None:
    data = _member(response, "data")
    if not isinstance(data, Sequence) or len(data) != 1:
        return None
    embedding = _member(data[0], "embedding")
    if not isinstance(embedding, Sequence) or len(embedding) != dimensions:
        return None
    try:
        return [float(value) for value in embedding]
    except (TypeError, ValueError):
        return None
