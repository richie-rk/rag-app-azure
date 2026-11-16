"""The two chat-small calls of the after-response work, `summary` and `memory`
(`13-conversations-and-memory.md` sections 6.7 and 7.13; `12-chat-pipeline.md` sections 10.6,
10.8, 15 and 17.7).

Both go through the process's one `AsyncOpenAI` client (`max_retries=0`, so only the rule here
retries), non-streaming, on the chat-small deployment with the reasoning effort of their own
ops-config area, `safety_identifier`, and the explicit prompt-cache option when the deployment's
prompt cache is `disabled`. Each attempt runs inside `asyncio.timeout` of its call's limit, and a
call has at most 2 attempts: a retry follows only a connection error (a timed-out attempt counts as
one), a `408`, a `429` whose stated delay is at most 10 s, or a `500`, `502`, `503` or `504`, after
the stated delay or 2 s, and never a `400`.

Every attempt produces one `ModelCallUsage` with the call's name, from the usage the response
reported or estimated from the prompt as sent and the text received (counted with the chat-small
encoding under the 20,000-character rule), and hands it to the recorder at once, so the
after-response calls count against the user's quotas and cost like chat calls. The record is built
in a task of its own, so cancelling the work at the drain can't skip the accounting of an attempt
that was made.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final
from uuid import UUID

import openai
from openai import AsyncOpenAI

from docchat_api.chat.calls import (
    ChatDeploymentSettings,
    ReportedUsage,
    UsageRecorder,
    elapsed_ms,
    is_prompt_filtered,
    reported_usage,
    stated_delay,
)
from docchat_api.chat.prompts import Message, count_messages
from docchat_api.tokens import count_tokens
from docchat_api.usage import QuotaCheck, model_call_usage, record_model_call
from docchat_core.cosmos.conversations import (
    CallOutcome,
    InternalErrorCode,
    ModelCall,
    ModelCallUsage,
    ReasoningEffort,
)
from docchat_core.models.clock import Clock
from docchat_core.opsconfig import OpsReasoningEffort, PricingOps

type Sleep = Callable[[float], Awaitable[None]]
type QuotaGate = Callable[[UUID], Awaitable[QuotaCheck]]
"""`docchat_api.usage.check_token_quota`: the background quota gate."""

ATTEMPTS: Final = 2
RETRY_WAIT_SECONDS: Final = 2.0
RETRY_AFTER_MAX_SECONDS: Final = 10.0
RETRIED_SERVER_STATUSES: Final = frozenset(
    {
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)
CONTENT_FILTER: Final = "content_filter"

_ACCOUNTING: Final[set[asyncio.Task[ModelCallUsage]]] = set()
"""The record builds still running, held until each ends: a build outlives a cancelled call."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CallTarget:
    """Who a call is made for: the user whose quotas and counters it uses, the session (and turn)
    its records are labelled with, the user's safety identifier and the snapshot's price table."""

    oid: UUID
    session_id: UUID
    turn_id: UUID | None
    safety_identifier: str
    pricing: PricingOps


@dataclass(frozen=True, slots=True)
class CallReply:
    """A call's ending: the first choice's content, finish reason and refusal when a response
    arrived; `failure` when the call failed (`model_unavailable` after its attempts, or
    `content_filtered` for a filtered prompt); one usage record per attempt made."""

    content: str | None
    finish_reason: str | None
    refusal: object
    failure: InternalErrorCode | None
    records: tuple[ModelCallUsage, ...]

    @property
    def filtered(self) -> bool:
        """A filtered prompt (`400` with code `content_filter`) or a filtered completion."""
        return (
            self.failure is InternalErrorCode.CONTENT_FILTERED
            or self.finish_reason == CONTENT_FILTER
        )

    @property
    def last_record(self) -> ModelCallUsage | None:
        return self.records[-1] if self.records else None


def _member(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def first_choice(response: object) -> tuple[str | None, str | None, object]:
    """The first choice's content and finish reason, and the message's refusal."""
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


def retry_wait(failure: openai.APIStatusError) -> float | None:
    """The wait before the retry of a status error, or `None` when it isn't retried: a `408`, a
    `429` whose stated delay is at most 10 s, and a `500`, `502`, `503` or `504` are retried after
    the stated delay or 2 s."""
    status = failure.status_code
    delay = stated_delay(failure)
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        if delay is not None and delay > RETRY_AFTER_MAX_SECONDS:
            return None
    elif status != HTTPStatus.REQUEST_TIMEOUT and status not in RETRIED_SERVER_STATUSES:
        return None
    return delay if delay is not None else RETRY_WAIT_SECONDS


@dataclass(slots=True)
class _Attempt:
    """One attempt's ending: the response, the call outcome, the wait before a retry (`None`:
    no retry) and a failure that ends the call at once."""

    response: Any = None
    outcome: CallOutcome = CallOutcome.FAILED
    wait: float | None = None
    failure: InternalErrorCode | None = None


async def _shielded(work: Coroutine[Any, Any, ModelCallUsage]) -> ModelCallUsage:
    task = asyncio.get_running_loop().create_task(work)
    _ACCOUNTING.add(task)
    task.add_done_callback(_ACCOUNTING.discard)
    return await asyncio.shield(task)


class SmallModel:
    """The after-response work's caller of the chat-small deployment."""

    def __init__(
        self,
        client: AsyncOpenAI,
        deployment: ChatDeploymentSettings,
        *,
        clock: Clock,
        recorder: UsageRecorder = record_model_call,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.client = client
        self.deployment = deployment
        self._clock = clock
        self._recorder = recorder
        self._sleep = sleep

    def parameters(
        self,
        *,
        messages: Sequence[Message],
        max_completion_tokens: int,
        reasoning_effort: OpsReasoningEffort,
        safety_identifier: str,
        response_format: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """The request: the deployment's parameters with the area's reasoning effort, the
        messages, and the response format when the call has one; nothing else."""
        deployment = dataclasses.replace(
            self.deployment, reasoning_effort=ReasoningEffort(reasoning_effort)
        )
        parameters = deployment.parameters(max_completion_tokens, safety_identifier)
        parameters["messages"] = [message.payload() for message in messages]
        if response_format is not None:
            parameters["response_format"] = dict(response_format)
        return parameters

    async def complete(
        self,
        *,
        call: ModelCall,
        messages: Sequence[Message],
        max_completion_tokens: int,
        reasoning_effort: OpsReasoningEffort,
        timeout_seconds: float,
        target: CallTarget,
        response_format: Mapping[str, Any] | None = None,
    ) -> CallReply:
        """One call with its retry rule; each attempt's record goes to the recorder, and the
        counter writes are awaited before the reply returns."""
        parameters = self.parameters(
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            safety_identifier=target.safety_identifier,
            response_format=response_format,
        )
        records: list[ModelCallUsage] = []
        writes: list[Awaitable[None]] = []
        attempt = _Attempt()
        for number in range(ATTEMPTS):
            started = self._clock.monotonic()
            attempt = _Attempt()
            try:
                await self._send(parameters, timeout_seconds, attempt)
            finally:
                record = await _shielded(
                    self._account(
                        call=call,
                        messages=messages,
                        attempt=attempt,
                        duration_ms=elapsed_ms(self._clock, started),
                        target=target,
                    )
                )
                records.append(record)
                # Started now, whatever the recorder returns: a call cancelled here (the work
                # killed mid-call) never awaits `writes`, and its record must still be written.
                writes.append(
                    asyncio.ensure_future(
                        self._recorder(
                            target.oid,
                            record,
                            session_id=target.session_id,
                            turn_id=target.turn_id,
                        )
                    )
                )
            if attempt.response is not None or attempt.failure is not None:
                break
            if attempt.wait is None or number + 1 == ATTEMPTS:
                break
            await self._sleep(attempt.wait)
        await asyncio.gather(*writes, return_exceptions=True)
        if attempt.response is None:
            return CallReply(
                None,
                None,
                None,
                attempt.failure or InternalErrorCode.MODEL_UNAVAILABLE,
                tuple(records),
            )
        content, finish, refusal = first_choice(attempt.response)
        return CallReply(content, finish, refusal, None, tuple(records))

    async def _send(
        self, parameters: Mapping[str, Any], timeout_seconds: float, attempt: _Attempt
    ) -> None:
        """One attempt, its ending written into `attempt`."""
        try:
            async with asyncio.timeout(timeout_seconds):
                attempt.response = await self.client.chat.completions.create(**parameters)
            attempt.outcome = CallOutcome.OK
        except (TimeoutError, openai.APITimeoutError):
            attempt.outcome = CallOutcome.TIMED_OUT
            attempt.wait = RETRY_WAIT_SECONDS
        except openai.APIConnectionError:
            attempt.wait = RETRY_WAIT_SECONDS
        except openai.APIStatusError as error:
            if is_prompt_filtered(error):
                attempt.outcome = CallOutcome.FILTERED
                attempt.failure = InternalErrorCode.CONTENT_FILTERED
            else:
                attempt.wait = retry_wait(error)
        except openai.APIError:
            # Any other client failure (a response that couldn't be decoded) isn't retried.
            attempt.wait = None
        except asyncio.CancelledError:
            attempt.outcome = CallOutcome.CANCELLED
            raise
        _, finish, _ = first_choice(attempt.response)
        if finish == CONTENT_FILTER:
            attempt.outcome = CallOutcome.FILTERED

    async def _account(
        self,
        *,
        call: ModelCall,
        messages: Sequence[Message],
        attempt: _Attempt,
        duration_ms: int,
        target: CallTarget,
    ) -> ModelCallUsage:
        """One attempt's record: the usage the response reported, or the estimate."""
        response = attempt.response
        reported = reported_usage(_member(response, "usage")) if response is not None else None
        counted = reported
        if counted is None:
            tokenizer = self.deployment.tokenizer
            received, _, _ = first_choice(response)
            counted = ReportedUsage(
                prompt_tokens=await count_messages(tokenizer, messages),
                completion_tokens=await count_tokens(tokenizer, received or ""),
            )
        return model_call_usage(
            target.pricing,
            call=call,
            deployment=self.deployment.deployment,
            model=self.deployment.model,
            prompt_tokens=counted.prompt_tokens,
            completion_tokens=counted.completion_tokens,
            reasoning_tokens=counted.reasoning_tokens,
            cached_tokens=counted.cached_tokens,
            cache_write_tokens=counted.cache_write_tokens,
            estimated=reported is None,
            duration_ms=duration_ms,
            outcome=attempt.outcome,
        )
