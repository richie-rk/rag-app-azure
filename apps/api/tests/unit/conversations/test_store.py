from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.conversations.store import (
    ConversationError,
    ConversationStore,
    FinalizeResult,
    Replay,
    Started,
    TurnHandle,
    TurnOutcome,
    fit_memory_lines,
    new_session_document,
    ordered_facts,
    problem_on_failure,
    session_title,
)
from docchat_api.conversations.telemetry import Charges
from docchat_api.http.problems import DependencyUnavailable, problem_body
from docchat_core.auth import Principal
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES
from docchat_core.cosmos.conversations import (
    CallOutcome,
    ExtractionState,
    Feedback,
    FeedbackRating,
    FinishReason,
    ModelCall,
    ModelCallUsage,
    ModelInput,
    PromptMessage,
    ReasoningEffort,
    RewriteOutcome,
    SessionDocument,
    TurnDocument,
    TurnError,
    TurnStatus,
    TurnTraceData,
    TurnUsage,
    new_session_id,
    turn_id,
    turn_trace_id,
)
from docchat_core.cosmos.memories import Fact, FactSource, FactSourceKind, MemoryDocument
from docchat_core.memory import FactCategory
from docchat_core.models import format_utc
from docchat_core.opsconfig import API_AREAS, OpsConfig, OpsConfigStore, RowReader
from docchat_testing.builders.canaries import canary
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.cosmos import FakeContainer, batch_error

USER = UUID("0a11ce00-0000-4000-8000-000000000001")
PROJECT = UUID("3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74")
OTHER_PROJECT = UUID("9a1b2c3d-4e5f-4061-8a7b-8c9d0e1f2a3b")
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
DEADLINE = START + timedelta(seconds=210)
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
EFFORTS = frozenset({"none", "low", "medium"})
PRINCIPAL = Principal(oid=USER, tid=uuid4(), azp=uuid4(), client="web", roles=frozenset({"User"}))
EMPTY_TRACE = TurnTraceData(rewrite_outcome=RewriteOutcome.SKIPPED)


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


class FakeRows(Protocol):
    rows: dict[str, str]


class Ops:
    """An ops source whose snapshot a test replaces."""

    def __init__(self, config: OpsConfig) -> None:
        self.config = config

    async def snapshot(self) -> OpsConfig:
        return self.config


class WordTokenizer:
    """One token per whitespace-separated word; records the thread of each count."""

    def __init__(self) -> None:
        self.threads: list[int] = []

    def encode(self, text: str) -> Sequence[int]:
        self.threads.append(threading.get_ident())
        return [0] * len(text.split())


async def load_ops(fake_sql: FakeRows, **values: str) -> OpsConfig:
    """The seeded ops config with `values` (stored JSON text by key) changed."""
    fake_sql.rows.update(values)
    store = OpsConfigStore(cast("RowReader", fake_sql), API_AREAS, chat_small_efforts=EFFORTS)
    return await store.start()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def conversations(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(CONVERSATIONS, clock=clock)


@pytest.fixture
def memories(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(MEMORIES, clock=clock)


@pytest.fixture
async def ops(fake_sql: FakeRows) -> Ops:
    return Ops(await load_ops(fake_sql))


@pytest.fixture
def tokenizer() -> WordTokenizer:
    return WordTokenizer()


@pytest.fixture
def store(
    conversations: FakeContainer,
    memories: FakeContainer,
    ops: Ops,
    tokenizer: WordTokenizer,
    clock: FakeClock,
) -> ConversationStore:
    return ConversationStore(
        conversations, memories, ops=ops, tokenizer=tokenizer, clock=clock, sleep=clock.sleep
    )


async def begin(
    store: ConversationStore,
    *,
    session_id: UUID | None = None,
    client_turn_id: UUID | None = None,
    question: str = "How many days do I have to submit an expense claim?",
    project_id: UUID = PROJECT,
) -> Started | Replay:
    return await store.begin_turn(
        PRINCIPAL,
        project_id=project_id,
        session_id=session_id,
        client_turn_id=client_turn_id or uuid4(),
        question=question,
        received_at=START,
        trace_id=TRACE_ID,
    )


async def started(store: ConversationStore, **arguments: Any) -> Started:
    result = await begin(store, **arguments)
    assert isinstance(result, Started)
    return result


def pk(session_id: UUID) -> list[str]:
    return [str(USER), str(session_id)]


def seed_session(conversations: FakeContainer, **overrides: Any) -> SessionDocument:
    session_id = overrides.pop("session_id", uuid4())
    values: dict[str, Any] = {
        "id": session_id,
        "user_id": USER,
        "session_id": session_id,
        "project_id": PROJECT,
        "title": "Expense claims",
        "created_at": START - timedelta(hours=1),
        "updated_at": START - timedelta(hours=1),
        "last_turn_at": START - timedelta(hours=1),
        "turn_count": 1,
    }
    session = SessionDocument.model_validate(values | overrides)
    conversations.put(session.to_item())
    return session


def seed_turn(
    conversations: FakeContainer,
    session_id: UUID,
    client_turn_id: UUID | None = None,
    **overrides: Any,
) -> TurnDocument:
    client = client_turn_id or uuid4()
    values: dict[str, Any] = {
        "id": turn_id(session_id, client),
        "user_id": USER,
        "session_id": session_id,
        "seq": 1,
        "client_turn_id": client,
        "project_id": PROJECT,
        "status": TurnStatus.COMPLETED,
        "question": "How many days do I have to submit an expense claim?",
        "answer": "Thirty days ⟦1⟧.",
        "finish_reason": FinishReason.STOP,
        "history_tokens": 12,
        "trace_id": TRACE_ID,
        "deadline_at": START - timedelta(minutes=50),
        "created_at": START - timedelta(hours=1),
        "ended_at": START - timedelta(minutes=59),
        "updated_at": START - timedelta(minutes=59),
    }
    turn = TurnDocument.model_validate(values | overrides)
    conversations.put(turn.to_item())
    return turn


def span(captured: Captured, name: str) -> dict[str, Any]:
    (found,) = captured.finished_spans(name)
    return dict(found.attributes or {})


def usage_record(call: ModelCall = ModelCall.FOLLOWUPS) -> ModelCallUsage:
    return ModelCallUsage(
        call=call,
        deployment="chat-small",
        model="gpt-5.6-terra",
        prompt_tokens=900,
        completion_tokens=60,
        reasoning_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        total_tokens=960,
        estimated=False,
        estimated_cost_micros=None,
        duration_ms=700,
        outcome=CallOutcome.OK,
    )


# --- T-307: session titles ----------------------------------------------------------------------


def test_t307_whitespace_and_line_breaks_collapse() -> None:
    assert session_title("  How   many\r\n\n days?\t\N{NO-BREAK SPACE}") == "How many days?"


def test_t307_cc_cf_co_and_cs_characters_go() -> None:
    question = "Ex\N{ZERO WIDTH SPACE}pense\u0007 claim\N{WORD JOINER}s\ud800"

    assert session_title(question) == "Expense claims"


def test_t307_80_code_points_stay_whole() -> None:
    question = "x" * 80

    assert session_title(question) == question


def test_t307_a_long_question_is_cut_at_a_space_at_position_40_or_later() -> None:
    words = "Which travel expenses can I claim back after a conference trip abroad"
    question = (words + " ") * 3  # about 210 characters

    title = session_title(question)

    assert title.endswith("…")
    assert len(title) <= 80
    kept = title[:-1]
    assert question.startswith(kept)
    assert question[len(kept)] == " "
    assert len(kept) + 1 >= 40


def test_t307_without_such_a_space_the_cut_is_at_79() -> None:
    assert session_title("y" * 200) == "y" * 79 + "…"
    early_space = "a" * 30 + " " + "b" * 169

    assert session_title(early_space) == early_space[:79] + "…"


def test_t307_trailing_punctuation_before_the_cut_goes() -> None:
    question = "w" * 60 + " " + "x" * 10 + ".,;:-" + " " + "z" * 100  # a space at 77

    assert session_title(question) == "w" * 60 + " " + "x" * 10 + "…"


def test_t307_emoji_and_right_to_left_text_are_kept() -> None:
    question = "שלום, מה זמן ההגשה? 👋🏽 مرحبا"

    assert session_title(question) == unicodedata.normalize("NFC", question)


def test_t307_nothing_left_is_an_ellipsis() -> None:
    assert session_title(" \N{ZERO WIDTH SPACE}\n") == "…"


def test_a_new_session_document() -> None:
    session_id, turn = uuid4(), uuid4()

    document = new_session_document(
        USER, session_id, PROJECT, "  First   question ", START, turn_id=turn, deadline_at=DEADLINE
    )

    assert document["id"] == document["sessionId"] == str(session_id)
    assert document["title"] == "First question"
    assert document["turnCount"] == 1
    assert document["lastTurnAt"] == format_utc(START)
    assert document["activeTurn"] == {"turnId": str(turn), "deadlineAt": format_utc(DEADLINE)}
    assert document["history"] == {"unsummarisedTokens": 0, "unsummarisedTurns": 0}
    assert document["summary"]["text"] is None
    assert document["summaryJob"]["consecutiveFailures"] == 0
    assert document["schemaVersion"] == 1


# --- Errors ----------------------------------------------------------------------------------


def test_t295_session_turn_limit_renders_with_session_id_and_without_turn_id() -> None:
    session_id = uuid4()
    error = ConversationError("session_turn_limit", session_id=session_id)

    body = problem_body(error.problem(), "0af7651916cd43dd8448eb211c80319c").model_dump(
        mode="json", exclude_none=True
    )

    assert error.status == 409
    assert body["sessionId"] == str(session_id)
    assert "turnId" not in body


def test_a_turn_in_progress_carries_its_wait_and_both_ids() -> None:
    session_id, active = uuid4(), uuid4()
    error = ConversationError(
        "turn_in_progress", retry_after_seconds=12, session_id=session_id, turn_id=active
    )

    problem = error.problem()

    assert (problem.status, problem.retry_after_seconds) == (409, 12)
    assert (problem.session_id, problem.turn_id) == (session_id, active)


def test_stream_codes_have_no_problem() -> None:
    error = ConversationError("turn_save_failed")

    assert error.status is None
    with pytest.raises(TypeError):
        error.problem()
    with pytest.raises(ValueError, match="CODES"):
        ConversationError("not_a_code")
    assert (
        ConversationError("dependency_unavailable", retry_after_seconds=5).problem().status == 503
    )


# --- begin_turn ------------------------------------------------------------------------------


async def test_a_first_turn_creates_the_session_and_the_turn_in_one_batch(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    client = uuid4()

    result = await started(store, client_turn_id=client)

    session_id = new_session_id(USER, client)
    handle = result.handle
    assert result.is_new_session
    assert (handle.session_id, handle.turn_id) == (session_id, turn_id(session_id, client))
    assert (handle.seq, handle.deadline_at, handle.project_id) == (1, DEADLINE, PROJECT)
    (batch,) = conversations.calls_of("execute_item_batch")
    assert batch.partition_key == (str(USER), str(session_id))
    assert batch.kwargs["timeout"] == 5
    session = conversations.get(pk(session_id), str(session_id))
    turn = conversations.get(pk(session_id), str(handle.turn_id))
    assert session is not None
    assert turn is not None
    assert handle.session_etag == session["_etag"]
    assert handle.turn_etag == turn["_etag"]
    assert session["activeTurn"]["turnId"] == str(handle.turn_id)
    assert session["title"] == "How many days do I have to submit an expense claim?"
    assert (turn["status"], turn["seq"], turn["answer"], turn["traceId"]) == (
        "streaming",
        1,
        "",
        TRACE_ID,
    )
    assert turn["deadlineAt"] == format_utc(DEADLINE)
    assert turn["memoryExtraction"]["state"] == "not_applicable"
    assert turn["historyTokens"] is None
    attributes = span(captured, "docchat.conversations.begin_turn")
    assert attributes["docchat.outcome"] == "started"
    assert attributes["docchat.session_new"] is True
    assert attributes["docchat.turn_seq"] == 1
    # The batch is charged; a 404 raises before the SDK calls the response hook.
    assert attributes["docchat.cosmos.request_charge"] == 1.0


async def test_a_later_turn_takes_the_next_seq(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    session = seed_session(conversations, turn_count=3)

    result = await started(store, session_id=session.session_id)

    assert result.handle.seq == 4
    assert not result.is_new_session
    stored = conversations.get(pk(session.session_id), str(session.session_id))
    assert stored is not None
    assert stored["turnCount"] == 4
    assert stored["lastTurnAt"] == format_utc(START)
    assert stored["activeTurn"]["turnId"] == str(result.handle.turn_id)
    assert result.session.turn_count == 4
    assert result.handle.session_etag == stored["_etag"]


@pytest.mark.parametrize("case", ["missing", "other_project"])
async def test_a_missing_session_and_another_projects_are_both_not_found(
    store: ConversationStore, conversations: FakeContainer, case: str, captured: Captured
) -> None:
    session = seed_session(conversations, project_id=OTHER_PROJECT)
    session_id = uuid4() if case == "missing" else session.session_id

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session_id)

    assert (caught.value.code, caught.value.status) == ("session_not_found", 404)
    assert (caught.value.session_id, caught.value.turn_id) == (None, None)
    assert span(captured, "docchat.conversations.begin_turn")["docchat.outcome"] == "not_found"


async def test_a_session_at_its_turn_limit_refuses(
    conversations: FakeContainer,
    memories: FakeContainer,
    fake_sql: FakeRows,
    clock: FakeClock,
) -> None:
    store = ConversationStore(
        conversations,
        memories,
        ops=Ops(
            await load_ops(fake_sql, **{"conversations.max_turns_per_session": '{"value":50}'})
        ),
        tokenizer=WordTokenizer(),
        clock=clock,
    )
    session = seed_session(conversations, turn_count=50)

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id)

    assert caught.value.code == "session_turn_limit"
    assert (caught.value.session_id, caught.value.turn_id) == (session.session_id, None)


async def test_a_live_active_turn_is_in_progress(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    active = uuid4()
    session = seed_session(
        conversations,
        active_turn={
            "turn_id": active,
            "deadline_at": START + timedelta(seconds=99, milliseconds=1),
        },
    )

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id)

    error = caught.value
    assert (error.code, error.retry_after_seconds) == ("turn_in_progress", 100)
    assert (error.session_id, error.turn_id) == (session.session_id, active)
    assert conversations.calls_of("execute_item_batch") == []


async def test_an_expired_active_turn_is_taken_over(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    session = seed_session(conversations, turn_count=1)
    stale = seed_turn(
        conversations,
        session.session_id,
        status=TurnStatus.STREAMING,
        answer="",
        finish_reason=None,
        history_tokens=None,
        ended_at=None,
        deadline_at=START - timedelta(seconds=1),
    )
    conversations.touch(
        pk(session.session_id),
        str(session.session_id),
        activeTurn={"turnId": str(stale.id), "deadlineAt": format_utc(stale.deadline_at)},
    )

    result = await started(store, session_id=session.session_id)

    assert result.handle.seq == 2
    stored_stale = conversations.get(pk(session.session_id), str(stale.id))
    assert stored_stale is not None
    assert stored_stale["status"] == "failed"
    assert stored_stale["error"] == {"code": "turn_abandoned", "retryable": True}
    assert stored_stale["endedAt"] == format_utc(START)
    (batch,) = conversations.calls_of("execute_item_batch")
    assert batch.partition_key == (str(USER), str(session.session_id))
    assert captured.points("docchat.conversations.abandoned") == {(("source", "takeover"),): 1}


async def test_an_expired_active_turn_already_terminal_adds_nothing(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    session = seed_session(conversations)
    old = seed_turn(conversations, session.session_id)
    conversations.touch(
        pk(session.session_id),
        str(session.session_id),
        activeTurn={"turnId": str(old.id), "deadlineAt": format_utc(START - timedelta(seconds=1))},
    )

    await started(store, session_id=session.session_id)

    stored_old = conversations.get(pk(session.session_id), str(old.id))
    assert stored_old is not None
    assert stored_old["status"] == "completed"
    assert captured.points("docchat.conversations.abandoned") == {}


async def test_a_412_starts_over_on_fresh_reads(
    store: ConversationStore, conversations: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    session = seed_session(conversations)
    conversations.before(
        "execute_item_batch",
        lambda: conversations.touch(pk(session.session_id), str(session.session_id)),
    )
    start = clock.monotonic()

    result = await started(store, session_id=session.session_id)

    assert result.handle.seq == 2
    assert clock.monotonic() - start == pytest.approx(0.025)
    assert span(captured, "docchat.conversations.begin_turn")["docchat.attempts"] == 2
    assert captured.points("docchat.conversations.store_conflicts") == {
        (("operation", "begin_turn"),): 1
    }


async def test_three_412s_without_a_live_turn_are_unavailable(
    store: ConversationStore, conversations: FakeContainer, clock: FakeClock
) -> None:
    session = seed_session(conversations)
    conversations.before(
        "execute_item_batch",
        lambda: conversations.touch(pk(session.session_id), str(session.session_id)),
        times=3,
    )
    start = clock.monotonic()

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id)

    assert (caught.value.code, caught.value.retry_after_seconds) == ("dependency_unavailable", 5)
    assert clock.monotonic() - start == pytest.approx(0.025 + 0.05 + 0.1)
    assert len(conversations.calls_of("execute_item_batch")) == 3


async def test_three_412s_lost_to_another_start_are_in_progress(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    session = seed_session(conversations)
    winner = uuid4()
    calls = 0

    def rival() -> None:
        nonlocal calls
        calls += 1
        changes: dict[str, Any] = {}
        if calls == 3:
            changes["activeTurn"] = {"turnId": str(winner), "deadlineAt": format_utc(DEADLINE)}
        conversations.touch(pk(session.session_id), str(session.session_id), **changes)

    conversations.before("execute_item_batch", rival, times=3)

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id)

    assert (caught.value.code, caught.value.turn_id) == ("turn_in_progress", winner)


async def test_a_concurrent_identical_first_turn_is_settled_by_rereading(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    client = uuid4()
    session_id = new_session_id(USER, client)

    def rival_wins() -> None:
        seed_session(conversations, session_id=session_id)
        seed_turn(
            conversations,
            session_id,
            client,
            status=TurnStatus.STREAMING,
            answer="",
            finish_reason=None,
            history_tokens=None,
            ended_at=None,
            deadline_at=DEADLINE,
        )

    conversations.before("execute_item_batch", rival_wins)

    with pytest.raises(ConversationError) as caught:
        await begin(store, client_turn_id=client)

    assert caught.value.code == "turn_in_progress"
    assert caught.value.retry_after_seconds == 210
    assert captured.points("docchat.conversations.duplicates") == {(("outcome", "in_progress"),): 1}


async def test_a_conflict_that_never_shows_its_turn_is_unavailable(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    conversations.fail("execute_item_batch", error=lambda: batch_error(1, 409, 2))

    with pytest.raises(ConversationError) as caught:
        await begin(store)

    assert caught.value.code == "dependency_unavailable"
    assert len(conversations.calls_of("read_item")) == 4  # the pre-check and 3 re-reads


@pytest.mark.parametrize("failure", ["read", "batch"])
async def test_store_failures_are_unavailable(
    store: ConversationStore, conversations: FakeContainer, failure: str, captured: Captured
) -> None:
    if failure == "read":
        conversations.fail("read_item", error=ServiceRequestError("down"))
    else:
        conversations.fail("execute_item_batch", status=503)

    with pytest.raises(ConversationError) as caught:
        await begin(store)

    assert (caught.value.status, caught.value.retry_after_seconds) == (503, 5)
    assert span(captured, "docchat.conversations.begin_turn")["docchat.outcome"] == "unavailable"


async def test_a_question_breaking_its_limit_is_an_internal_error(store: ConversationStore) -> None:
    with pytest.raises(ConversationError) as caught:
        await begin(store, question="q" * 4_001)

    assert (caught.value.code, caught.value.status) == ("internal_error", 500)


async def test_the_turns_snapshot_sets_the_deadline(
    store: ConversationStore, fake_sql: FakeRows
) -> None:
    snapshot = await load_ops(fake_sql, **{"chat.max_turn_seconds": '{"value":60}'})

    result = await store.begin_turn(
        PRINCIPAL,
        project_id=PROJECT,
        session_id=None,
        client_turn_id=uuid4(),
        question="Who approves late claims?",
        received_at=START,
        trace_id=TRACE_ID,
        ops=snapshot,
    )

    assert isinstance(result, Started)
    assert result.handle.deadline_at == START + timedelta(seconds=90)


# --- Duplicates (section 2.4) -----------------------------------------------------------------


async def test_a_duplicate_of_a_completed_turn_replays_and_writes_nothing(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    session = seed_session(conversations)
    client = uuid4()
    stored = seed_turn(conversations, session.session_id, client)

    result = await begin(store, session_id=session.session_id, client_turn_id=client)

    assert isinstance(result, Replay)
    assert result.turn == stored
    assert {call.method for call in conversations.calls} == {"read_item"}
    assert captured.points("docchat.conversations.duplicates") == {(("outcome", "replay"),): 1}
    assert span(captured, "docchat.conversations.begin_turn")["docchat.outcome"] == "replay"


@pytest.mark.parametrize("change", ["question", "project"])
async def test_a_reused_client_turn_id_with_another_request_conflicts(
    store: ConversationStore, conversations: FakeContainer, change: str
) -> None:
    session = seed_session(conversations)
    client = uuid4()
    stored = seed_turn(conversations, session.session_id, client)

    with pytest.raises(ConversationError) as caught:
        await begin(
            store,
            session_id=session.session_id,
            client_turn_id=client,
            question="Another question?" if change == "question" else stored.question,
            project_id=OTHER_PROJECT if change == "project" else PROJECT,
        )

    error = caught.value
    assert error.code == "client_turn_id_conflict"
    assert (error.session_id, error.turn_id) == (session.session_id, stored.id)


@pytest.mark.parametrize("status", [TurnStatus.FAILED, TurnStatus.CANCELLED])
async def test_a_duplicate_of_an_ended_turn_has_already_ended(
    store: ConversationStore, conversations: FakeContainer, status: TurnStatus
) -> None:
    session = seed_session(conversations)
    client = uuid4()
    error_value = TurnError(code="retrieval_unavailable", retryable=True)
    seed_turn(
        conversations,
        session.session_id,
        client,
        status=status,
        error=error_value if status is TurnStatus.FAILED else None,
        history_tokens=None,
    )

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id, client_turn_id=client)

    assert caught.value.code == "turn_already_ended"


async def test_a_duplicate_of_an_expired_streaming_turn_abandons_it(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    client = uuid4()
    session = seed_session(conversations)
    stale = seed_turn(
        conversations,
        session.session_id,
        client,
        status=TurnStatus.STREAMING,
        answer="",
        finish_reason=None,
        history_tokens=None,
        ended_at=None,
        deadline_at=START - timedelta(seconds=1),
    )
    conversations.touch(
        pk(session.session_id),
        str(session.session_id),
        activeTurn={"turnId": str(stale.id), "deadlineAt": format_utc(stale.deadline_at)},
    )

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id, client_turn_id=client)

    assert caught.value.code == "turn_already_ended"
    turn = conversations.get(pk(session.session_id), str(stale.id))
    stored_session = conversations.get(pk(session.session_id), str(session.session_id))
    assert turn is not None
    assert stored_session is not None
    assert turn["status"] == "failed"
    assert turn["error"]["code"] == "turn_abandoned"
    assert stored_session["activeTurn"] is None
    assert captured.points("docchat.conversations.abandoned") == {(("source", "duplicate"),): 1}


async def test_a_duplicate_that_finds_the_turn_finished_meanwhile_replays_it(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    client = uuid4()
    session = seed_session(conversations)
    stale = seed_turn(
        conversations,
        session.session_id,
        client,
        status=TurnStatus.STREAMING,
        answer="",
        finish_reason=None,
        history_tokens=None,
        ended_at=None,
        deadline_at=START - timedelta(seconds=1),
    )
    conversations.before(
        "replace_item",
        lambda: conversations.touch(
            pk(session.session_id), str(stale.id), status="completed", answer="Done."
        ),
    )

    result = await begin(store, session_id=session.session_id, client_turn_id=client)

    assert isinstance(result, Replay)
    assert result.turn.answer == "Done."


async def test_a_duplicate_of_a_live_streaming_turn_is_in_progress(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    client = uuid4()
    session = seed_session(conversations)
    seed_turn(
        conversations,
        session.session_id,
        client,
        status=TurnStatus.STREAMING,
        answer="",
        finish_reason=None,
        history_tokens=None,
        ended_at=None,
        deadline_at=START + timedelta(seconds=30),
    )

    with pytest.raises(ConversationError) as caught:
        await begin(store, session_id=session.session_id, client_turn_id=client)

    assert (caught.value.code, caught.value.retry_after_seconds) == ("turn_in_progress", 30)


# --- read_recent_turns -------------------------------------------------------------------------


async def _handle(
    store: ConversationStore, conversations: FakeContainer, **session: Any
) -> TurnHandle:
    seeded = seed_session(conversations, **session)
    return (await started(store, session_id=seeded.session_id)).handle


async def test_recent_turns_come_oldest_first_without_citation_tokens(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations, turn_count=5)
    for seq, status in enumerate(
        [TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.COMPLETED, TurnStatus.CANCELLED], 1
    ):
        seed_turn(
            conversations,
            handle.session_id,
            seq=seq,
            status=status,
            answer=f"Answer {seq} ⟦1⟧ and ⟦2⟧.",
            followups=["What is the receipt limit?"] if seq == 3 else [],
            history_tokens=10 if status is TurnStatus.COMPLETED else None,
        )

    turns = await store.read_recent_turns(handle, 28_000)

    assert [turn.seq for turn in turns] == [1, 3]
    assert turns[1].answer == "Answer 3 and."
    assert turns[1].followups == ("What is the receipt limit?",)
    attributes = span(captured, "docchat.conversations.read_recent_turns")
    assert attributes["docchat.history.turns_loaded"] == 2
    assert attributes["docchat.history.tokens_loaded"] == 20
    assert attributes["docchat.history.stopped_at_budget"] is False
    (query,) = conversations.calls_of("query_items")
    assert query.partition_key == (str(USER), str(handle.session_id))
    assert query.kwargs["max_item_count"] == 20
    assert query.kwargs["timeout"] == 5
    assert "'" not in (query.query or "")


async def test_the_summary_covers_the_older_turns(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(
        store, conversations, turn_count=4, summary={"text": "Earlier.", "covers_through_seq": 2}
    )
    for seq in (1, 2, 3, 4):
        seed_turn(conversations, handle.session_id, seq=seq)

    turns = await store.read_recent_turns(handle, 28_000)

    assert [turn.seq for turn in turns] == [3, 4]


async def test_t299_the_read_stops_after_the_turn_that_crosses_the_budget(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations, turn_count=31)
    for seq in range(1, 31):
        seed_turn(conversations, handle.session_id, seq=seq, history_tokens=300)

    turns = await store.read_recent_turns(handle, 1_000)

    assert [turn.seq for turn in turns] == [27, 28, 29, 30]
    assert conversations.pages_served == 1
    assert span(captured, "docchat.conversations.read_recent_turns")[
        "docchat.history.stopped_at_budget"
    ]


async def test_the_sections_example_keeps_5_turns(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations, turn_count=26)
    for seq in range(1, 26):
        seed_turn(conversations, handle.session_id, seq=seq, history_tokens=450)

    turns = await store.read_recent_turns(handle, 2_000)

    assert [turn.seq for turn in turns] == [21, 22, 23, 24, 25]


async def test_a_failed_history_read_is_conversation_unavailable(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("query_items", hang=True)

    with pytest.raises(ConversationError) as caught:
        await store.read_recent_turns(handle, 28_000)

    assert (caught.value.code, caught.value.status) == ("conversation_unavailable", None)


# --- get_prompt_memory (T-325) -------------------------------------------------------------------


def _fact(category: FactCategory, text: str, minutes_ago: int) -> Fact:
    at = START - timedelta(minutes=minutes_ago)
    return Fact(
        id=uuid4(),
        text=text,
        category=category,
        tokens=len(text.split()),
        source=FactSource(kind=FactSourceKind.USER),
        created_at=at,
        updated_at=at,
    )


def _memory(memories: FakeContainer, facts: list[Fact], *, paused: bool = False) -> None:
    document = MemoryDocument.empty(USER, START).model_copy(
        update={"facts": facts, "paused": paused}
    )
    memories.put(document.to_item())


async def test_t325_category_order_labels_and_newest_first(
    store: ConversationStore, memories: FakeContainer, captured: Captured
) -> None:
    _memory(
        memories,
        [
            _fact(FactCategory.ANSWER_STYLE, "Wants short answers", 5),
            _fact(FactCategory.TOPIC, "Works on supplier contracts", 10),
            _fact(FactCategory.ROLE, "Works as a site safety coordinator", 30),
            _fact(FactCategory.TOPIC, "Works on travel policy", 1),
            _fact(FactCategory.TEAM, "Works in the southern depots team", 20),
            _fact(FactCategory.EXPERTISE, "Knows the expense system well", 40),
        ],
    )

    lines = await store.get_prompt_memory(USER)

    assert lines == [
        "Role: Works as a site safety coordinator",
        "Team: Works in the southern depots team",
        "Expertise: Knows the expense system well",
        "Recurring topic: Works on travel policy",
        "Recurring topic: Works on supplier contracts",
        "Answer preference: Wants short answers",
    ]
    attributes = span(captured, "docchat.memory.prompt_read")
    assert attributes == {
        "docchat.memory.active": True,
        "docchat.memory.facts_rendered": 6,
        "docchat.outcome": "ok",
    }
    (read,) = memories.calls_of("read_item")
    assert (read.item_id, read.partition_key, read.kwargs["timeout"]) == (
        str(USER),
        (str(USER),),
        2,
    )


def test_ordered_facts_is_category_order_then_newest_first() -> None:
    facts = [
        _fact(FactCategory.TOPIC, "Works on contracts", 40),
        _fact(FactCategory.ANSWER_STYLE, "Wants short answers", 1),
        _fact(FactCategory.TOPIC, "Works on travel", 5),
        _fact(FactCategory.ROLE, "Works as a coordinator", 50),
    ]

    assert [fact.text for fact in ordered_facts(facts)] == [
        "Works as a coordinator",
        "Works on travel",
        "Works on contracts",
        "Wants short answers",
    ]


def test_t325_the_cap_drops_the_least_recently_updated_facts() -> None:
    facts = [
        _fact(FactCategory.ROLE, "Works as a coordinator", 50),  # oldest
        _fact(FactCategory.TEAM, "Works in the depots team", 10),
        _fact(FactCategory.TOPIC, "Works on contracts", 40),
        _fact(FactCategory.TOPIC, "Works on travel", 5),
    ]
    tokenizer = WordTokenizer()

    # Rendered words: "- Role: ..." 6, "- Team: ..." 7, each "- Recurring topic: ..." 6.
    assert len(fit_memory_lines(facts, tokenizer, 25)) == 4
    kept = fit_memory_lines(facts, tokenizer, 24)

    assert kept == [
        "Team: Works in the depots team",
        "Recurring topic: Works on travel",
        "Recurring topic: Works on contracts",
    ]
    assert fit_memory_lines(facts, tokenizer, 12) == ["Recurring topic: Works on travel"]
    assert fit_memory_lines(facts, tokenizer, 5) == []


@pytest.mark.parametrize("case", ["paused", "missing", "empty"])
async def test_t325_nothing_when_paused_missing_or_empty(
    store: ConversationStore, memories: FakeContainer, case: str, captured: Captured
) -> None:
    if case != "missing":
        _memory(
            memories,
            [] if case == "empty" else [_fact(FactCategory.ROLE, "Works as a clerk", 1)],
            paused=case == "paused",
        )

    assert await store.get_prompt_memory(USER) == []
    attributes = span(captured, "docchat.memory.prompt_read")
    assert attributes["docchat.outcome"] == "empty"
    assert attributes["docchat.memory.active"] is (case != "paused")


async def test_t325_nothing_when_memory_is_off_for_everyone(
    conversations: FakeContainer, memories: FakeContainer, fake_sql: FakeRows, clock: FakeClock
) -> None:
    store = ConversationStore(
        conversations,
        memories,
        ops=Ops(await load_ops(fake_sql, **{"memory.enabled": '{"value":false}'})),
        tokenizer=WordTokenizer(),
        clock=clock,
    )
    _memory(memories, [_fact(FactCategory.ROLE, "Works as a clerk", 1)])

    assert await store.get_prompt_memory(USER) == []
    assert memories.calls == []


async def test_a_failed_memory_read_is_logged_and_gives_nothing(
    store: ConversationStore, memories: FakeContainer, captured: Captured
) -> None:
    memories.fail("read_item", error=ServiceRequestError("down"))

    assert await store.get_prompt_memory(USER) == []
    (record,) = captured.records("memory_unavailable")
    assert record.levelno == logging.WARNING
    assert record.__dict__["error.type"] == "azure.core.exceptions.ServiceRequestError"
    assert captured.points("docchat.memory.read_failures") == {(): 1}
    assert span(captured, "docchat.memory.prompt_read")["docchat.outcome"] == "unavailable"


# --- finalize_turn -------------------------------------------------------------------------------


def _outcome(**overrides: Any) -> TurnOutcome:
    values: dict[str, Any] = {
        "status": TurnStatus.COMPLETED,
        "answer": "You have thirty days ⟦1⟧.",
        "finish_reason": FinishReason.STOP,
        "usage": TurnUsage.of([usage_record(ModelCall.ANSWER)]),
        "trace": EMPTY_TRACE,
    }
    return TurnOutcome(**(values | overrides))


async def test_a_completed_turn_is_finalised_in_one_batch(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(
        store, conversations, history={"unsummarised_tokens": 5, "unsummarised_turns": 1}
    )
    question_words = len(handle.turn.question.split())

    result = await store.finalize_turn(handle, _outcome())

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    trace = conversations.get(pk(handle.session_id), str(turn_trace_id(handle.turn_id)))
    session = conversations.get(pk(handle.session_id), str(handle.session_id))
    assert turn is not None
    assert trace is not None
    assert session is not None
    assert result == FinalizeResult(saved=True, turn_etag=turn["_etag"])
    assert turn["status"] == "completed"
    assert turn["answer"] == "You have thirty days ⟦1⟧."
    assert turn["historyTokens"] == question_words + 4
    assert turn["memoryExtraction"]["state"] == "pending"
    assert turn["memoryExtraction"]["attempts"] == 0
    assert turn["endedAt"] == format_utc(START)
    assert turn["usage"]["totals"]["totalTokens"] == 960
    assert trace["type"] == "turn_trace"
    assert trace["turnId"] == str(handle.turn_id)
    assert session["activeTurn"] is None
    assert session["history"] == {
        "unsummarisedTokens": 5 + question_words + 4,
        "unsummarisedTurns": 2,
    }
    assert handle.turn_etag == turn["_etag"]
    assert handle.session_etag == session["_etag"]
    assert handle.turn.status is TurnStatus.COMPLETED
    assert captured.points("docchat.conversations.turns_finalized") == {
        (("error_code", "none"), ("status", "completed")): 1
    }
    attributes = span(captured, "docchat.conversations.finalize_turn")
    assert attributes["docchat.outcome"] == "saved"
    assert attributes["docchat.attempts"] == 1
    assert attributes["docchat.turn.status"] == "completed"


async def test_a_suggested_question_is_never_extracted(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)

    await store.finalize_turn(handle, _outcome(question_suggested=True))

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    assert turn is not None
    assert turn["memoryExtraction"]["state"] == "not_applicable"
    assert turn["memoryExtraction"]["reason"] == "suggested_question"


async def test_memory_off_for_everyone_leaves_completed_turns_not_applicable(
    conversations: FakeContainer, memories: FakeContainer, fake_sql: FakeRows, clock: FakeClock
) -> None:
    store = ConversationStore(
        conversations,
        memories,
        ops=Ops(await load_ops(fake_sql, **{"memory.enabled": '{"value":false}'})),
        tokenizer=WordTokenizer(),
        clock=clock,
    )
    handle = await _handle(store, conversations)

    await store.finalize_turn(handle, _outcome())

    assert handle.turn.memory_extraction.state is ExtractionState.NOT_APPLICABLE
    assert handle.turn.memory_extraction.reason is None


@pytest.mark.parametrize("status", [TurnStatus.FAILED, TurnStatus.CANCELLED])
async def test_failed_and_cancelled_turns_count_no_history(
    store: ConversationStore, conversations: FakeContainer, status: TurnStatus, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    error = TurnError(code="retrieval_unavailable", retryable=True)

    await store.finalize_turn(
        handle,
        _outcome(
            status=status,
            answer="Partial",
            finish_reason=None if status is TurnStatus.FAILED else FinishReason.CANCELLED,
            error=error if status is TurnStatus.FAILED else None,
        ),
    )

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    session = conversations.get(pk(handle.session_id), str(handle.session_id))
    assert turn is not None
    assert session is not None
    assert (turn["status"], turn["historyTokens"]) == (status.value, None)
    assert turn["memoryExtraction"]["state"] == "not_applicable"
    assert session["history"]["unsummarisedTurns"] == 0
    assert session["activeTurn"] is None
    expected_code = "retrieval_unavailable" if status is TurnStatus.FAILED else "none"
    assert captured.points("docchat.conversations.turns_finalized") == {
        (("error_code", expected_code), ("status", status.value)): 1
    }


async def test_t297_a_session_changed_meanwhile_is_reread_and_counts_history_once(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    lease = format_utc(START + timedelta(minutes=2))
    conversations.touch(
        pk(handle.session_id),
        str(handle.session_id),
        summaryJob={
            "leaseExpiresAt": lease,
            "consecutiveFailures": 0,
            "notBefore": None,
            "lastErrorCode": None,
            "lastAttemptAt": lease,
            "lastSucceededAt": None,
        },
    )

    result = await store.finalize_turn(handle, _outcome())

    session = conversations.get(pk(handle.session_id), str(handle.session_id))
    assert session is not None
    assert result.saved
    assert session["history"]["unsummarisedTurns"] == 1
    assert session["summaryJob"]["leaseExpiresAt"] == lease
    assert session["activeTurn"] is None
    assert captured.points("docchat.conversations.store_conflicts") == {
        (("operation", "finalize_turn"),): 1
    }
    assert span(captured, "docchat.conversations.finalize_turn")["docchat.attempts"] == 2


async def test_t297_a_batch_whose_response_was_lost_is_saved_once(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("execute_item_batch", status=503, after_commit=True)

    result = await store.finalize_turn(handle, _outcome())

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    assert turn is not None
    assert result == FinalizeResult(saved=True, turn_etag=turn["_etag"])
    assert captured.points("docchat.conversations.turns_finalized") == {}


async def test_t297_a_turn_taken_over_before_finalisation_is_superseded(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    conversations.touch(
        pk(handle.session_id),
        str(handle.turn_id),
        status="failed",
        error={"code": "turn_abandoned", "retryable": True},
    )

    result = await store.finalize_turn(handle, _outcome())

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    assert turn is not None
    assert result == FinalizeResult(saved=False, turn_etag=None)
    assert turn["error"]["code"] == "turn_abandoned"
    assert conversations.get(pk(handle.session_id), str(turn_trace_id(handle.turn_id))) is None
    assert captured.points("docchat.conversations.finalize_superseded") == {(): 1}
    assert span(captured, "docchat.conversations.finalize_turn")["docchat.outcome"] == "superseded"


async def test_a_streaming_turn_changed_meanwhile_is_resubmitted(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)
    conversations.touch(pk(handle.session_id), str(handle.turn_id))

    result = await store.finalize_turn(handle, _outcome())

    assert result.saved


async def test_five_failed_attempts_raise_turn_save_failed(
    store: ConversationStore, conversations: FakeContainer, clock: FakeClock, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("execute_item_batch", status=429, times=5)
    start = clock.monotonic()

    with pytest.raises(ConversationError) as caught:
        await store.finalize_turn(handle, _outcome())

    assert caught.value.code == "turn_save_failed"
    assert clock.monotonic() - start == pytest.approx(0.025 + 0.05 + 0.1 + 0.2)
    assert len(conversations.calls_of("execute_item_batch")) == 6  # begin_turn's and 5
    assert span(captured, "docchat.conversations.finalize_turn")["docchat.outcome"] == "failed"


async def test_a_failure_that_cant_be_retried_raises_at_once(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("execute_item_batch", error=ServiceRequestError("down"))

    with pytest.raises(ConversationError) as caught:
        await store.finalize_turn(handle, _outcome())

    assert caught.value.code == "turn_save_failed"


async def test_the_15_second_budget_stops_the_attempts(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("execute_item_batch", hang=True, times=5)

    with pytest.raises(ConversationError):
        await store.finalize_turn(handle, _outcome())

    # Each attempt times out after 5 s, so the budget allows three.
    assert len(conversations.calls_of("execute_item_batch")) == 1 + 3


async def test_t093_a_trace_above_its_limit_is_stored_without_its_model_input(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    model_input = ModelInput(
        rules_version="rules-v1",
        marker="0123456789abcdef",
        deployment="chat",
        model="gpt-5.6-sol",
        max_completion_tokens=12_000,
        reasoning_effort=ReasoningEffort.LOW,
        messages=[PromptMessage(role="user", content=canary("chunk") * 100_000)],
        prompt_tokens=28_000,
        dropped_turns=0,
        dropped_source_ids=[],
    )

    await store.finalize_turn(
        handle, _outcome(trace=EMPTY_TRACE.model_copy(update={"model_input": model_input}))
    )

    trace = conversations.get(pk(handle.session_id), str(turn_trace_id(handle.turn_id)))
    assert trace is not None
    assert trace["modelInput"] == {"omitted": "size_limit"}
    assert captured.points("docchat.conversations.trace_truncated") == {(): 1}


async def test_t093_a_turn_above_350000_bytes_is_an_internal_error(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)

    with pytest.raises(ConversationError) as caught:
        await store.finalize_turn(handle, _outcome(answer="😀" * 100_000))

    assert caught.value.code == "internal_error"
    assert len(conversations.calls_of("execute_item_batch")) == 1


async def test_t300_a_large_turn_counts_its_tokens_in_a_worker_thread(
    store: ConversationStore, conversations: FakeContainer, tokenizer: WordTokenizer
) -> None:
    handle = await _handle(store, conversations)
    tokenizer.threads.clear()

    result = await store.finalize_turn(handle, _outcome(answer="word " * 20_000))

    assert result.saved
    assert tokenizer.threads
    assert threading.get_ident() not in tokenizer.threads
    assert handle.turn.history_tokens == len(handle.turn.question.split()) + 20_000


def test_an_outcome_is_terminal() -> None:
    with pytest.raises(ValueError, match="terminal"):
        TurnOutcome(status=TurnStatus.STREAMING, trace=EMPTY_TRACE)


# --- attach_followups ----------------------------------------------------------------------------


async def test_followups_and_their_usage_are_attached_without_a_condition(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    await store.finalize_turn(handle, _outcome())

    saved = await store.attach_followups(handle, ["What about receipts?"], usage_record())

    turn = conversations.get(pk(handle.session_id), str(handle.turn_id))
    assert turn is not None
    assert saved
    assert turn["followups"] == ["What about receipts?"]
    assert turn["followupsAt"] == format_utc(START)
    assert [call["call"] for call in turn["usage"]["calls"]] == ["answer", "followups"]
    assert turn["usage"]["totals"]["totalTokens"] == 1_920
    (patch,) = conversations.calls_of("patch_item")
    assert "etag" not in patch.kwargs
    assert handle.turn.followups == ["What about receipts?"]
    assert span(captured, "docchat.conversations.attach_followups")["docchat.outcome"] == "saved"


async def test_followups_that_cant_be_stored_return_false(
    store: ConversationStore, conversations: FakeContainer, captured: Captured
) -> None:
    handle = await _handle(store, conversations)
    await store.finalize_turn(handle, _outcome())
    conversations.fail("patch_item", status=503, times=3)

    saved = await store.attach_followups(handle, ["What about receipts?"], usage_record())

    assert not saved
    assert len(conversations.calls_of("patch_item")) == 3
    assert captured.events("followups_not_saved") == [
        {"sessionId": str(handle.session_id), "turnId": str(handle.turn_id)}
    ]
    assert captured.points("docchat.conversations.followups_not_saved") == {(): 1}


async def test_a_followups_failure_that_cant_be_retried_stops_at_once(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    handle = await _handle(store, conversations)
    conversations.fail("patch_item", status=404)

    assert not await store.attach_followups(handle, ["Why?"], usage_record())
    assert len(conversations.calls_of("patch_item")) == 1


# --- get_turn ------------------------------------------------------------------------------------


async def test_get_turn_reads_in_the_callers_partition(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    session = seed_session(conversations)
    stored = seed_turn(conversations, session.session_id)

    assert await store.get_turn(PRINCIPAL, session.session_id, stored.id) == stored
    assert await store.get_turn(PRINCIPAL, session.session_id, uuid4()) is None
    other = PRINCIPAL.model_copy(update={"oid": uuid4()})
    assert await store.get_turn(other, session.session_id, stored.id) is None


async def test_get_turn_fails_with_dependency_unavailable(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    conversations.fail("read_item", status=503)

    with pytest.raises(ConversationError) as caught:
        await store.get_turn(PRINCIPAL, uuid4(), uuid4())

    assert (caught.value.status, caught.value.retry_after_seconds) == (503, 5)


# --- The routes' reads and writes ------------------------------------------------------------


async def test_route_reads_and_writes_carry_their_timeouts_and_charges(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    session = seed_session(conversations, turn_count=2)
    seed_turn(conversations, session.session_id)
    charges = Charges()

    assert await store.read_session(PRINCIPAL, session.session_id, charges) == session
    assert await store.read_session(PRINCIPAL, uuid4(), charges) is None
    rows = await store.list_turn_rows(
        PRINCIPAL, session.session_id, before=3, take=2, charges=charges
    )

    assert [row["seq"] for row in rows] == [1]
    assert charges.total == 2.0
    read, _, query = conversations.calls
    assert (read.kwargs["timeout"], query.kwargs["timeout"]) == (2, 5)
    assert query.partition_key == (str(USER), str(session.session_id))


async def test_the_session_list_reads_the_callers_prefix_after_a_keyset_position(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    for minutes in (1, 2, 3):
        seed_session(conversations, last_turn_at=START + timedelta(minutes=minutes))
    seed_session(conversations, user_id=uuid4(), last_turn_at=START)

    first = await store.list_session_rows(
        PRINCIPAL, project_ids=[PROJECT], take=2, after=None, charges=Charges()
    )
    last = first[-1]
    rest = await store.list_session_rows(
        PRINCIPAL,
        project_ids=[PROJECT],
        take=2,
        after=(datetime.fromisoformat(last["lastTurnAt"]), UUID(last["sessionId"])),
        charges=Charges(),
    )

    assert len(first) == 2
    assert len(rest) == 1
    assert {call.partition_key for call in conversations.calls_of("query_items")} == {(str(USER),)}
    parameters = {
        item["name"]: item["value"]
        for item in conversations.calls_of("query_items")[1].kwargs["parameters"]
    }
    assert parameters["@t"] == last["lastTurnAt"]
    assert parameters["@projectIds"] == [str(PROJECT)]


async def test_route_queries_fail_with_dependency_unavailable(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    conversations.fail("query_items", error=ServiceRequestError("down"))

    with pytest.raises(ConversationError) as caught:
        await store.list_turn_rows(PRINCIPAL, uuid4(), before=2, take=2, charges=Charges())

    assert caught.value.code == "dependency_unavailable"


async def test_a_feedback_patch_is_conditioned_on_the_etag_it_was_read_with(
    store: ConversationStore, conversations: FakeContainer
) -> None:
    session = seed_session(conversations)
    turn = seed_turn(conversations, session.session_id)
    entry = await store.read_turn_entry(PRINCIPAL, session.session_id, turn.id, Charges())
    assert entry is not None
    feedback = Feedback(rating=FeedbackRating.UP, created_at=START, updated_at=START)

    conversations.touch(pk(session.session_id), str(turn.id), followups=["Next?"])
    assert not await store.patch_feedback(PRINCIPAL, entry, feedback, START, Charges())
    fresh = await store.read_turn_entry(PRINCIPAL, session.session_id, turn.id, Charges())
    assert fresh is not None
    assert await store.patch_feedback(PRINCIPAL, fresh, feedback, START, Charges())

    stored = conversations.get(pk(session.session_id), str(turn.id))
    assert stored is not None
    assert stored["feedback"]["rating"] == "up"
    assert stored["followups"] == ["Next?"]
    assert await store.read_turn_entry(PRINCIPAL, session.session_id, uuid4(), Charges()) is None


async def test_memory_documents_are_created_once_and_replaced_on_their_etag(
    store: ConversationStore, memories: FakeContainer
) -> None:
    created = await store.ensure_memory(USER, Charges())
    again = await store.ensure_memory(USER, Charges())
    paused = created.document.model_copy(update={"paused": True, "updated_at": START})

    written = await store.replace_memory(paused, created.etag, Charges())
    stale = await store.replace_memory(paused, created.etag, Charges())

    assert again == created
    assert created.document.facts == []
    assert len(memories.calls_of("create_item")) == 1
    assert written is not None
    assert written.document.paused is True
    assert written.etag != created.etag
    assert stale is None
    assert await store.read_memory(uuid4(), Charges()) is None


async def test_a_concurrently_created_memory_document_is_read(
    store: ConversationStore, memories: FakeContainer
) -> None:
    rival = MemoryDocument.empty(USER, START)
    memories.before("create_item", lambda: memories.put(rival.to_item()))

    stored = await store.ensure_memory(USER, Charges())

    assert stored.document == rival
    assert len(memories.calls_of("read_item")) == 2


async def test_a_memory_read_failure_is_dependency_unavailable(
    store: ConversationStore, memories: FakeContainer
) -> None:
    memories.fail("read_item", error=ServiceRequestError("down"))

    with pytest.raises(ConversationError) as caught:
        await store.ensure_memory(USER, Charges())

    assert caught.value.code == "dependency_unavailable"


def test_problem_on_failure_turns_a_store_error_into_its_problem() -> None:
    with pytest.raises(DependencyUnavailable), problem_on_failure():
        raise ConversationError("dependency_unavailable")
