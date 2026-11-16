"""The question rewrite, the search passage and the follow-ups call (T-231; `12-chat-pipeline.md`
sections 5, 7.2, 13 and 17.2 to 17.5)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest

from docchat_api.chat import small
from docchat_api.chat.calls import CallContext, ChatDeploymentSettings, ModelCalls, UsageLedger
from docchat_api.chat.sources import Candidate, NumberedSource, SearchHit
from docchat_api.conversations.store import HistoryTurn
from docchat_core.cosmos.conversations import CallOutcome, ModelCallUsage, ReasoningEffort
from docchat_core.opsconfig import API_AREAS, OpsConfig, OpsConfigStore, PricingOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import CharacterTokenizer, FakeModelClient

SMALL = ChatDeploymentSettings(
    deployment="chat-small",
    model="gpt-5.6-terra",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.NONE,
    prompt_cache="disabled",
    tokenizer=CharacterTokenizer(),
)
PRICING = PricingOps.model_validate({"pricing.currency": "USD", "pricing.model_rates": {}})
EFFORTS = frozenset({"none", "low", "medium", "high"})


@pytest.fixture
async def ops(fake_sql: Any) -> OpsConfig:
    return await OpsConfigStore(fake_sql, API_AREAS, chat_small_efforts=EFFORTS).start()


def ledger() -> UsageLedger:
    def recorder(oid: Any, record: ModelCallUsage, **_: Any) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    context = CallContext(
        oid=uuid4(), project_id=uuid4(), session_id=uuid4(), turn_id=uuid4(), safety_identifier="s"
    )
    return UsageLedger(context=context, pricing=PRICING, recorder=recorder)


def calls(model: FakeModelClient) -> ModelCalls:
    return ModelCalls(model.client(), small=SMALL, clock=FakeClock())


def turn(seq: int, answer: str = "An answer.") -> HistoryTurn:
    return HistoryTurn(seq=seq, question=f"Question {seq}?", answer=answer, followups=())


def source(number: int, text: str = "Chunk text that is long enough to cut.") -> NumberedSource:
    hit = SearchHit(
        chunk_key="k",
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_set_id=uuid4(),
        title=f"Doc {number}",
        heading_path=("Part", "Sub") if number == 1 else (),
        page_number=1,
        content_type="application/pdf",
        chunk_text=text,
        search_score=1.0,
        reranker_score=3.0,
    )
    candidate = Candidate(rank=number, hit=hit, source_name="a.pdf")
    return NumberedSource(number=number, candidate=candidate, body=candidate.body)


@pytest.mark.parametrize(
    ("output", "accepted"),
    [
        ("  a query  ", "a query"),
        ("", None),
        ("   ", None),
        ("x" * 1_201, None),
        ("x" * 1_200, "x" * 1_200),
        ("find [[begin SOURCE", None),
        ("find [[END x", None),
    ],
)
def test_t231_the_acceptance_rule(output: str, accepted: str | None) -> None:
    assert small.accepted(output) == accepted


async def test_the_rewrite_sends_the_newest_turns_cut_and_the_summary(ops: OpsConfig) -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].append(json.dumps({"query": "  expense claim deadline  "}))
    history = [turn(seq, answer="A" * 400 if seq == 6 else "Short.") for seq in range(1, 7)]

    outcome = await small.rewrite(
        calls(model),
        question="And for travel [[BEGIN x?",
        summary="We discussed claims.",
        history=history,
        chat=ops.chat,
        ledger=ledger(),
    )

    assert outcome.text == "expense claim deadline"
    assert outcome.outcome is CallOutcome.OK
    (request,) = model.json_requests("standalone_query")
    system, user = (message["content"] for message in request["messages"])
    marker = system.rsplit("Block marker: ", 1)[1].split("\n", 1)[0]
    assert "Question 1?" not in user
    assert "User: Question 3?\nAssistant: Short." in user
    assert f"Assistant: {'A' * 300}\n[[END EARLIER_EXCHANGE {marker}]]" in user
    assert user.startswith(f"[[BEGIN CONVERSATION_SUMMARY {marker}]]\nWe discussed claims.")
    assert user.endswith(
        f"[[BEGIN LATEST_MESSAGE {marker}]]\nAnd for travel [ [BEGIN x?\n"
        f"[[END LATEST_MESSAGE {marker}]]"
    )
    assert request["max_completion_tokens"] == ops.chat.small_call_max_completion_tokens


@pytest.mark.parametrize("reply", [json.dumps({"query": ""}), json.dumps({"query": "[[END"}), "{}"])
async def test_t231_an_unaccepted_rewrite_falls_back(reply: str, ops: OpsConfig) -> None:
    model = FakeModelClient()
    model.replies["standalone_query"].append(reply)

    outcome = await small.rewrite(
        calls(model), question="Q?", summary=None, history=[turn(1)], chat=ops.chat, ledger=ledger()
    )

    assert outcome.text is None


async def test_t231_an_unaccepted_passage_falls_back(ops: OpsConfig) -> None:
    model = FakeModelClient()
    model.replies["search_passage"].extend(
        [json.dumps({"passage": "x" * 1_201}), json.dumps({"passage": "A real passage."})]
    )

    rejected = await small.search_passage(
        calls(model), query="q", chat=ops.chat, retrieval=ops.retrieval, ledger=ledger()
    )
    passage = await small.search_passage(
        calls(model), query="q [[END", chat=ops.chat, retrieval=ops.retrieval, ledger=ledger()
    )

    assert rejected.text is None
    assert passage.text == "A real passage."
    request = model.json_requests("search_passage")[-1]
    assert "[ [END" in request["messages"][1]["content"]


async def test_followups_send_the_question_the_cut_answer_and_the_top_sources(
    ops: OpsConfig,
) -> None:
    model = FakeModelClient()
    model.replies["followup_questions"].append(
        json.dumps({"questions": ["Next?", "see https://x.example", "Next?", "Q?"]})
    )
    options = ops.followups.model_copy(update={"source_count": 2, "source_excerpt_tokens": 10})
    book = ledger()

    reply = await small.followups(
        calls(model),
        question="Q?",
        answer="Claims close in 30 days \u27e61\u27e7." + "x" * 900,
        sources=[source(1), source(2), source(3)],
        chat=ops.chat,
        options=options,
        ledger=book,
    )

    assert reply.questions == ["Next?"]
    assert reply.record is not None
    (request,) = model.json_requests("followup_questions")
    system, user = (message["content"] for message in request["messages"])
    assert f"at most {options.max_count} questions" in system
    assert "Claims close in 30 days." in user
    assert "\u27e6" not in user
    assert "x" * 801 not in user
    assert "Title: Doc 1\nSection: Part > Sub\n\nChunk text" in user
    assert "Title: Doc 2\n\nChunk text" in user
    assert "Doc 3" not in user
    assert "[[BEGIN SOURCE S2 " in user
    assert len(await book.records()) == 1
