"""The conversation store's turn path against the Cosmos DB emulator (T-291 to T-297, T-299,
T-300 and this path's T-301 entries; `13-conversations-and-memory.md` sections 2 and 3).

Every test reaches the containers through `CosmosAudit` and runs inside the `createChatTurn`
context, so each call between `begin_turn` and the terminal status must name the full
`[oid, sessionId]` key (the Cosmos DB data-access audit, `84-testing.md` section 2.11); a fresh
user per test keeps the tests apart. Deadlines move with a `FakeClock`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceResponseTimeoutError
from azure.cosmos.aio import ContainerProxy, DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from docchat_api.conversations.store import (
    ConversationError,
    ConversationsContainer,
    ConversationStore,
    MemoriesContainer,
    Replay,
    Started,
    TurnHandle,
    TurnOutcome,
)
from docchat_api.tokens import TiktokenTokenizer
from docchat_core.auth import Principal
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES
from docchat_core.cosmos.conversations import (
    FinishReason,
    RewriteOutcome,
    TurnDocument,
    TurnError,
    TurnStatus,
    TurnTraceData,
    new_session_id,
    turn_id,
    turn_trace_id,
)
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.models import format_utc
from docchat_core.opsconfig import ChatOps, ConversationsOps, MemoryOps, OpsConfig, efforts_context
from docchat_testing.builders.canaries import canary
from docchat_testing.clock import FakeClock
from docchat_testing.data_audit import CosmosAudit
from docchat_testing.fakes.cosmos import not_found

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
PROJECT = UUID("3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74")
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACE = TurnTraceData(rewrite_outcome=RewriteOutcome.SKIPPED)
EFFORTS = efforts_context({"none", "low", "medium"})
"""The chat-small model's efforts, which the memory area is validated against."""


class StaticOps:
    def __init__(self) -> None:
        self.config = OpsConfig.model_validate(
            {
                "chat": ChatOps.model_validate(
                    {
                        "chat.context_token_budget": 40_000,
                        "chat.answer_max_completion_tokens": 12_000,
                        "chat.small_call_max_completion_tokens": 800,
                        "chat.rewrite_history_turns": 4,
                        "chat.rewrite_timeout_seconds": 8,
                        "chat.max_turn_seconds": 180,
                    }
                ),
                "conversations": ConversationsOps.model_validate(
                    {"conversations.max_turns_per_session": 500}
                ),
                "memory": MemoryOps.model_validate(
                    {
                        "memory.enabled": True,
                        "memory.max_facts": 50,
                        "memory.max_tokens": 1_000,
                        "memory.prompt_max_tokens": 1_300,
                        "memory.max_operations_per_turn": 3,
                        "memory.max_completion_tokens": 1_200,
                        "memory.reasoning_effort": "low",
                        "memory.timeout_seconds": 20,
                        "memory.lease_seconds": 60,
                        "memory.max_attempts": 3,
                        "memory.retry_window_hours": 24,
                    },
                    context=EFFORTS,
                ),
            },
            context=EFFORTS,
        )

    async def snapshot(self) -> OpsConfig:
        return self.config


class Intercepted:
    """A container client with test hooks on some calls; every other call passes through."""

    def __init__(self, inner: ContainerProxy) -> None:
        self._inner = inner
        self.hidden_items: set[str] = set()
        self.lose_batch_responses = 0
        self.pages_fetched = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def read_item(self, item: str, partition_key: Any, **kwargs: Any) -> Any:
        if item in self.hidden_items:
            raise not_found()
        return await self._inner.read_item(item, partition_key, **kwargs)

    async def execute_item_batch(self, operations: Any, partition_key: Any, **kwargs: Any) -> Any:
        result = await self._inner.execute_item_batch(operations, partition_key, **kwargs)
        if self.lose_batch_responses:
            self.lose_batch_responses -= 1
            raise ServiceResponseTimeoutError("the response was lost after the commit")
        return result

    def query_items(self, query: str, **kwargs: Any) -> Any:
        pager = self._inner.query_items(query, **kwargs)
        counter = self

        class Counted:
            def by_page(self, continuation_token: str | None = None) -> Any:
                pages = pager.by_page(continuation_token)

                class Pages:
                    def __aiter__(self) -> Any:
                        return self

                    async def __anext__(self) -> Any:
                        page = await anext(pages)
                        counter.pages_fetched += 1
                        return page

                return Pages()

        return Counted()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def audit() -> Iterator[CosmosAudit]:
    audit = CosmosAudit()
    with audit.operation("createChatTurn"):
        yield audit
    audit.check()
    conversations = [call for call in audit.calls if call.container == CONVERSATIONS.name]
    assert conversations
    assert all(call.full_key for call in conversations)


@pytest.fixture
def raw(cosmos_database: DatabaseProxy) -> ContainerProxy:
    """The `conversations` container without the audit, for a test's own seeding and reads."""
    return cosmos_database.get_container_client(CONVERSATIONS.name)


@pytest.fixture
def conversations(raw: ContainerProxy, audit: CosmosAudit) -> Intercepted:
    return Intercepted(audit(raw))


@pytest.fixture
def tokenizer() -> TiktokenTokenizer:
    # Loading the encoding reads its file; it happens here, outside the test's event loop.
    tokenizer = TiktokenTokenizer(TokenEncoding.O200K_BASE)
    tokenizer.encode("warm")
    return tokenizer


@pytest.fixture
def store(
    conversations: Intercepted,
    cosmos_database: DatabaseProxy,
    audit: CosmosAudit,
    tokenizer: TiktokenTokenizer,
    clock: FakeClock,
) -> ConversationStore:
    return ConversationStore(
        cast("ConversationsContainer", conversations),
        cast("MemoriesContainer", audit(cosmos_database.get_container_client(MEMORIES.name))),
        ops=StaticOps(),
        tokenizer=tokenizer,
        clock=clock,
    )


@pytest.fixture
def principal() -> Principal:
    return Principal(oid=uuid4(), tid=uuid4(), azp=uuid4(), client="web", roles=frozenset({"User"}))


async def _begin(
    store: ConversationStore,
    principal: Principal,
    *,
    session_id: UUID | None = None,
    client_turn_id: UUID | None = None,
    question: str = "How many days do I have to submit an expense claim?",
    received_at: datetime = START,
) -> Started | Replay:
    return await store.begin_turn(
        principal,
        project_id=PROJECT,
        session_id=session_id,
        client_turn_id=client_turn_id or uuid4(),
        question=question,
        received_at=received_at,
        trace_id=TRACE_ID,
    )


async def _started(store: ConversationStore, principal: Principal, **arguments: Any) -> Started:
    result = await _begin(store, principal, **arguments)
    assert isinstance(result, Started)
    return result


def _completed(answer: str = "Thirty days ⟦1⟧.") -> TurnOutcome:
    return TurnOutcome(
        status=TurnStatus.COMPLETED, answer=answer, finish_reason=FinishReason.STOP, trace=TRACE
    )


async def _read(raw: ContainerProxy, handle_or_key: TurnHandle | list[str], item: UUID) -> Any:
    key = handle_or_key.partition_key if isinstance(handle_or_key, TurnHandle) else handle_or_key
    return await raw.read_item(str(item), key, timeout=10)


async def test_t291_a_first_turn_creates_the_session_and_the_turn_in_one_batch(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    started = await _started(store, principal)

    session = await _read(raw, started.handle, started.handle.session_id)
    turn = await _read(raw, started.handle, started.handle.turn_id)
    assert (session["turnCount"], session["activeTurn"]["turnId"]) == (1, str(turn["id"]))
    assert (turn["seq"], turn["status"]) == (1, "streaming")


async def test_t291_a_forced_failure_of_the_turn_create_leaves_no_session(
    store: ConversationStore, raw: ContainerProxy, conversations: Intercepted, principal: Principal
) -> None:
    client = uuid4()
    session_id = new_session_id(principal.oid, client)
    blocker = turn_id(session_id, client)
    await raw.create_item(
        {"id": str(blocker), "userId": str(principal.oid), "sessionId": str(session_id)},
        timeout=10,
    )
    conversations.hidden_items.add(str(blocker))

    with pytest.raises(ConversationError) as caught:
        await _begin(store, principal, client_turn_id=client)

    assert caught.value.code == "dependency_unavailable"
    with pytest.raises(CosmosResourceNotFoundError):
        await _read(raw, [str(principal.oid), str(session_id)], session_id)


async def test_t292_20_sequential_turns_get_seq_1_to_20(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    first = await _started(store, principal)
    session_id = first.handle.session_id
    endings = [
        _completed(),
        TurnOutcome(
            status=TurnStatus.FAILED,
            error=TurnError(code="retrieval_unavailable", retryable=True),
            trace=TRACE,
        ),
        TurnOutcome(status=TurnStatus.CANCELLED, finish_reason=FinishReason.CANCELLED, trace=TRACE),
    ]
    handles = [first.handle]
    await store.finalize_turn(first.handle, endings[0])
    for index in range(1, 20):
        started = await _started(store, principal, session_id=session_id)
        handles.append(started.handle)
        await store.finalize_turn(started.handle, endings[index % 3])

    assert [handle.seq for handle in handles] == list(range(1, 21))
    session = await _read(raw, first.handle, session_id)
    assert session["turnCount"] == 20
    statuses = [(await _read(raw, handle, handle.turn_id))["status"] for handle in handles]
    assert statuses[1:4] == ["failed", "cancelled", "completed"]


async def test_t293_two_starts_in_one_session_let_exactly_one_through(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    first = await _started(store, principal)
    await store.finalize_turn(first.handle, _completed())

    results = await asyncio.gather(
        _begin(store, principal, session_id=first.handle.session_id),
        _begin(store, principal, session_id=first.handle.session_id),
        return_exceptions=True,
    )

    started = [result for result in results if isinstance(result, Started)]
    refused = [result for result in results if isinstance(result, ConversationError)]
    assert len(started) == 1
    assert [error.code for error in refused] == ["turn_in_progress"]
    assert started[0].handle.seq == 2
    session = await _read(raw, first.handle, first.handle.session_id)
    assert session["turnCount"] == 2


async def test_t294_ten_identical_first_turns_leave_one_session_and_one_turn(
    store: ConversationStore, principal: Principal
) -> None:
    client = uuid4()

    results = await asyncio.gather(
        *(_begin(store, principal, client_turn_id=client) for _ in range(10)),
        return_exceptions=True,
    )

    started = [result for result in results if isinstance(result, Started)]
    refused = [result for result in results if isinstance(result, ConversationError)]
    assert len(started) == 1
    assert {error.code for error in refused} == {"turn_in_progress"}
    assert len(refused) == 9
    await store.finalize_turn(started[0].handle, _completed())
    replay = await _begin(store, principal, client_turn_id=client)
    assert isinstance(replay, Replay)
    assert replay.turn.answer == "Thirty days ⟦1⟧."


async def test_t296_a_turn_left_streaming_past_its_deadline_is_abandoned_by_the_next_start(
    store: ConversationStore, raw: ContainerProxy, principal: Principal, clock: FakeClock
) -> None:
    stale = await _started(store, principal)
    clock.advance(211)

    taken = await _started(
        store, principal, session_id=stale.handle.session_id, received_at=clock.now()
    )

    assert taken.handle.seq == 2
    old = await _read(raw, stale.handle, stale.handle.turn_id)
    assert old["status"] == "failed"
    assert old["error"] == {"code": "turn_abandoned", "retryable": True}


async def test_t297_a_summary_lease_written_meanwhile_is_retried_and_counted_once(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    started = await _started(store, principal)
    lease = format_utc(START + timedelta(minutes=2))
    await raw.patch_item(
        str(started.handle.session_id),
        started.handle.partition_key,
        [{"op": "set", "path": "/summaryJob/leaseExpiresAt", "value": lease}],
        timeout=10,
    )

    result = await store.finalize_turn(started.handle, _completed())

    session = await _read(raw, started.handle, started.handle.session_id)
    turn = await _read(raw, started.handle, started.handle.turn_id)
    assert result.saved
    assert session["history"]["unsummarisedTokens"] == turn["historyTokens"]
    assert session["history"]["unsummarisedTurns"] == 1
    assert session["summaryJob"]["leaseExpiresAt"] == lease


async def test_t297_a_batch_whose_response_was_lost_returns_saved(
    store: ConversationStore, raw: ContainerProxy, conversations: Intercepted, principal: Principal
) -> None:
    started = await _started(store, principal)
    conversations.lose_batch_responses = 1

    result = await store.finalize_turn(started.handle, _completed())

    turn = await _read(raw, started.handle, started.handle.turn_id)
    assert result.saved
    assert result.turn_etag == turn["_etag"]
    assert turn["status"] == "completed"
    await _read(raw, started.handle, turn_trace_id(started.handle.turn_id))


async def test_t297_a_turn_taken_over_before_finalisation_is_superseded(
    store: ConversationStore, raw: ContainerProxy, principal: Principal, clock: FakeClock
) -> None:
    stale = await _started(store, principal)
    clock.advance(211)
    await _started(store, principal, session_id=stale.handle.session_id, received_at=clock.now())

    result = await store.finalize_turn(stale.handle, _completed())

    assert not result.saved
    old = await _read(raw, stale.handle, stale.handle.turn_id)
    assert old["error"]["code"] == "turn_abandoned"


async def _seed_completed(
    raw: ContainerProxy, handle: TurnHandle, seq: int, history_tokens: int, answer: str
) -> None:
    client = uuid4()
    turn = TurnDocument(
        id=turn_id(handle.session_id, client),
        user_id=handle.oid,
        session_id=handle.session_id,
        seq=seq,
        client_turn_id=client,
        project_id=PROJECT,
        status=TurnStatus.COMPLETED,
        question=canary("question"),
        answer=answer,
        finish_reason=FinishReason.STOP,
        followups=[f"Follow-up {seq}?"],
        history_tokens=history_tokens,
        trace_id=TRACE_ID,
        deadline_at=START,
        created_at=START,
        ended_at=START,
        updated_at=START,
    )
    await raw.create_item(turn.to_item(), timeout=10)


async def test_t299_12_turns_under_the_budget_all_come_oldest_first(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    handle = (await _started(store, principal)).handle
    for seq in range(101, 113):
        await _seed_completed(raw, handle, seq, 200, f"Answer {seq} ⟦1⟧.")

    turns = await store.read_recent_turns(handle, 3_000)

    assert [turn.seq for turn in turns] == list(range(101, 113))
    assert turns[0].answer == "Answer 101."
    assert turns[0].followups == ("Follow-up 101?",)


async def test_t299_the_second_page_is_never_requested(
    store: ConversationStore, raw: ContainerProxy, conversations: Intercepted, principal: Principal
) -> None:
    handle = (await _started(store, principal)).handle
    for seq in range(101, 131):
        await _seed_completed(raw, handle, seq, 300, f"Answer {seq}.")

    turns = await store.read_recent_turns(handle, 1_000)

    assert [turn.seq for turn in turns] == [127, 128, 129, 130]
    assert conversations.pages_fetched == 1


async def test_t300_a_large_turn_finalises_with_counting_off_the_loop(
    store: ConversationStore, raw: ContainerProxy, principal: Principal
) -> None:
    started = await _started(store, principal, question="q" * 4_000)

    result = await store.finalize_turn(started.handle, _completed(answer="a " * 50_000))

    turn = await _read(raw, started.handle, started.handle.turn_id)
    assert result.saved
    assert turn["historyTokens"] > 0
