"""The after-response work's chat-small calls: parameters, the retry rule and one usage record per
attempt (`13-conversations-and-memory.md` sections 6.7 and 7.13; `12-chat-pipeline.md` sections
10.8 and 15)."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import openai
import pytest

from docchat_api.chat.calls import ChatDeploymentSettings, safety_identifier
from docchat_api.chat.prompts import Message
from docchat_api.conversations.model_calls import (
    CallTarget,
    SmallModel,
    first_choice,
    retry_wait,
)
from docchat_core.cosmos.conversations import (
    CallOutcome,
    InternalErrorCode,
    ModelCall,
    ModelCallUsage,
    ReasoningEffort,
)
from docchat_core.opsconfig import PricingOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import (
    PLAIN_TEXT,
    CharacterTokenizer,
    FakeModelClient,
    completion,
    usage_of,
)

TID = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
OID = UUID("0a11ce00-0000-4000-8000-000000000001")
SESSION = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
TURN = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
PRICING = PricingOps.model_validate({"pricing.currency": "USD", "pricing.model_rates": {}})
SMALL = ChatDeploymentSettings(
    deployment="chat-small",
    model="gpt-5.6-terra",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.NONE,
    prompt_cache="disabled",
    tokenizer=CharacterTokenizer(),
)
TARGET = CallTarget(
    oid=OID,
    session_id=SESSION,
    turn_id=TURN,
    safety_identifier=safety_identifier(TID, OID),
    pricing=PRICING,
)
MESSAGES = (Message("system", "rules"), Message("user", "data"))
FORMAT = {"type": "json_schema", "json_schema": {"name": "memory_operations", "strict": True}}
NEVER_SENT = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "n",
    "stop",
    "tools",
    "tool_choice",
    "functions",
    "max_tokens",
    "user",
    "verbosity",
)


class Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, ModelCallUsage, dict[str, Any]]] = []

    def __call__(self, oid: UUID, record: ModelCallUsage, **labels: Any) -> asyncio.Future[None]:
        self.records.append((oid, record, labels))
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


def status_error(
    status: int, headers: dict[str, str] | None = None, code: str | None = None
) -> Exception:
    response = cast("Any", SimpleNamespace(status_code=status, headers=headers or {}, request=None))
    body = {"code": code} if code is not None else None
    if status == 400:
        return openai.BadRequestError("bad", response=response, body=body)
    if status == 429:
        return openai.RateLimitError("slow", response=response, body=body)
    if status >= 500:
        return openai.InternalServerError("down", response=response, body=body)
    return openai.APIStatusError("other", response=response, body=body)


def connection_error() -> Exception:
    return openai.APIConnectionError(request=cast("Any", None))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def model() -> FakeModelClient:
    return FakeModelClient()


def small(
    model: FakeModelClient,
    clock: FakeClock,
    recorder: Recorder,
    deployment: ChatDeploymentSettings = SMALL,
) -> SmallModel:
    return SmallModel(model.client(), deployment, clock=clock, recorder=recorder, sleep=clock.sleep)


async def complete(
    caller: SmallModel, *, call: ModelCall = ModelCall.SUMMARY, response_format: Any = None
) -> Any:
    return await caller.complete(
        call=call,
        messages=MESSAGES,
        max_completion_tokens=2_000,
        reasoning_effort="low",
        timeout_seconds=30,
        target=TARGET,
        response_format=response_format,
    )


async def test_the_request_carries_exactly_the_call_parameters(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    await complete(small(model, clock, recorder))
    await complete(small(model, clock, recorder), call=ModelCall.MEMORY, response_format=FORMAT)

    summary, memory = model.chat_requests()
    assert summary == {
        "kind": "chat",
        "model": "chat-small",
        "max_completion_tokens": 2_000,
        "reasoning_effort": "low",
        "safety_identifier": safety_identifier(TID, OID),
        "extra_body": {"prompt_cache_options": {"mode": "explicit"}},
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "data"},
        ],
    }
    assert memory["response_format"] == FORMAT
    for request in (summary, memory):
        assert not set(NEVER_SENT) & set(request)


async def test_an_automatic_prompt_cache_sends_no_cache_option(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    automatic = dataclasses.replace(SMALL, prompt_cache="automatic")

    await complete(small(model, clock, recorder, automatic))

    assert "extra_body" not in model.chat_requests()[0]


async def test_a_reply_and_its_reported_usage(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(completion("A summary.", usage=usage_of(120, 30)))

    reply = await complete(small(model, clock, recorder))

    assert (reply.content, reply.finish_reason, reply.refusal, reply.failure) == (
        "A summary.",
        "stop",
        None,
        None,
    )
    ((oid, record, labels),) = recorder.records
    assert oid == OID
    assert labels == {"session_id": SESSION, "turn_id": TURN}
    assert record.call is ModelCall.SUMMARY
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (120, 30, 150)
    assert (record.estimated, record.outcome) == (False, CallOutcome.OK)
    assert (record.deployment, record.model) == ("chat-small", "gpt-5.6-terra")
    assert reply.last_record == record


async def test_a_response_without_usage_is_estimated_with_the_small_encoding(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(completion("abcd"))

    reply = await complete(small(model, clock, recorder))

    record = reply.records[0]
    # One token per character: the two contents, 3 per message and 3 for the reply priming.
    assert record.prompt_tokens == len("rules") + len("data") + 3 * 2 + 3
    assert record.completion_tokens == 4
    assert record.estimated


async def test_a_short_429_is_retried_after_its_stated_delay(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(status_error(429, {"retry-after": "3"}))
    started = clock.monotonic()

    reply = await complete(small(model, clock, recorder))

    assert reply.content is not None
    assert clock.monotonic() - started == 3
    assert [record.outcome for record in reply.records] == [CallOutcome.FAILED, CallOutcome.OK]
    assert reply.records[0].estimated
    assert len(recorder.records) == 2


async def test_a_long_429_is_not_retried(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(status_error(429, {"retry-after-ms": "30000"}))

    reply = await complete(small(model, clock, recorder))

    assert reply.failure is InternalErrorCode.MODEL_UNAVAILABLE
    assert len(model.chat_requests()) == 1
    assert len(reply.records) == 1


@pytest.mark.parametrize(
    ("failure", "wait"),
    [
        (TimeoutError(), 2.0),
        (connection_error(), 2.0),
        (status_error(408), 2.0),
        (status_error(503, {"retry-after-ms": "1500"}), 1.5),
        (status_error(500), 2.0),
    ],
)
async def test_transient_failures_are_retried_once(
    model: FakeModelClient,
    clock: FakeClock,
    recorder: Recorder,
    failure: Exception,
    wait: float,
) -> None:
    model.replies[PLAIN_TEXT].extend([failure, failure])
    started = clock.monotonic()

    reply = await complete(small(model, clock, recorder))

    assert reply.failure is InternalErrorCode.MODEL_UNAVAILABLE
    assert len(model.chat_requests()) == 2
    assert clock.monotonic() - started == wait
    expected = CallOutcome.TIMED_OUT if isinstance(failure, TimeoutError) else CallOutcome.FAILED
    assert [record.outcome for record in reply.records] == [expected, expected]


async def test_a_filtered_prompt_fails_at_once(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(status_error(400, code="content_filter"))

    reply = await complete(small(model, clock, recorder))

    assert reply.failure is InternalErrorCode.CONTENT_FILTERED
    assert reply.filtered
    assert len(model.chat_requests()) == 1
    assert reply.records[0].outcome is CallOutcome.FILTERED


async def test_another_400_is_never_retried(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(status_error(400, code="invalid_value"))

    reply = await complete(small(model, clock, recorder))

    assert reply.failure is InternalErrorCode.MODEL_UNAVAILABLE
    assert not reply.filtered
    assert len(model.chat_requests()) == 1


async def test_a_filtered_completion_is_a_reply_with_the_filtered_outcome(
    model: FakeModelClient, clock: FakeClock, recorder: Recorder
) -> None:
    model.replies[PLAIN_TEXT].append(completion("", finish_reason="content_filter"))

    reply = await complete(small(model, clock, recorder))

    assert reply.failure is None
    assert reply.filtered
    assert reply.records[0].outcome is CallOutcome.FILTERED


async def test_a_cancelled_call_still_records_its_estimate(
    clock: FakeClock, recorder: Recorder
) -> None:
    started = asyncio.Event()

    async def hang(**_arguments: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=hang)))
    caller = SmallModel(cast("Any", client), SMALL, clock=clock, recorder=recorder)
    task = asyncio.create_task(complete(caller))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    ((_, record, _),) = recorder.records
    assert (record.outcome, record.estimated, record.completion_tokens) == (
        CallOutcome.CANCELLED,
        True,
        0,
    )


def test_retry_wait_follows_the_rule() -> None:
    assert retry_wait(cast("Any", status_error(429))) == 2.0
    assert retry_wait(cast("Any", status_error(429, {"retry-after": "10"}))) == 10.0
    assert retry_wait(cast("Any", status_error(429, {"retry-after": "11"}))) is None
    assert retry_wait(cast("Any", status_error(502))) == 2.0
    assert retry_wait(cast("Any", status_error(501))) is None
    assert retry_wait(cast("Any", status_error(404))) is None


def test_first_choice_reads_a_response_or_nothing() -> None:
    assert first_choice(None) == (None, None, None)
    assert first_choice(SimpleNamespace(choices=[])) == (None, None, None)
    assert first_choice(completion("x", refusal="no")) == ("x", "stop", "no")
