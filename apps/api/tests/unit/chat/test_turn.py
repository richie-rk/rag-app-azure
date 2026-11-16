"""The turn task over in-memory doubles: T1 to T14, the finaliser, interruptions and the replay
(T-219, T-226, T-228, the unit half of T-261/T-262/T-272/T-273; `12-chat-pipeline.md` sections 4
to 15).

The conversation store and the usage service are the real ones over `FakeContainer`s; the model,
search and Prompt Shields are the doubles of `docchat_testing.fakes.chat`, with one token per
character.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import openai
import pytest

from docchat_api.chat.calls import ChatDeploymentSettings, ModelCalls
from docchat_api.chat.generation import AnswerCall
from docchat_api.chat.screening import PromptShields
from docchat_api.chat.service import ChatService
from docchat_api.chat.tasks import TaskTracker
from docchat_api.chat.turn import ChatTurn, ReplayTurn, TurnRequest
from docchat_api.conversations.store import (
    ConversationError,
    ConversationsContainer,
    ConversationStore,
    MemoriesContainer,
    Replay,
    Started,
)
from docchat_api.usage import UsageContainer, UsageService
from docchat_core.access import AccessibleProject, PhysicalIndex, ResolvedIndex
from docchat_core.auth import Principal
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import (
    ReasoningEffort,
    SessionDocument,
    TurnDocument,
    TurnStatus,
    turn_trace_id,
)
from docchat_core.ingestion.settings import EmbeddingModelCatalog
from docchat_core.opsconfig import API_AREAS, OpsConfig, OpsConfigStore
from docchat_core.sql.repositories.retrieval import ServedChunkSet
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import (
    SHIELD_ATTACK,
    CharacterTokenizer,
    ChatFakes,
    FakeStream,
    chunk,
    search_result,
    text_stream,
    usage_of,
)
from docchat_testing.fakes.cosmos import FakeContainer

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TID = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
CORRELATION = "4bf92f3577b34da6a3ce929d0e0e4736"
EFFORTS = frozenset({"none", "low", "medium", "high"})
QUESTION = "How many days do I have to submit an expense claim?"


class PostTurn:
    def __init__(self) -> None:
        self.ensured: list[UUID] = []
        self.scheduled: list[tuple[UUID, UUID, UUID]] = []

    def ensure_post_turn_work(self, session: SessionDocument) -> None:
        self.ensured.append(session.id)

    def schedule_post_turn_work(self, oid: UUID, session_id: UUID, turn_id: UUID) -> None:
        self.scheduled.append((oid, session_id, turn_id))


class StaticOps:
    def __init__(self, config: OpsConfig) -> None:
        self.config = config

    async def snapshot(self) -> OpsConfig:
        return self.config


@dataclass
class World:
    """The doubles, the stores and the chat service of one test."""

    ops: OpsConfig
    clock: FakeClock = field(default_factory=lambda: FakeClock(START))
    fakes: ChatFakes = field(default_factory=ChatFakes)
    post_turn: PostTurn = field(default_factory=PostTurn)
    tracker: TaskTracker = field(default_factory=TaskTracker)
    served: dict[UUID, ServedChunkSet] = field(default_factory=dict)
    sql_failure: Exception | None = None
    principal: Principal = field(
        default_factory=lambda: Principal(
            oid=uuid4(), tid=TID, azp=uuid4(), client="web", roles=frozenset({"User"})
        )
    )

    def __post_init__(self) -> None:
        self.conversations = FakeContainer.of(CONVERSATIONS, clock=self.clock)
        self.memories = FakeContainer.of(MEMORIES, clock=self.clock)
        self.usage_container = FakeContainer.of(USAGE, clock=self.clock)
        ops = StaticOps(self.ops)
        tokenizer = CharacterTokenizer()
        self.store = ConversationStore(
            cast("ConversationsContainer", self.conversations),
            cast("MemoriesContainer", self.memories),
            ops=ops,
            tokenizer=tokenizer,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.usage = UsageService(
            cast("UsageContainer", self.usage_container),
            ops=ops,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.project = AccessibleProject(
            id=uuid4(),
            name="Project",
            description="",
            instructions="Answer briefly.",
            example_questions=("Who approves late claims?",),
            access="member",
        )
        self.index = ResolvedIndex(
            search_index_id=uuid4(),
            source="project_default",
            override_expires_at=None,
            physical_index=PhysicalIndex(
                id=uuid4(),
                name="docchat-0123456789abcdef0123456789abcdef-g1",
                schema_version=1,
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1024,
                query_embedding_deployment="embed-query-te3-large-v1",
            ),
        )
        settings = {"model_version": "2026-07-09", "prompt_cache": "disabled"}
        chat = ChatDeploymentSettings(
            deployment="chat",
            model="gpt-5.6-sol",
            reasoning_effort=ReasoningEffort.LOW,
            tokenizer=tokenizer,
            **settings,  # type: ignore[arg-type]
        )
        small = ChatDeploymentSettings(
            deployment="chat-small",
            model="gpt-5.6-terra",
            reasoning_effort=ReasoningEffort.NONE,
            tokenizer=tokenizer,
            **settings,  # type: ignore[arg-type]
        )
        client = self.fakes.model.client()
        self.service = ChatService(
            calls=ModelCalls(client, small=small, clock=self.clock, sleep=self.clock.sleep),
            answer=AnswerCall(client, chat=chat, clock=self.clock, sleep=self.clock.sleep),
            search_clients=self.fakes.search,
            served=self._served,
            shields=PromptShields(
                self.fakes.shields.client(), timeout_seconds=5, sleep=self.clock.sleep
            ),
            conversations=self.store,
            usage=self.usage,
            embeddings=EmbeddingModelCatalog.model_validate({}),
            tokenizers=lambda _encoding: tokenizer,
            clock=self.clock,
            tracker=self.tracker,
            post_turn=self.post_turn,
        )

    async def _served(
        self, physical: UUID, documents: Sequence[UUID]
    ) -> Mapping[UUID, ServedChunkSet]:
        if self.sql_failure is not None:
            raise self.sql_failure
        return {document: row for document, row in self.served.items() if document in documents}

    def add_sources(
        self, count: int, *, reranker: float | None = 3.0, texts: Sequence[str] = ()
    ) -> None:
        for number in range(count):
            document, chunk_set = uuid4(), uuid4()
            text = texts[number] if number < len(texts) else f"Source text {number}."
            self.fakes.search.results.append(
                search_result(
                    document_id=document,
                    chunk_set_id=chunk_set,
                    version_id=uuid4(),
                    title=f"Doc {number}",
                    text=text,
                    reranker_score=reranker,
                )
            )
            self.served[document] = ServedChunkSet(
                document_id=document,
                chunk_set_id=chunk_set,
                document_version_id=uuid4(),
                source_name=f"doc-{number}.pdf",
            )

    async def begin(
        self,
        *,
        question: str = QUESTION,
        session_id: UUID | None = None,
        client_turn_id: UUID | None = None,
    ) -> tuple[Started | Replay, Any]:
        client_turn_id = client_turn_id or uuid4()
        received_at = self.clock.now()
        start = await self.store.begin_turn(
            self.principal,
            project_id=self.project.id,
            session_id=session_id,
            client_turn_id=client_turn_id,
            question=question,
            received_at=received_at,
            trace_id=CORRELATION,
            ops=self.ops,
        )
        handle_session = (
            start.handle.session_id if isinstance(start, Started) else start.turn.session_id
        )
        handle_turn = start.handle.turn_id if isinstance(start, Started) else start.turn.id
        lease = await self.usage.acquire_stream_lease(
            self.principal.oid,
            session_id=handle_session,
            turn_id=handle_turn,
            ratelimit=self.ops.ratelimit,
            chat=self.ops.chat,
        )
        return start, lease

    async def turn(self, *, question: str = QUESTION, session_id: UUID | None = None) -> ChatTurn:
        start, lease = await self.begin(question=question, session_id=session_id)
        assert isinstance(start, Started)
        turn = ChatTurn(
            self.service,
            TurnRequest(
                principal=self.principal,
                project=self.project,
                index=self.index,
                question=question,
                received_at=self.clock.now(),
                received_monotonic=self.clock.monotonic(),
                correlation_id=CORRELATION,
                ops=self.ops,
                lease=lease,
            ),
            start,
        )
        turn.start()
        return turn

    def stored_turn(self, turn: ChatTurn) -> TurnDocument:
        item = self.conversations.get(turn.handle.partition_key, str(turn.handle.turn_id))
        assert item is not None
        return TurnDocument.from_item(item)

    def stored_trace(self, turn: ChatTurn) -> dict[str, Any]:
        item = self.conversations.get(
            turn.handle.partition_key, str(turn_trace_id(turn.handle.turn_id))
        )
        assert item is not None
        return item

    def leases(self) -> list[dict[str, Any]]:
        return [
            item
            for (_, item_id), item in self.usage_container.items.items()
            if item_id.startswith("lease:")
        ]


async def read_all(turn: ChatTurn | ReplayTurn) -> list[dict[str, Any]]:
    events = [json.loads(line) async for line in turn.writer.lines()]
    assert turn.task is not None
    await turn.task
    return events


def types(events: Sequence[Mapping[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


@pytest.fixture
async def ops(fake_sql: Any) -> OpsConfig:
    store = OpsConfigStore(fake_sql, API_AREAS, chat_small_efforts=EFFORTS)
    return await store.start()


@pytest.fixture
def world(ops: OpsConfig) -> World:
    return World(ops=ops)


# --- A complete turn ------------------------------------------------------------------------------


async def test_a_complete_turn_streams_in_order_and_is_stored_before_citations(
    world: World,
) -> None:
    world.add_sources(2)
    world.fakes.model.streams.append(
        text_stream(
            ["Claims close in 30 days [S", "1], with receipts [S2]."], usage=usage_of(900, 40)
        )
    )
    turn = await world.turn()

    events = await read_all(turn)

    assert types(events) == ["meta", "delta", "delta", "citations", "followups", "done"]
    assert events[0]["sessionCreated"] is True
    assert events[0]["replayed"] is False
    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert answer == "Claims close in 30 days \u27e61\u27e7, with receipts \u27e62\u27e7."
    assert [citation["number"] for citation in events[3]["citations"]] == [1, 2]
    stored = world.stored_turn(turn)
    assert stored.status is TurnStatus.COMPLETED
    assert stored.answer == answer
    assert [citation.source_id for citation in stored.citations] == ["S1", "S2"]
    assert stored.followups == ["What else does the policy cover?"]
    assert [call.call.value for call in stored.usage.calls] == [
        "query_embedding",
        "answer",
        "followups",
    ]
    assert events[-1]["usage"]["totalTokens"] == stored.usage.totals.total_tokens
    assert world.leases() == []
    assert world.post_turn.scheduled == [
        (world.principal.oid, turn.handle.session_id, turn.handle.turn_id)
    ]
    assert world.post_turn.ensured == [turn.handle.session_id]


async def test_the_trace_records_every_step(world: World) -> None:
    world.add_sources(1)
    turn = await world.turn()

    await read_all(turn)

    trace = world.stored_trace(turn)
    assert trace["rewriteOutcome"] == "skipped"
    assert trace["searchQuery"] == QUESTION
    retrieval = trace["retrieval"]
    assert retrieval["broadenOutcome"] == "not_needed"
    assert retrieval["kept"][0]["outcome"] == "sent"
    assert retrieval["screening"]["outcome"] == "passed"
    assert trace["modelInput"]["rulesVersion"] == "rules-v1"
    assert trace["modelInput"]["messages"][0]["role"] == "system"
    assert trace["promptBudget"]["budgetTokens"] == world.ops.chat.context_token_budget
    assert trace["deployments"]["chat"]["deployment"] == "chat"
    assert trace["timings"]["totalMs"] is not None


async def test_t226_the_history_read_uses_the_prompt_budget_and_no_turn_count(world: World) -> None:
    budgets: list[int] = []
    read = world.store.read_recent_turns

    async def recording(handle: Any, max_tokens: int) -> Any:
        budgets.append(max_tokens)
        return await read(handle, max_tokens)

    world.store.read_recent_turns = recording  # type: ignore[method-assign]
    await read_all(await world.turn())

    chat = world.ops.chat
    assert budgets == [chat.context_token_budget - chat.answer_max_completion_tokens]
    assert budgets == [28_000]


async def test_a_second_turn_rewrites_the_question_and_sends_the_history(world: World) -> None:
    world.add_sources(1)
    first = await world.turn()
    await read_all(first)
    second = await world.turn(question="And for travel?", session_id=first.handle.session_id)

    await read_all(second)

    trace = world.stored_trace(second)
    assert trace["rewriteOutcome"] == "rewritten"
    assert trace["standaloneQuery"] == "standalone search query"
    assert world.fakes.search.requests[-1]["search_text"] == "standalone search query"
    messages = world.fakes.model.answer_requests()[-1]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert messages[1]["content"] == QUESTION


# --- T-228: the suggested-question flag -----------------------------------------------------------


async def test_t228_a_repeated_follow_up_is_a_suggested_question(world: World) -> None:
    world.add_sources(1)
    world.fakes.model.replies["followup_questions"].append(
        json.dumps({"questions": ["What is the receipt limit?"]})
    )
    first = await world.turn()
    await read_all(first)

    repeated = await world.turn(
        question="  what is the RECEIPT limit? ", session_id=first.handle.session_id
    )
    await read_all(repeated)
    reordered = await world.turn(
        question="Is the receipt limit what?", session_id=first.handle.session_id
    )
    await read_all(reordered)

    assert world.stored_turn(repeated).memory_extraction.reason == "suggested_question"
    assert world.stored_turn(reordered).memory_extraction.reason is None


async def test_t228_an_example_question_is_suggested_and_nothing_else_isnt(world: World) -> None:
    example = await world.turn(question="Who approves late claims?")
    await read_all(example)
    own = await world.turn(question="Something else entirely?")
    await read_all(own)

    assert world.stored_turn(example).memory_extraction.reason == "suggested_question"
    assert world.stored_turn(own).memory_extraction.reason is None


async def test_t228_conversation_unavailable_passes_false(world: World) -> None:
    async def failing(handle: Any, max_tokens: int) -> Any:
        raise ConversationError("conversation_unavailable")

    world.store.read_recent_turns = failing  # type: ignore[method-assign]
    turn = await world.turn(question="Who approves late claims?")

    events = await read_all(turn)

    assert types(events) == ["meta", "error"]
    assert events[-1]["code"] == "conversation_unavailable"
    stored = world.stored_turn(turn)
    assert stored.status is TurnStatus.FAILED
    assert stored.error is not None
    assert stored.error.code == "conversation_unavailable"
    assert stored.memory_extraction.reason is None
    assert world.leases() == []
    assert world.post_turn.scheduled == []


# --- Failures before the answer call --------------------------------------------------------------


async def test_an_embedding_failure_is_retrieval_unavailable(world: World) -> None:
    world.fakes.model.embeddings_script.extend(
        [openai.APIConnectionError(request=cast("Any", None))] * 2
    )
    turn = await world.turn()

    events = await read_all(turn)

    assert events[-1]["code"] == "retrieval_unavailable"
    assert world.fakes.model.answer_requests() == []
    assert world.stored_turn(turn).finish_reason is None


async def test_an_unknown_schema_version_ends_before_any_call(world: World) -> None:
    world.index = ResolvedIndex(
        search_index_id=world.index.search_index_id,
        source="project_default",
        override_expires_at=None,
        physical_index=PhysicalIndex(
            id=uuid4(),
            name="docchat-0123456789abcdef0123456789abcdef-g2",
            schema_version=99,
            embedding_model="text-embedding-3-large",
            embedding_dimensions=1024,
            query_embedding_deployment="embed-query-te3-large-v1",
        ),
    )
    turn = await world.turn()

    events = await read_all(turn)

    assert events[-1]["code"] == "retrieval_unavailable"
    assert world.fakes.model.embedding_requests() == []
    assert world.fakes.search.requests == []


async def test_a_failed_filter_after_the_first_pass_is_retrieval_unavailable(world: World) -> None:
    world.add_sources(1)
    world.sql_failure = RuntimeError("sql")
    turn = await world.turn()

    events = await read_all(turn)

    assert events[-1]["code"] == "retrieval_unavailable"


async def test_a_screening_failure_ends_before_the_answer_call(world: World) -> None:
    world.add_sources(1)
    world.fakes.shields.queued.extend([httpx.Response(503), httpx.Response(503)])
    turn = await world.turn()

    events = await read_all(turn)

    assert events[-1]["code"] == "source_screening_unavailable"
    assert world.fakes.model.answer_requests() == []
    trace = world.stored_trace(turn)
    assert trace["retrieval"]["screening"]["outcome"] == "unavailable"
    assert world.stored_turn(turn).usage.billable.shield_calls == 2


async def test_a_flagged_source_is_dropped_and_reported(world: World, captured: Any) -> None:
    world.add_sources(
        2, texts=[f"Poisoned {SHIELD_ATTACK} https://evil.example/x", "Clean https://ok.example/y"]
    )
    world.fakes.model.streams.append(text_stream(["See [S1] and [S2]."], usage=usage_of(10, 5)))
    turn = await world.turn()

    events = await read_all(turn)

    sent = world.fakes.model.answer_requests()[0]["messages"][-2]["content"]
    assert "Poisoned" not in sent
    assert events[types(events).index("citations")]["allowedLinks"] == ["https://ok.example/y"]
    assert [
        citation["number"] for citation in events[types(events).index("citations")]["citations"]
    ] == [1]
    (event,) = captured.events("source_screening_flagged")
    assert event["indexId"] == str(world.index.search_index_id)
    kept = world.stored_trace(turn)["retrieval"]["kept"]
    assert [(entry["outcome"], entry["sourceId"]) for entry in kept] == [
        ("flagged", None),
        ("sent", "S1"),
    ]


async def test_low_confidence_broadens_and_replaces_the_set(world: World) -> None:
    world.add_sources(1, reranker=1.0)
    turn = await world.turn()

    events = await read_all(turn)

    assert types(events)[:2] == ["meta", "status"]
    retrieval = world.stored_trace(turn)["retrieval"]
    assert (retrieval["broadenOutcome"], retrieval["broadened"]) == ("replaced", True)
    assert len(world.fakes.search.requests[-1]["vector_queries"]) == 2
    assert world.stored_turn(turn).usage.billable.semantic_queries == 2


async def test_a_partial_response_never_broadens(world: World) -> None:
    world.add_sources(1, reranker=None)
    turn = await world.turn()

    events = await read_all(turn)

    assert "status" not in types(events)
    assert world.stored_trace(turn)["retrieval"]["broadenOutcome"] == "skipped_partial"


# --- Answer endings -------------------------------------------------------------------------------


async def test_an_answer_failure_keeps_the_released_text(world: World) -> None:
    world.add_sources(1)
    world.fakes.model.streams.append(
        text_stream(["Partial [S1] text"], finish_reason="content_filter")
    )
    turn = await world.turn()

    events = await read_all(turn)

    assert types(events) == ["meta", "delta", "error"]
    assert events[-1]["code"] == "answer_filtered"
    stored = world.stored_turn(turn)
    assert (stored.status, stored.finish_reason) == (TurnStatus.FAILED, "content_filter")
    assert stored.answer == "Partial \u27e61\u27e7 text"
    assert len(stored.citations) == 1
    assert world.post_turn.scheduled == []


async def test_a_length_finish_with_text_completes_truncated(world: World) -> None:
    world.add_sources(1)
    world.fakes.model.streams.append(text_stream(["Some text"], finish_reason="length"))
    turn = await world.turn()

    events = await read_all(turn)

    assert events[-1] == {**events[-1], "type": "done", "truncated": True}
    assert world.stored_turn(turn).answer_truncated


async def test_t261_a_failed_save_ends_with_turn_save_failed_and_no_citations(world: World) -> None:
    world.add_sources(1)

    async def failing(handle: Any, outcome: Any) -> Any:
        raise ConversationError("turn_save_failed")

    world.store.finalize_turn = failing  # type: ignore[method-assign]
    turn = await world.turn()

    events = await read_all(turn)

    assert "citations" not in types(events)
    assert types(events)[-1] == "error"
    assert events[-1]["code"] == "turn_save_failed"
    assert world.leases() == []
    assert world.post_turn.scheduled == []


# --- T-219 and interruptions ----------------------------------------------------------------------


async def test_t272_a_disconnect_during_generation_saves_cancelled(world: World) -> None:
    world.add_sources(1)
    stream = FakeStream([chunk("Partial "), chunk("[S1] answer")])
    stream.gate = asyncio.Event()
    world.fakes.model.streams.append(stream)
    turn = await world.turn()
    lines = turn.writer.lines()
    seen = [json.loads(await anext(lines)) for _ in range(3)]

    await lines.aclose()
    assert turn.task is not None
    await turn.task

    assert types(seen) == ["meta", "delta", "delta"]
    assert stream.closed
    stored = world.stored_turn(turn)
    assert (stored.status, stored.finish_reason) == (TurnStatus.CANCELLED, "cancelled")
    assert stored.answer == "Partial \u27e61\u27e7 answer"
    answer_call = next(call for call in stored.usage.calls if call.call.value == "answer")
    assert answer_call.estimated
    assert answer_call.outcome.value == "cancelled"
    assert world.leases() == []
    assert world.post_turn.scheduled == []


async def test_the_deadline_before_finalisation_is_turn_timeout(world: World) -> None:
    world.add_sources(1)
    stream = FakeStream([chunk("Slow ")])
    stream.gate = asyncio.Event()
    world.fakes.model.streams.append(stream)
    turn = await world.turn()
    lines = turn.writer.lines()
    seen = [json.loads(await anext(lines)) for _ in range(2)]

    turn.deadline_passed()
    rest = [json.loads(line) async for line in lines]
    assert turn.task is not None
    await turn.task

    assert types(seen + rest) == ["meta", "delta", "error"]
    assert rest[-1]["code"] == "turn_timeout"
    stored = world.stored_turn(turn)
    assert stored.error is not None
    assert stored.error.code == "turn_timeout"
    assert stored.finish_reason == "error"
    answer_call = next(call for call in stored.usage.calls if call.call.value == "answer")
    assert answer_call.outcome.value == "timed_out"


async def test_t273_the_drain_before_finalisation_is_service_restarting(world: World) -> None:
    world.add_sources(1)
    stream = FakeStream([chunk("Draining ")])
    stream.gate = asyncio.Event()
    world.fakes.model.streams.append(stream)
    turn = await world.turn()
    lines = turn.writer.lines()
    [json.loads(await anext(lines)) for _ in range(2)]

    world.tracker.drain_point()
    rest = [json.loads(line) async for line in lines]
    assert turn.task is not None
    await turn.task

    assert rest[-1]["code"] == "service_restarting"
    assert rest[-1]["retryAfterSeconds"] == 5
    stored = world.stored_turn(turn)
    assert stored.error is not None
    assert stored.error.code == "service_restarting"


async def test_t273_a_finalised_turn_at_the_drain_point_drops_its_follow_ups_and_ends_done(
    world: World,
) -> None:
    world.add_sources(1)
    gate = asyncio.Event()
    model = world.fakes.model
    original = model._create

    async def slow_followups(**arguments: Any) -> Any:
        if not arguments.get("stream"):
            await gate.wait()
        return await original(**arguments)

    model.chat.completions.create = slow_followups
    turn = await world.turn()
    lines = turn.writer.lines()
    seen: list[dict[str, Any]] = []
    while not seen or seen[-1]["type"] != "citations":
        seen.append(json.loads(await anext(lines)))

    world.tracker.drain_point()
    rest = [json.loads(line) async for line in lines]
    assert turn.task is not None
    await turn.task

    assert types(rest) == ["done"]
    assert world.stored_turn(turn).status is TurnStatus.COMPLETED
    assert world.stored_turn(turn).followups == []


async def test_t219_a_disconnect_after_finalisation_keeps_the_turn_completed(world: World) -> None:
    world.add_sources(1)
    turn = await world.turn()
    lines = turn.writer.lines()
    while json.loads(await anext(lines))["type"] != "citations":
        pass

    await lines.aclose()
    assert turn.task is not None
    await turn.task

    assert world.stored_turn(turn).status is TurnStatus.COMPLETED
    assert world.leases() == []
    assert len(world.post_turn.scheduled) == 1


# --- The replay -----------------------------------------------------------------------------------


async def test_t224_a_completed_duplicate_replays_without_calls_or_writes(world: World) -> None:
    world.add_sources(1)
    client_turn_id = uuid4()
    start, lease = await world.begin(client_turn_id=client_turn_id)
    assert isinstance(start, Started)
    first = ChatTurn(
        world.service,
        TurnRequest(
            principal=world.principal,
            project=world.project,
            index=world.index,
            question=QUESTION,
            received_at=world.clock.now(),
            received_monotonic=world.clock.monotonic(),
            correlation_id=CORRELATION,
            ops=world.ops,
            lease=lease,
        ),
        start,
    )
    first.start()
    original = await read_all(first)
    requests = len(world.fakes.model.requests)
    writes = len(world.conversations.calls)

    duplicate, replay_lease = await world.begin(client_turn_id=client_turn_id)
    assert isinstance(duplicate, Replay)
    replay = ReplayTurn(
        world.service,
        turn=duplicate.turn,
        lease=replay_lease,
        correlation_id="1" * 32,
        received_monotonic=world.clock.monotonic(),
    )
    replay.start()
    replayed = await read_all(replay)

    assert types(replayed) == ["meta", "delta", "citations", "followups", "done"]
    assert types(original)[-3:] == ["citations", "followups", "done"]
    assert replayed[0]["replayed"] is True
    assert replayed[0]["sessionCreated"] is False
    assert replayed[0]["correlationId"] == "1" * 32
    assert "".join(e["text"] for e in replayed if e["type"] == "delta") == "".join(
        e["text"] for e in original if e["type"] == "delta"
    )
    assert (
        replayed[types(replayed).index("citations")] == original[types(original).index("citations")]
    )
    assert len(world.fakes.model.requests) == requests
    assert [call.method for call in world.conversations.calls[writes:]] == ["read_item"]
    assert world.leases() == []
