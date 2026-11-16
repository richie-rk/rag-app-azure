"""The chat-small and embedding calls, the safety identifier and the usage ledger (T-252's value
check, T-274; `12-chat-pipeline.md` sections 6.2, 10.8, 15 and 17)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast, override
from uuid import UUID, uuid4

import openai
import pytest

from docchat_api.chat.calls import (
    STANDALONE_QUERY_FORMAT,
    CallContext,
    ChatDeploymentSettings,
    ModelCalls,
    ReportedUsage,
    StandaloneQuery,
    UsageLedger,
    message_tokens,
    reported_usage,
    safety_identifier,
    stated_delay,
)
from docchat_api.chat.prompts import Message
from docchat_core.cosmos.conversations import (
    CallOutcome,
    ModelCall,
    ModelCallUsage,
    ReasoningEffort,
)
from docchat_core.opsconfig import PricingOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import CharacterTokenizer, FakeModelClient, completion

TID = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
OID = UUID("0a11ce00-0000-4000-8000-000000000001")
PRICING = PricingOps.model_validate(
    {
        "pricing.currency": "USD",
        "pricing.model_rates": {
            "gpt-5.6-terra": {
                "inputPerMillion": "1",
                "cachedInputPerMillion": "0.5",
                "cacheWritePerMillion": "2",
                "outputPerMillion": "4",
            }
        },
    }
)
SMALL = ChatDeploymentSettings(
    deployment="chat-small",
    model="gpt-5.6-terra",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.NONE,
    prompt_cache="disabled",
    tokenizer=CharacterTokenizer(),
)


class Recorder:
    def __init__(self) -> None:
        self.records: list[ModelCallUsage] = []
        self.gate = asyncio.Event()
        self.gate.set()

    def __call__(self, oid: UUID, record: ModelCallUsage, **_: Any) -> asyncio.Future[None]:
        self.records.append(record)
        return asyncio.ensure_future(self._released())

    async def _released(self) -> None:
        await self.gate.wait()


def ledger(recorder: Recorder | None = None) -> UsageLedger:
    context = CallContext(
        oid=OID,
        project_id=uuid4(),
        session_id=uuid4(),
        turn_id=uuid4(),
        safety_identifier=safety_identifier(TID, OID),
    )
    return UsageLedger(context=context, pricing=PRICING, recorder=recorder or Recorder())


def calls(model: FakeModelClient, clock: FakeClock | None = None) -> ModelCalls:
    return ModelCalls(model.client(), small=SMALL, clock=clock or FakeClock(), sleep=_no_sleep)


async def _no_sleep(_seconds: float) -> None:
    return None


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


def test_t252_the_safety_identifier_for_a_known_tid_and_oid() -> None:
    expected = hashlib.sha256(f"docchat:{TID}:{OID}".encode()).hexdigest()[:32]

    assert safety_identifier(TID, OID) == expected
    assert len(expected) == 32


def test_reported_usage_reads_every_detail_and_counts_missing_ones_as_zero() -> None:
    full = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30, cache_write_tokens=10),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
    )

    assert reported_usage(full) == ReportedUsage(
        prompt_tokens=100,
        completion_tokens=40,
        reasoning_tokens=12,
        cached_tokens=30,
        cache_write_tokens=10,
    )
    assert reported_usage(SimpleNamespace(prompt_tokens=5)) == ReportedUsage(
        prompt_tokens=5, completion_tokens=0
    )
    assert reported_usage(None) is None


def test_stated_delays() -> None:
    assert stated_delay(status_error(429, {"retry-after-ms": "1500"})) == 1.5
    assert stated_delay(status_error(429, {"retry-after": "7"})) == 7.0
    assert stated_delay(status_error(429)) is None


def test_the_counting_rule_of_a_message_list() -> None:
    assert message_tokens(CharacterTokenizer(), [Message("user", "abc")]) == 3 + 3 + 3


# --- The ledger -------------------------------------------------------------------------------


async def test_t274_records_from_reported_usage_are_priced_in_micro_units() -> None:
    recorder = Recorder()
    book = ledger(recorder)
    _ = book.submit(
        call=ModelCall.REWRITE,
        deployment="chat-small",
        model="gpt-5.6-terra",
        duration_ms=12,
        outcome=CallOutcome.OK,
        usage=ReportedUsage(
            prompt_tokens=1_000,
            completion_tokens=500,
            reasoning_tokens=100,
            cached_tokens=200,
            cache_write_tokens=100,
        ),
        estimate=lambda: pytest.fail("no estimate for reported usage"),
    )

    (record,) = await book.records()

    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (
        1_000,
        500,
        1_500,
    )
    assert (record.reasoning_tokens, record.cached_tokens, record.cache_write_tokens) == (
        100,
        200,
        100,
    )
    assert record.estimated is False
    # 700 x 1 + 200 x 0.5 + 100 x 2 + 500 x 4 micro-units.
    assert record.estimated_cost_micros == 3_000
    assert recorder.records == [record]


async def test_t274_a_missing_usage_chunk_gives_an_estimated_record() -> None:
    book = ledger()
    _ = book.submit(
        call=ModelCall.ANSWER,
        deployment="chat",
        model="gpt-5.6-sol",
        duration_ms=1,
        outcome=CallOutcome.CANCELLED,
        usage=None,
        estimate=lambda: ReportedUsage(prompt_tokens=50, completion_tokens=7),
    )

    (record,) = await book.records()

    assert record.estimated is True
    assert record.outcome is CallOutcome.CANCELLED
    assert (record.prompt_tokens, record.completion_tokens, record.reasoning_tokens) == (50, 7, 0)
    assert record.estimated_cost_micros is None


async def test_settle_waits_for_every_counter_write() -> None:
    recorder = Recorder()
    recorder.gate.clear()
    book = ledger(recorder)
    _ = book.submit(
        call=ModelCall.REWRITE,
        deployment="chat-small",
        model="gpt-5.6-terra",
        duration_ms=1,
        outcome=CallOutcome.OK,
        usage=ReportedUsage(prompt_tokens=1, completion_tokens=1),
        estimate=lambda: ReportedUsage(prompt_tokens=0, completion_tokens=0),
    )
    settling = asyncio.create_task(book.settle())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not settling.done()
    recorder.gate.set()
    await settling


# --- Chat-small calls -------------------------------------------------------------------------


async def complete(
    model: FakeModelClient, book: UsageLedger | None = None, seconds: float = 8.0
) -> Any:
    return await calls(model).complete_json(
        call=ModelCall.REWRITE,
        messages=[Message("system", "s"), Message("user", "u")],
        response_format=STANDALONE_QUERY_FORMAT,
        model_type=StandaloneQuery,
        max_completion_tokens=800,
        timeout_seconds=seconds,
        ledger=book or ledger(),
    )


async def test_t252_a_chat_small_request_sends_exactly_its_parameters() -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].append(json.dumps({"query": "q"}))

    reply = await complete(model)

    assert reply.value == StandaloneQuery(query="q")
    (request,) = model.chat_requests()
    assert request == {
        "kind": "chat",
        "model": "chat-small",
        "max_completion_tokens": 800,
        "reasoning_effort": "none",
        "safety_identifier": safety_identifier(TID, OID),
        "extra_body": {"prompt_cache_options": {"mode": "explicit"}},
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        "response_format": STANDALONE_QUERY_FORMAT,
        "stream": False,
    }
    for never in (
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "functions",
        "max_tokens",
        "user",
        "stop",
    ):
        assert never not in request
    assert STANDALONE_QUERY_FORMAT["json_schema"] == {
        "name": "standalone_query",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }


@pytest.mark.parametrize(
    "scripted",
    [
        completion("not json"),
        completion(json.dumps({"query": "q", "extra": 1})),
        completion(json.dumps({"query": "q"}), finish_reason="length"),
        completion(None, refusal="no"),
    ],
    ids=["unparsable", "extra-member", "length", "refusal"],
)
async def test_unusable_output_takes_the_fallback(scripted: Any) -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].append(scripted)

    reply = await complete(model)

    assert reply.value is None
    assert reply.outcome is CallOutcome.OK


async def test_a_filtered_prompt_or_completion_is_filtered_and_reported(captured: Any) -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].extend(
        [status_error(400, code="content_filter"), completion("{}", finish_reason="content_filter")]
    )

    first = await complete(model)
    second = await complete(model)

    assert (first.value, first.outcome) == (None, CallOutcome.FILTERED)
    assert (second.value, second.outcome) == (None, CallOutcome.FILTERED)
    assert [event["where"] for event in captured.events("content_filter_triggered")] == [
        "prompt",
        "completion",
    ]


async def test_a_failure_and_a_timeout_record_estimates() -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].append(status_error(500))
    book = ledger()

    failed = await complete(model, book)

    class Hanging(FakeModelClient):
        @override
        async def _create(self, **arguments: Any) -> Any:
            await asyncio.Event().wait()

    timed = await complete(Hanging(), book, seconds=0.01)
    records = await book.records()

    assert failed.outcome is CallOutcome.FAILED
    assert timed.outcome is CallOutcome.TIMED_OUT
    assert [record.estimated for record in records] == [True, True]
    assert records[0].prompt_tokens == message_tokens(
        CharacterTokenizer(), [Message("system", "s"), Message("user", "u")]
    )


async def test_a_cancelled_call_records_with_the_ledgers_cancel_outcome() -> None:
    class Hanging(FakeModelClient):
        @override
        async def _create(self, **arguments: Any) -> Any:
            await asyncio.Event().wait()

    book = ledger()
    book.cancel_outcome = CallOutcome.TIMED_OUT
    task = asyncio.create_task(complete(Hanging(), book))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = await book.records()
    assert record.outcome is CallOutcome.TIMED_OUT
    assert record.estimated


# --- Embeddings -------------------------------------------------------------------------------


async def embed(
    model: FakeModelClient, book: UsageLedger | None = None, dimensions: int = 1024
) -> list[float] | None:
    return await calls(model).embed(
        call=ModelCall.QUERY_EMBEDDING,
        deployment="embed-query",
        model="text-embedding-3-large",
        dimensions=dimensions,
        text="query",
        tokenizer=CharacterTokenizer(),
        ledger=book or ledger(),
        search_pass="first",
    )


async def test_an_embedding_request_names_the_deployment_and_dimensions() -> None:
    model = FakeModelClient()
    book = ledger()

    vector = await embed(model, book)

    assert vector is not None
    assert len(vector) == 1024
    assert model.embedding_requests() == [
        {"kind": "embed", "model": "embed-query", "input": "query", "dimensions": 1024}
    ]
    (record,) = await book.records()
    assert (record.call, record.prompt_tokens, record.completion_tokens) == (
        ModelCall.QUERY_EMBEDDING,
        7,
        0,
    )


@pytest.mark.parametrize(
    "failure",
    [
        openai.APIConnectionError(request=cast("Any", None)),
        status_error(408),
        status_error(429, {"retry-after": "5"}),
        status_error(503),
    ],
    ids=["connection", "408", "429-5s", "503"],
)
async def test_retried_embedding_failures(failure: Exception) -> None:
    model = FakeModelClient()
    model.embeddings_script.append(failure)

    assert await embed(model) is not None
    assert len(model.embedding_requests()) == 2


@pytest.mark.parametrize(
    "failure",
    [status_error(429, {"retry-after": "6"}), status_error(400), status_error(404)],
    ids=["429-6s", "400", "404"],
)
async def test_embedding_failures_that_arent_retried(failure: Exception) -> None:
    model = FakeModelClient()
    model.embeddings_script.append(failure)
    book = ledger()

    assert await embed(model, book) is None
    assert len(model.embedding_requests()) == 1
    (record,) = await book.records()
    assert (record.outcome, record.estimated, record.prompt_tokens) == (CallOutcome.FAILED, True, 5)


async def test_a_vector_of_another_length_is_a_failure() -> None:
    assert await embed(FakeModelClient(), dimensions=256) is not None
    model = FakeModelClient()
    model.embeddings_script.append(
        SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])], usage=None)
    )

    assert await embed(model) is None


def test_deployment_settings_name_their_trace() -> None:
    assert SMALL.trace().model_dump(by_alias=True, mode="json") == {
        "deployment": "chat-small",
        "model": "gpt-5.6-terra",
        "modelVersion": "2026-07-09",
        "reasoningEffort": "none",
    }
    automatic = ChatDeploymentSettings(
        deployment="d",
        model="m",
        model_version="v",
        reasoning_effort=ReasoningEffort.LOW,
        prompt_cache="automatic",
        tokenizer=CharacterTokenizer(),
    )
    assert "extra_body" not in automatic.parameters(10, "s")
