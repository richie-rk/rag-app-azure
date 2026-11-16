"""The answer call: retries, stream processing and the finish-reason mapping (T-246, T-274's
estimate; `12-chat-pipeline.md` sections 10.1 to 10.4 and 15.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import openai
import pytest

from docchat_api.chat.calls import CallContext, ChatDeploymentSettings, UsageLedger
from docchat_api.chat.generation import (
    ENDINGS,
    AnswerCall,
    Ending,
    Generation,
    busy_retry_after,
    classify,
)
from docchat_api.chat.markers import CitationMinter
from docchat_api.chat.prompts import Message
from docchat_api.chat.sources import Candidate, NumberedSource, SearchHit
from docchat_core.cosmos.conversations import (
    CallOutcome,
    FinishReason,
    ModelCallUsage,
    ReasoningEffort,
)
from docchat_core.opsconfig import PricingOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import (
    CharacterTokenizer,
    FakeModelClient,
    FakeStream,
    chunk,
    text_stream,
    usage_of,
)

CHAT = ChatDeploymentSettings(
    deployment="chat",
    model="gpt-5.6-sol",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.LOW,
    prompt_cache="disabled",
    tokenizer=CharacterTokenizer(),
)
PRICING = PricingOps.model_validate({"pricing.currency": "USD", "pricing.model_rates": {}})
MESSAGES = [Message("system", "rules"), Message("user", "question")]


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


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def source() -> NumberedSource:
    hit = SearchHit(
        chunk_key="k",
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_set_id=uuid4(),
        title="T",
        heading_path=(),
        page_number=1,
        content_type="application/pdf",
        chunk_text="text",
        search_score=1.0,
        reranker_score=3.0,
    )
    candidate = Candidate(rank=1, hit=hit, source_name="a.pdf")
    return NumberedSource(number=1, candidate=candidate, body=candidate.body)


class Recorder:
    def __init__(self) -> None:
        self.records: list[ModelCallUsage] = []

    def __call__(self, oid: Any, record: ModelCallUsage, **_: Any) -> asyncio.Future[None]:
        self.records.append(record)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


def ledger() -> UsageLedger:
    context = CallContext(
        oid=uuid4(),
        project_id=uuid4(),
        session_id=uuid4(),
        turn_id=uuid4(),
        safety_identifier="s" * 32,
    )
    return UsageLedger(context=context, pricing=PRICING, recorder=Recorder())


async def generate(
    model: FakeModelClient, book: UsageLedger | None = None, sleeps: Sleeps | None = None
) -> tuple[Generation, list[str], list[str]]:
    emitted: list[str] = []
    degraded: list[str] = []
    answer = AnswerCall(model.client(), chat=CHAT, clock=FakeClock(), sleep=sleeps or Sleeps())
    generation = await answer.run(
        messages=MESSAGES,
        prompt_tokens=40,
        max_completion_tokens=12_000,
        minter=CitationMinter([source()]),
        emit=emitted.append,
        ledger=book or ledger(),
        on_degraded=lambda: degraded.append("degraded"),
    )
    return generation, emitted, degraded


# --- T-246: the finish-reason mapping -----------------------------------------------------------


@pytest.mark.parametrize(
    ("finish", "capped", "answer", "failure", "ending"),
    [
        ("stop", False, "x", None, Ending.STOP),
        ("stop", False, "", None, Ending.STOP),
        ("length", False, "visible", None, Ending.LENGTH),
        ("length", False, " \u27e61\u27e7 ", None, Ending.LENGTH_EMPTY),
        (None, True, "x", None, Ending.LENGTH),
        ("content_filter", False, "x", None, Ending.COMPLETION_FILTERED),
        (None, False, "x", None, Ending.UNAVAILABLE),
        (None, False, "", status_error(400, code="content_filter"), Ending.PROMPT_FILTERED),
        (None, False, "", status_error(400), Ending.REJECTED),
        (None, False, "", status_error(429), Ending.BUSY),
        (None, False, "", status_error(503), Ending.UNAVAILABLE),
        (None, False, "", openai.APITimeoutError(request=cast("Any", None)), Ending.UNAVAILABLE),
    ],
)
def test_t246_every_ending_maps_to_its_outcome(
    finish: str | None, capped: bool, answer: str, failure: Exception | None, ending: Ending
) -> None:
    assert classify(finish_reason=finish, capped=capped, answer=answer, failure=failure) is ending


def test_t246_the_turn_side_of_each_ending() -> None:
    assert {
        ending: (outcome.completed, outcome.finish_reason, outcome.error_code)
        for ending, outcome in ENDINGS.items()
    } == {
        Ending.STOP: (True, FinishReason.STOP, None),
        Ending.LENGTH: (True, FinishReason.LENGTH, None),
        Ending.LENGTH_EMPTY: (False, FinishReason.LENGTH, "answer_incomplete"),
        Ending.COMPLETION_FILTERED: (False, FinishReason.CONTENT_FILTER, "answer_filtered"),
        Ending.PROMPT_FILTERED: (False, FinishReason.CONTENT_FILTER, "question_filtered"),
        Ending.REJECTED: (False, FinishReason.ERROR, "model_request_rejected"),
        Ending.BUSY: (False, FinishReason.ERROR, "model_busy"),
        Ending.UNAVAILABLE: (False, FinishReason.ERROR, "model_unavailable"),
    }


def test_model_busy_waits_are_rounded_up_and_kept_from_1_to_60() -> None:
    assert busy_retry_after(status_error(429, {"retry-after-ms": "1200"})) == 2
    assert busy_retry_after(status_error(429, {"retry-after": "600"})) == 60
    assert busy_retry_after(status_error(429, {"retry-after-ms": "0"})) == 1
    assert busy_retry_after(status_error(429)) == 1


# --- The call ---------------------------------------------------------------------------------


async def test_the_request_and_a_streamed_answer() -> None:
    model = FakeModelClient()
    model.streams.append(text_stream(["Answer [S", "1] done."], usage=usage_of(40, 9, reasoning=3)))
    book = ledger()

    generation, emitted, _ = await generate(model, book)

    (request,) = model.answer_requests()
    assert request == {
        "kind": "chat",
        "model": "chat",
        "max_completion_tokens": 12_000,
        "reasoning_effort": "low",
        "safety_identifier": "s" * 32,
        "extra_body": {"prompt_cache_options": {"mode": "explicit"}},
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "question"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    for never in (
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "functions",
        "stop",
        "response_format",
        "max_tokens",
        "user",
    ):
        assert never not in request
    assert generation.ending is Ending.STOP
    assert "".join(emitted) == "Answer \u27e61\u27e7 done."
    assert model.opened[0].closed
    (record,) = await book.records()
    assert (record.estimated, record.reasoning_tokens, record.outcome) == (False, 3, CallOutcome.OK)


async def test_a_filter_error_annotation_marks_the_turn_degraded_once() -> None:
    model = FakeModelClient()
    model.streams.append(
        [chunk("a", filter_error=True), chunk("b", filter_error=True), chunk(finish_reason="stop")]
    )

    generation, emitted, degraded = await generate(model)

    assert generation.ending is Ending.STOP
    assert degraded == ["degraded"]
    assert "".join(emitted) == "ab"


@pytest.mark.parametrize(
    ("first", "wait"),
    [
        (openai.APIConnectionError(request=cast("Any", None)), 1.0),
        (status_error(408), 1.0),
        (status_error(429, {"retry-after": "10"}), 10.0),
        (status_error(500), 1.0),
        (status_error(502, {"retry-after-ms": "300"}), 0.3),
        (status_error(503), 1.0),
        (status_error(504), 1.0),
    ],
    ids=["connection", "408", "429-10s", "500", "502", "503", "504"],
)
async def test_retried_before_the_first_chunk(first: Exception, wait: float) -> None:
    model = FakeModelClient()
    model.streams.append(first)
    sleeps = Sleeps()

    generation, _, _ = await generate(model, sleeps=sleeps)

    assert generation.ending is Ending.STOP
    assert len(model.answer_requests()) == 2
    assert sleeps.waits == [wait]


@pytest.mark.parametrize(
    ("failure", "ending", "retry_after"),
    [
        (status_error(429, {"retry-after": "11"}), Ending.BUSY, 11),
        (status_error(400, code="content_filter"), Ending.PROMPT_FILTERED, None),
        (status_error(400), Ending.REJECTED, None),
        (status_error(404), Ending.UNAVAILABLE, None),
    ],
    ids=["429-11s", "prompt-filter", "400", "404"],
)
async def test_failures_that_arent_retried(
    failure: Exception, ending: Ending, retry_after: int | None, captured: Any
) -> None:
    model = FakeModelClient()
    model.streams.append(failure)
    book = ledger()

    generation, emitted, _ = await generate(model, book)

    assert (generation.ending, generation.retry_after_seconds) == (ending, retry_after)
    assert len(model.answer_requests()) == 1
    assert emitted == []
    (record,) = await book.records()
    assert record.estimated
    expected = CallOutcome.FILTERED if ending is Ending.PROMPT_FILTERED else CallOutcome.FAILED
    assert record.outcome is expected


async def test_two_throttled_attempts_count_twice_and_end_busy(captured: Any) -> None:
    model = FakeModelClient()
    model.streams.extend(
        [status_error(429, {"retry-after-ms": "200"}), status_error(429, {"retry-after-ms": "200"})]
    )

    generation, _, _ = await generate(model)

    assert generation.ending is Ending.BUSY
    assert generation.retry_after_seconds == 1
    assert sum(captured.points("docchat.openai.throttled").values()) == 2


async def test_nothing_is_retried_after_the_first_chunk() -> None:
    model = FakeModelClient()
    model.streams.append(
        FakeStream(
            [chunk("partial ")], failure=openai.APIConnectionError(request=cast("Any", None))
        )
    )

    generation, emitted, _ = await generate(model)

    assert generation.ending is Ending.UNAVAILABLE
    assert emitted == ["partial "]
    assert len(model.answer_requests()) == 1


async def test_a_stream_without_a_finish_reason_is_unavailable() -> None:
    model = FakeModelClient()
    model.streams.append(text_stream(["cut"], finish_reason=None))

    generation, _, _ = await generate(model)

    assert generation.ending is Ending.UNAVAILABLE


async def test_a_completion_filter_is_reported(captured: Any) -> None:
    model = FakeModelClient()
    model.streams.append(text_stream(["text"], finish_reason="content_filter"))

    generation, _, _ = await generate(model)

    assert generation.ending is Ending.COMPLETION_FILTERED
    (event,) = captured.events("content_filter_triggered")
    assert (event["where"], event["call"], event["deployment"]) == ("completion", "answer", "chat")


async def test_the_answer_cap_closes_the_stream_as_length() -> None:
    model = FakeModelClient()
    stream = FakeStream([chunk("a" * 60_000), chunk("b" * 60_000), chunk("never")])
    model.streams.append(stream)

    generation, emitted, _ = await generate(model)

    assert generation.ending is Ending.LENGTH
    assert len("".join(emitted)) == 100_000
    assert stream.closed


async def test_t274_cancelling_closes_the_stream_and_records_an_estimate() -> None:
    model = FakeModelClient()
    stream = FakeStream([chunk("partial [S")])
    stream.gate = asyncio.Event()
    model.streams.append(stream)
    book = ledger()
    task = asyncio.create_task(generate(model, book))
    for _ in range(10):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed
    (record,) = await book.records()
    assert (record.outcome, record.estimated) == (CallOutcome.CANCELLED, True)
    assert (record.prompt_tokens, record.completion_tokens) == (40, len("partial [S"))
