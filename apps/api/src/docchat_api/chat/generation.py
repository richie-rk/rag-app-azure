"""The answer call: one streamed chat completion on the chat deployment, its retries, the stream
processing and the mapping of every ending to the turn's outcome (`12-chat-pipeline.md` sections
10.1 to 10.5, 11.2, 15.2, 15.3 and 17.4).

The call sends the assembled messages with `stream=True`, `stream_options.include_usage`,
`max_completion_tokens` (covering reasoning and visible tokens), `reasoning_effort`,
`safety_identifier` and, when the deployment's prompt cache is `disabled`, the explicit
`prompt_cache_options`; nothing else. It has 2 attempts, used only before the first streamed chunk,
on a connection error, `408`, a `429` stating at most 10 s and `500`, `502`, `503` or `504`, after
the stated delay or 1 s.

Each chunk's content goes through the citation minter, which releases text as `delta` events; a
chunk whose `content_filter_results` carries an `error` object marks the turn degraded once and
doesn't stop it (the platform filter fails open, source screening fails closed). When the released
answer reaches 100,000 code points the stream is closed and the ending counts as `length` with
visible text. 60 s without a chunk, or a stream that ends without a finish reason, is
`model_unavailable`. Cancelling the caller closes the model's HTTP stream at once and records the
call's usage, estimated when the usage chunk hadn't arrived.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from typing import Any, Final

import openai
from openai import AsyncOpenAI

from docchat_api.chat import telemetry
from docchat_api.chat.calls import (
    CONTENT_FILTER,
    DEFAULT_RETRY_WAIT_SECONDS,
    CallContext,
    ChatDeploymentSettings,
    ReportedUsage,
    Sleep,
    UsageLedger,
    elapsed_ms,
    is_prompt_filtered,
    reported_usage,
    stated_delay,
)
from docchat_api.chat.markers import CitationMinter, has_visible_text
from docchat_api.chat.prompts import Message
from docchat_api.tokens import count_sync
from docchat_core.cosmos.conversations import CallOutcome, FinishReason, ModelCall
from docchat_core.models.clock import Clock
from docchat_core.telemetry import set_attributes, start_span

ANSWER_ATTEMPTS: Final = 2
ANSWER_RETRY_AFTER_MAX_SECONDS: Final = 10.0
MODEL_BUSY_RETRY_MIN: Final = 1
MODEL_BUSY_RETRY_MAX: Final = 60
RETRIED_STATUSES: Final = frozenset(
    {
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)


class Ending(Enum):
    """How the answer call ended (section 10.4)."""

    STOP = "stop"
    LENGTH = "length"
    LENGTH_EMPTY = "length_empty"
    COMPLETION_FILTERED = "completion_filtered"
    PROMPT_FILTERED = "prompt_filtered"
    REJECTED = "rejected"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EndingOutcome:
    """The turn's side of one ending: completed or not, the stored finish reason, the error code."""

    completed: bool
    finish_reason: FinishReason
    error_code: str | None


ENDINGS: Final[dict[Ending, EndingOutcome]] = {
    Ending.STOP: EndingOutcome(True, FinishReason.STOP, None),
    Ending.LENGTH: EndingOutcome(True, FinishReason.LENGTH, None),
    Ending.LENGTH_EMPTY: EndingOutcome(False, FinishReason.LENGTH, "answer_incomplete"),
    Ending.COMPLETION_FILTERED: EndingOutcome(
        False, FinishReason.CONTENT_FILTER, "answer_filtered"
    ),
    Ending.PROMPT_FILTERED: EndingOutcome(False, FinishReason.CONTENT_FILTER, "question_filtered"),
    Ending.REJECTED: EndingOutcome(False, FinishReason.ERROR, "model_request_rejected"),
    Ending.BUSY: EndingOutcome(False, FinishReason.ERROR, "model_busy"),
    Ending.UNAVAILABLE: EndingOutcome(False, FinishReason.ERROR, "model_unavailable"),
}


_FINISHES: Final[dict[str | None, Ending]] = {
    "stop": Ending.STOP,
    CONTENT_FILTER: Ending.COMPLETION_FILTERED,
}
_FAILURES: Final[dict[int | None, Ending]] = {
    HTTPStatus.BAD_REQUEST: Ending.REJECTED,
    HTTPStatus.TOO_MANY_REQUESTS: Ending.BUSY,
}


def classify(
    *, finish_reason: str | None, capped: bool, answer: str, failure: BaseException | None
) -> Ending:
    """The ending of an answer call: a failure before any usable stream, else the finish reason
    (the answer cap counts as `length` with visible text); a stream without a finish reason is
    `model_unavailable`."""
    if failure is not None:
        if is_prompt_filtered(failure):
            return Ending.PROMPT_FILTERED
        status = failure.status_code if isinstance(failure, openai.APIStatusError) else None
        return _FAILURES.get(status, Ending.UNAVAILABLE)
    if capped:
        return Ending.LENGTH
    if finish_reason == "length":
        return Ending.LENGTH if has_visible_text(answer) else Ending.LENGTH_EMPTY
    return _FINISHES.get(finish_reason, Ending.UNAVAILABLE)


def busy_retry_after(failure: BaseException | None) -> int:
    """`model_busy`'s wait: the stated delay rounded up, kept from 1 to 60; 1 when none."""
    delay = stated_delay(failure) if failure is not None else None
    if delay is None:
        return MODEL_BUSY_RETRY_MIN
    return min(MODEL_BUSY_RETRY_MAX, max(MODEL_BUSY_RETRY_MIN, math.ceil(delay)))


def _retry_wait(failure: BaseException) -> float | None:
    """The wait before the second attempt, or `None` when this failure isn't retried."""
    if isinstance(failure, openai.APIConnectionError):
        return DEFAULT_RETRY_WAIT_SECONDS
    if not isinstance(failure, openai.APIStatusError):
        return None
    delay = stated_delay(failure)
    status = failure.status_code
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        if delay is not None and delay > ANSWER_RETRY_AFTER_MAX_SECONDS:
            return None
        return delay if delay is not None else DEFAULT_RETRY_WAIT_SECONDS
    if status in RETRIED_STATUSES:
        return delay if delay is not None else DEFAULT_RETRY_WAIT_SECONDS
    return None


def _member(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _filter_degraded(choice: object) -> bool:
    results = _member(choice, "content_filter_results")
    return results is not None and _member(results, "error") is not None


@dataclass(slots=True)
class Generation:
    """The answer call's result."""

    ending: Ending
    minter: CitationMinter
    retry_after_seconds: int | None
    usage: ReportedUsage | None
    first_chunk_ms: int | None
    duration_ms: int

    @property
    def outcome(self) -> EndingOutcome:
        return ENDINGS[self.ending]


class AnswerCall:
    """The streamed answer call on the chat deployment."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        chat: ChatDeploymentSettings,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self.chat = chat
        self._clock = clock
        self._sleep = sleep

    def parameters(
        self, messages: Sequence[Message], max_completion_tokens: int, context: CallContext
    ) -> dict[str, Any]:
        parameters = self.chat.parameters(max_completion_tokens, context.safety_identifier)
        parameters["messages"] = [message.payload() for message in messages]
        parameters["stream"] = True
        parameters["stream_options"] = {"include_usage": True}
        return parameters

    async def _open(self, parameters: dict[str, Any]) -> tuple[Any, AsyncIterator[Any], Any | None]:
        """The stream, its iterator and its first chunk (`None` for a stream that ended before
        one), within the 2 attempts; the last failure is raised."""
        for attempt in range(ANSWER_ATTEMPTS):
            stream: Any = None
            try:
                stream = await self._client.chat.completions.create(**parameters)
                iterator = stream.__aiter__()
                first = await anext(iterator, None)
            except openai.APIError as failure:
                if stream is not None:
                    await stream.close()
                if status_is_throttled(failure):
                    telemetry.record_throttled(self.chat.deployment, ModelCall.ANSWER.value)
                wait = _retry_wait(failure)
                if wait is None or attempt + 1 == ANSWER_ATTEMPTS:
                    raise
                await self._sleep(wait)
            else:
                return stream, iterator, first
        raise RuntimeError("unreachable")  # pragma: no cover - the loop returns or raises

    def _consume_chunk(
        self,
        chunk: Any,
        progress: _Progress,
        minter: CitationMinter,
        emit: Callable[[str], None],
        on_degraded: Callable[[], None],
    ) -> None:
        choices = _member(chunk, "choices")
        if isinstance(choices, Sequence) and choices:
            choice = choices[0]
            content = _member(_member(choice, "delta"), "content")
            if isinstance(content, str) and content:
                released = minter.feed(content)
                if released:
                    emit(released)
            if not progress.degraded and _filter_degraded(choice):
                progress.degraded = True
                on_degraded()
            reason = _member(choice, "finish_reason")
            if isinstance(reason, str):
                progress.finish_reason = reason
        if _member(chunk, "usage") is not None:
            progress.usage_chunk = _member(chunk, "usage")

    async def _stream(
        self,
        parameters: dict[str, Any],
        progress: _Progress,
        minter: CitationMinter,
        emit: Callable[[str], None],
        on_degraded: Callable[[], None],
    ) -> None:
        """Open the stream and read it to its end, the answer cap or a failure."""
        try:
            progress.stream, iterator, chunk = await self._open(parameters)
        except openai.APIError as error:
            progress.failure = error
            return
        progress.outcome = CallOutcome.OK
        if chunk is not None:
            progress.first_chunk_ms = elapsed_ms(self._clock, progress.started)
            telemetry.record_first_chunk(progress.first_chunk_ms, self.chat.deployment)
        try:
            while chunk is not None:
                self._consume_chunk(chunk, progress, minter, emit, on_degraded)
                if minter.capped:
                    break
                chunk = await anext(iterator, None)
        except openai.APIError:
            # A cut or silent stream after the first chunk: no retry, no finish reason.
            progress.finish_reason = None
        if not minter.capped:
            tail = minter.finish()
            if tail:
                emit(tail)

    def _record(
        self, progress: _Progress, minter: CitationMinter, ledger: UsageLedger, prompt_tokens: int
    ) -> Ending:
        """The call's ending, its filter event and its usage record."""
        context = ledger.context
        ending = classify(
            finish_reason=progress.finish_reason,
            capped=minter.capped,
            answer=minter.answer,
            failure=progress.failure,
        )
        if ending in (Ending.PROMPT_FILTERED, Ending.COMPLETION_FILTERED):
            progress.outcome = CallOutcome.FILTERED
            telemetry.report_content_filter(
                where="prompt" if ending is Ending.PROMPT_FILTERED else "completion",
                call=ModelCall.ANSWER.value,
                deployment=self.chat.deployment,
                project_id=context.project_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
            )
        tokenizer = self.chat.tokenizer
        received = "".join(minter.received)

        def estimate() -> ReportedUsage:
            return ReportedUsage(
                prompt_tokens=prompt_tokens, completion_tokens=count_sync(tokenizer, [received])
            )

        progress.usage = reported_usage(progress.usage_chunk)
        _ = ledger.submit(
            call=ModelCall.ANSWER,
            deployment=self.chat.deployment,
            model=self.chat.model,
            duration_ms=elapsed_ms(self._clock, progress.started),
            outcome=progress.outcome,
            usage=progress.usage,
            estimate=estimate,
        )
        return ending

    async def run(
        self,
        *,
        messages: Sequence[Message],
        prompt_tokens: int,
        max_completion_tokens: int,
        minter: CitationMinter,
        emit: Callable[[str], None],
        ledger: UsageLedger,
        on_degraded: Callable[[], None],
    ) -> Generation:
        """Make the call; `emit` receives each released text. Cancellation closes the stream,
        records the call's usage and propagates."""
        parameters = self.parameters(messages, max_completion_tokens, ledger.context)
        progress = _Progress(started=self._clock.monotonic())
        attributes: dict[str, object] = {
            "docchat.deployment": self.chat.deployment,
            "docchat.model": self.chat.model,
            "docchat.reasoning_effort": self.chat.reasoning_effort.value,
            "docchat.prompt_tokens": prompt_tokens,
        }
        with start_span("docchat.chat.generate", **attributes) as span:
            cancelled = False
            try:
                await self._stream(parameters, progress, minter, emit, on_degraded)
            except asyncio.CancelledError:
                progress.outcome = ledger.cancel_outcome
                cancelled = True
                raise
            finally:
                if progress.stream is not None:
                    await progress.stream.close()
                ending = self._record(progress, minter, ledger, prompt_tokens)
                set_attributes(span, **progress.span_attributes(ending, cancelled=cancelled))
        return Generation(
            ending=ending,
            minter=minter,
            retry_after_seconds=busy_retry_after(progress.failure)
            if ending is Ending.BUSY
            else None,
            usage=progress.usage,
            first_chunk_ms=progress.first_chunk_ms,
            duration_ms=elapsed_ms(self._clock, progress.started),
        )


@dataclass(slots=True)
class _Progress:
    """What one answer call has seen so far."""

    started: float
    stream: Any = None
    failure: BaseException | None = None
    finish_reason: str | None = None
    usage_chunk: object = None
    usage: ReportedUsage | None = None
    first_chunk_ms: int | None = None
    degraded: bool = False
    outcome: CallOutcome = CallOutcome.FAILED

    def span_attributes(self, ending: Ending, *, cancelled: bool) -> dict[str, object]:
        attributes: dict[str, object] = {
            "docchat.finish_reason": FinishReason.CANCELLED.value
            if cancelled
            else ENDINGS[ending].finish_reason.value
        }
        if self.first_chunk_ms is not None:
            attributes["docchat.first_chunk_ms"] = self.first_chunk_ms
        if self.usage is not None:
            attributes["docchat.completion_tokens"] = self.usage.completion_tokens
            attributes["docchat.reasoning_tokens"] = self.usage.reasoning_tokens
        return attributes


def status_is_throttled(failure: BaseException) -> bool:
    return (
        isinstance(failure, openai.APIStatusError)
        and failure.status_code == HTTPStatus.TOO_MANY_REQUESTS
    )
