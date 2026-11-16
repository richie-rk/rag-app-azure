"""The after-response work: scheduling, the turn-start re-check and its throttle, the task's steps
and their isolation, the pending re-check, and the Cosmos DB data-access audit of the task's calls
(`13-conversations-and-memory.md` sections 6.4 and 6.5; `84-testing.md` section 2.11)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.chat.calls import ChatDeploymentSettings
from docchat_api.conversations import post_turn as post_turn_module
from docchat_api.conversations.extraction import MemoryExtractor
from docchat_api.conversations.model_calls import SmallModel
from docchat_api.conversations.post_turn import (
    RECHECK_QUERY,
    PostTurnWorker,
    build_post_turn_work,
    report_step_failed,
)
from docchat_api.conversations.summary import SummaryRebuilder
from docchat_api.usage import QuotaCheck
from docchat_core.audit import AuditEvent
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES
from docchat_core.cosmos.conversations import (
    ExtractionState,
    MemoryExtractionState,
    ModelCallUsage,
    ReasoningEffort,
    SessionDocument,
    SessionHistory,
    TurnDocument,
    TurnStatus,
)
from docchat_core.opsconfig import API_AREAS, OpsConfig, OpsConfigStore, RowReader
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_testing.builders.cosmos import session_document, turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.data_audit import CosmosAudit
from docchat_testing.fakes.chat import CharacterTokenizer, FakeModelClient
from docchat_testing.fakes.cosmos import FakeContainer

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TENANT = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
WORKLOAD = UUID("5f1c0de0-0000-4000-8000-0000000000a1")
EFFORTS = frozenset({"none", "low", "medium"})
TOKENIZER = CharacterTokenizer()
SMALL = ChatDeploymentSettings(
    deployment="chat-small",
    model="gpt-5.6-terra",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.NONE,
    prompt_cache="disabled",
    tokenizer=TOKENIZER,
)
MEMORY_CALL = "memory_operations"


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


class FakeRows(Protocol):
    rows: dict[str, str]


async def load_ops(fake_sql: FakeRows, **values: str) -> OpsConfig:
    fake_sql.rows.update(values)
    store = OpsConfigStore(cast("RowReader", fake_sql), API_AREAS, chat_small_efforts=EFFORTS)
    return await store.start()


class Ops:
    def __init__(self, config: OpsConfig) -> None:
        self.config = config
        self.failure: Exception | None = None

    async def snapshot(self) -> OpsConfig:
        if self.failure is not None:
            raise self.failure
        return self.config


async def allowed(_oid: UUID) -> QuotaCheck:
    return QuotaCheck(allowed=True, exceeded=None, retry_after_seconds=None)


async def passes(_text: str) -> bool:
    return False


class Audit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def __call__(self, event: AuditEvent) -> bool:
        self.events.append(event)
        return True


async def _recorded() -> None:
    return None


def recorder(oid: UUID, record: ModelCallUsage, **labels: Any) -> Any:
    return _recorded()


@dataclass
class World:
    clock: FakeClock
    conversations: Any
    memories: Any
    ops: Ops
    model: FakeModelClient = field(default_factory=FakeModelClient)
    audit: Audit = field(default_factory=Audit)
    registered: list[asyncio.Task[Any]] = field(default_factory=list)
    user: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)
    worker: PostTurnWorker = field(init=False)

    def __post_init__(self) -> None:
        model = SmallModel(
            self.model.client(), SMALL, clock=self.clock, recorder=recorder, sleep=self.clock.sleep
        )
        self.worker = PostTurnWorker(
            self.conversations,
            summary=SummaryRebuilder(
                self.conversations,
                model=model,
                tokenizer=TOKENIZER,
                tenant_id=TENANT,
                clock=self.clock,
                quota=allowed,
            ),
            extraction=MemoryExtractor(
                self.conversations,
                self.memories,
                model=model,
                tokenizer=TOKENIZER,
                tenant_id=TENANT,
                workload_principal_id=WORKLOAD,
                audit=self.audit,
                clock=self.clock,
                quota=allowed,
                shield=passes,
            ),
            ops=self.ops,
            snapshot=self.ops.config,
            clock=self.clock,
            register=self.registered.append,
        )

    @property
    def fake(self) -> FakeContainer:
        """The conversations container under any wrapper."""
        inner = getattr(self.conversations, "_inner", self.conversations)
        return cast("FakeContainer", inner)

    def seed_session(self, session_id: UUID | None = None, **overrides: Any) -> SessionDocument:
        session = session_id or self.session_id
        values: dict[str, Any] = {
            "turn_count": 9,
            "created_at": START - timedelta(hours=1),
            "updated_at": START - timedelta(minutes=1),
            "last_turn_at": START - timedelta(minutes=1),
        }
        document = session_document(user_id=self.user, session_id=session, **(values | overrides))
        self.fake.put(document.to_item())
        return document

    def seed_turn(
        self,
        *,
        session_id: UUID | None = None,
        seq: int = 1,
        state: ExtractionState = ExtractionState.PENDING,
        ended_at: datetime = START - timedelta(minutes=1),
        lease: datetime | None = None,
        **overrides: Any,
    ) -> TurnDocument:
        turn = turn_document(
            user_id=self.user,
            session_id=session_id or self.session_id,
            seq=seq,
            question=f"Question {seq}?",
            answer=f"Answer {seq}.",
            history_tokens=400,
            memory_extraction=MemoryExtractionState(state=state, lease_expires_at=lease),
            created_at=ended_at - timedelta(seconds=10),
            ended_at=ended_at,
            updated_at=ended_at,
            deadline_at=ended_at + timedelta(seconds=200),
            **overrides,
        )
        self.fake.put(turn.to_item())
        return turn

    def turn(self, turn: TurnDocument) -> TurnDocument:
        item = self.fake.get([str(self.user), str(turn.session_id)], str(turn.id))
        assert item is not None
        return TurnDocument.from_item(item)

    def session(self) -> SessionDocument:
        item = self.fake.get([str(self.user), str(self.session_id)], str(self.session_id))
        assert item is not None
        return SessionDocument.from_item(item)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
async def snapshot(fake_sql: FakeRows) -> OpsConfig:
    return await load_ops(fake_sql)


@pytest.fixture
def world(clock: FakeClock, snapshot: OpsConfig) -> World:
    return World(
        clock=clock,
        conversations=FakeContainer.of(CONVERSATIONS, clock=clock),
        memories=FakeContainer.of(MEMORIES, clock=clock),
        ops=Ops(snapshot),
    )


def spans(captured: Captured, name: str) -> list[dict[str, Any]]:
    return [dict(found.attributes or {}) for found in captured.finished_spans(name)]


# --- Scheduling (section 6.4 item 1) ----------------------------------------------------------


async def test_schedule_registers_one_tracked_task_and_returns_at_once(world: World) -> None:
    turn = world.seed_turn()
    world.seed_session()

    world.worker.schedule_post_turn_work(world.user, world.session_id, turn.id)

    assert len(world.registered) == 1
    assert world.fake.calls == []
    await world.worker.wait_idle()
    assert world.registered[0].done()
    assert world.turn(turn).memory_extraction.state is ExtractionState.DONE


async def test_the_task_extracts_its_turn_then_rechecks_then_reads_the_session(
    world: World, captured: Captured
) -> None:
    turn = world.seed_turn()
    world.seed_session()

    await world.worker.run(world.user, world.session_id, turn.id)

    assert [call.method for call in world.fake.calls][:1] == ["read_item"]
    queries = world.fake.calls_of("query_items")
    assert [call.query for call in queries] == [RECHECK_QUERY]
    assert queries[0].partition_key == (str(world.user),)
    assert world.fake.calls[-1].method == "read_item"
    assert world.fake.calls[-1].item_id == str(world.session_id)
    assert len(world.model.json_requests(MEMORY_CALL)) == 1
    assert world.model.text_requests() == []
    (attributes,) = spans(captured, "docchat.maintenance.post_turn")
    assert attributes == {
        "docchat.session_id": str(world.session_id),
        "docchat.turn_id": str(turn.id),
        "docchat.maintenance.extractions": 1,
        "docchat.maintenance.summary_ran": False,
    }


async def test_a_turn_that_is_not_pending_is_not_extracted(world: World) -> None:
    turn = world.seed_turn(state=ExtractionState.NOT_APPLICABLE)
    world.seed_session()

    await world.worker.run(world.user, world.session_id, turn.id)

    assert world.model.requests == []


async def test_a_due_session_is_rebuilt_after_the_extractions(
    world: World, captured: Captured
) -> None:
    world.seed_session(
        history=SessionHistory(unsummarised_tokens=3_600, unsummarised_turns=9),
    )
    for seq in range(1, 10):
        world.seed_turn(seq=seq, state=ExtractionState.DONE)

    await world.worker.run(world.user, world.session_id, None)

    assert len(world.model.text_requests()) == 1
    assert world.session().summary.covers_through_seq == 6
    (attributes,) = spans(captured, "docchat.maintenance.post_turn")
    assert attributes["docchat.maintenance.summary_ran"] is True
    assert "docchat.turn_id" not in attributes


# --- The pending re-check (section 6.4 step 2.3) -----------------------------------------------


async def test_the_recheck_takes_the_five_oldest_other_pending_turns(world: World) -> None:
    own = world.seed_turn(ended_at=START - timedelta(minutes=30))
    world.seed_session()
    others = [
        world.seed_turn(session_id=uuid4(), ended_at=START - timedelta(minutes=20 - minute))
        for minute in range(7)
    ]

    await world.worker.run(world.user, world.session_id, own.id)

    states = [world.turn(turn).memory_extraction.state for turn in others]
    assert states == [ExtractionState.DONE] * 5 + [ExtractionState.PENDING] * 2
    assert world.turn(own).memory_extraction.state is ExtractionState.DONE
    assert len(world.model.json_requests(MEMORY_CALL)) == 6


async def test_the_recheck_expires_old_turns_and_respects_live_leases(world: World) -> None:
    world.seed_session()
    old = world.seed_turn(session_id=uuid4(), ended_at=START - timedelta(hours=30))
    live = world.seed_turn(
        session_id=uuid4(),
        state=ExtractionState.RUNNING,
        lease=START + timedelta(seconds=30),
        ended_at=START - timedelta(minutes=5),
    )
    stale = world.seed_turn(
        session_id=uuid4(),
        state=ExtractionState.RUNNING,
        lease=START - timedelta(seconds=1),
        ended_at=START - timedelta(minutes=4),
    )

    await world.worker.run(world.user, world.session_id, None)

    assert world.turn(old).memory_extraction.state is ExtractionState.EXPIRED
    assert world.turn(live).memory_extraction.state is ExtractionState.RUNNING
    assert world.turn(stale).memory_extraction.state is ExtractionState.DONE
    assert len(world.model.json_requests(MEMORY_CALL)) == 1


# --- Isolation of steps (section 6.4 item 3) -----------------------------------------------------


async def test_a_failed_step_is_logged_and_the_next_step_still_runs(
    world: World, captured: Captured
) -> None:
    turn = world.seed_turn()
    world.seed_session(history=SessionHistory(unsummarised_tokens=3_600, unsummarised_turns=9))
    for seq in range(2, 10):
        world.seed_turn(seq=seq, state=ExtractionState.DONE)
    world.fake.fail("read_item", status=503, item_id=str(turn.id))

    await world.worker.run(world.user, world.session_id, turn.id)

    (record,) = captured.records("post_turn_step_failed")
    assert record.levelno == logging.WARNING
    assert record.__dict__["docchat.maintenance.step"] == "memory_extraction"
    assert record.__dict__["docchat.error_code"] == "dependency_unavailable"
    assert record.__dict__["docchat.session_id"] == str(world.session_id)
    assert record.__dict__["docchat.turn_id"] == str(turn.id)
    assert len(world.model.text_requests()) == 1


async def test_an_unexpected_failure_is_internal_error_and_the_task_never_raises(
    world: World, captured: Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = world.seed_turn()
    world.seed_session(history=SessionHistory(unsummarised_tokens=3_600, unsummarised_turns=9))

    async def broken(*_args: object) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(world.worker._extraction, "extract", broken)
    monkeypatch.setattr(world.worker._summary, "rebuild", broken)

    await world.worker.run(world.user, world.session_id, turn.id)

    records = captured.records("post_turn_step_failed")
    assert [
        (record.__dict__["docchat.maintenance.step"], record.__dict__["docchat.error_code"])
        for record in records
    ] == [("memory_extraction", "internal_error"), ("summary_rebuild", "internal_error")]


async def test_a_failed_recheck_query_is_one_step_failure(world: World, captured: Captured) -> None:
    world.seed_session()
    world.fake.fail("query_items", status=429)

    await world.worker.run(world.user, world.session_id, None)

    (record,) = captured.records("post_turn_step_failed")
    assert record.__dict__["docchat.maintenance.step"] == "memory_extraction"
    assert "docchat.turn_id" not in record.__dict__


async def test_without_a_snapshot_both_steps_are_reported(world: World, captured: Captured) -> None:
    world.ops.failure = SqlUnavailable("exhausted", 40613, "")

    await world.worker.run(world.user, world.session_id, None)

    steps = [
        record.__dict__["docchat.maintenance.step"]
        for record in captured.records("post_turn_step_failed")
    ]
    assert steps == ["memory_extraction", "summary_rebuild"]
    assert world.fake.calls == []


def test_report_step_failed_maps_failures_to_stable_codes(captured: Captured) -> None:
    session = uuid4()
    report_step_failed("summary_rebuild", TimeoutError(), session, None)
    report_step_failed("summary_rebuild", ValueError("text"), session, None)

    codes = [
        record.__dict__["docchat.error_code"]
        for record in captured.records("post_turn_step_failed")
    ]
    assert codes == ["dependency_unavailable", "internal_error"]


# --- The turn-start re-check (section 6.5) -----------------------------------------------------


async def test_ensure_schedules_once_a_minute_per_user_and_always_when_due(
    world: World, captured: Captured
) -> None:
    quiet = world.seed_session()
    due = quiet.model_copy(
        update={"history": SessionHistory(unsummarised_tokens=3_001, unsummarised_turns=8)}
    )

    world.worker.ensure_post_turn_work(quiet)
    world.worker.ensure_post_turn_work(quiet)
    assert world.fake.calls == []
    world.worker.ensure_post_turn_work(due)
    world.clock.advance(60)
    world.worker.ensure_post_turn_work(quiet)

    assert len(world.registered) == 3
    attributes = spans(captured, "docchat.conversations.ensure_post_turn_work")
    assert [item["docchat.maintenance.scheduled"] for item in attributes] == [
        True,
        False,
        True,
        True,
    ]
    assert [item["docchat.summary.due"] for item in attributes] == [False, False, True, False]
    assert attributes[2]["docchat.history.unsummarised_tokens"] == 3_001
    assert attributes[2]["docchat.history.unsummarised_turns"] == 8
    await world.worker.wait_idle()


async def test_ensure_clears_a_full_throttle_before_adding(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(post_turn_module, "THROTTLE_USERS_MAX", 2)
    sessions = [session_document(updated_at=START, created_at=START) for _ in range(3)]

    for session in sessions:
        world.worker.ensure_post_turn_work(session)
    world.worker.ensure_post_turn_work(sessions[0])

    assert len(world.registered) == 4
    await world.worker.wait_idle()


async def test_aclose_cancels_running_work_and_stops_scheduling(world: World) -> None:
    gate = asyncio.Event()
    world.fake.before("query_items", gate.wait)
    world.seed_session()

    world.worker.schedule_post_turn_work(world.user, world.session_id, uuid4())
    await asyncio.sleep(0)
    await world.worker.aclose()
    world.worker.schedule_post_turn_work(world.user, world.session_id, uuid4())

    assert len(world.registered) == 1
    assert world.registered[0].cancelled()


async def test_the_unsummarised_tokens_histogram_records_each_start(
    world: World, captured: Captured
) -> None:
    session = world.seed_session(
        history=SessionHistory(unsummarised_tokens=1_234, unsummarised_turns=3)
    )

    world.worker.ensure_post_turn_work(session)

    assert "docchat.summary.unsummarised_tokens" in {
        metric.name
        for resource in (cast("Any", captured).metrics.get_metrics_data().resource_metrics)
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    await world.worker.wait_idle()


# --- T-301's after-response entries: the Cosmos DB data-access audit ------------------------


async def test_t301_the_task_names_full_keys_and_its_recheck_is_the_one_prefix_query(
    clock: FakeClock, snapshot: OpsConfig, captured: Captured
) -> None:
    audit = CosmosAudit()
    conversations = FakeContainer.of(CONVERSATIONS, clock=clock)
    memories = FakeContainer.of(MEMORIES, clock=clock)
    world = World(
        clock=clock,
        conversations=audit(cast("Any", conversations)),
        memories=audit(cast("Any", memories)),
        ops=Ops(snapshot),
    )
    turn = world.seed_turn()
    world.seed_session(history=SessionHistory(unsummarised_tokens=3_600, unsummarised_turns=9))
    for seq in range(2, 10):
        world.seed_turn(seq=seq, state=ExtractionState.DONE)
    # A turn of the same user still streaming in another session: the re-check runs anyway,
    # off the request path.
    world.seed_turn(session_id=uuid4(), status=TurnStatus.STREAMING, ended_at=START, seq=1)
    audit.observe_write(
        CONVERSATIONS.name,
        {"type": "turn", "userId": str(world.user), "id": "x", "status": "streaming"},
    )

    await world.worker.run(world.user, world.session_id, turn.id)

    audit.check()
    prefix = [call for call in audit.calls if call.prefix_query]
    assert len(prefix) == 1
    assert prefix[0].context.after_response == "docchat.maintenance.post_turn"
    assert {call.container for call in audit.calls} == {CONVERSATIONS.name, MEMORIES.name}
    assert all(call.context.after_response is not None for call in audit.calls)
    assert audit.user_ids() == {str(world.user)}


def test_build_post_turn_work_composes_the_worker() -> None:
    clock = FakeClock(START)
    worker = build_post_turn_work(
        conversations=FakeContainer.of(CONVERSATIONS, clock=clock),
        memories=FakeContainer.of(MEMORIES, clock=clock),
        ops=Ops(cast("OpsConfig", None)),
        snapshot=cast("OpsConfig", None),
        openai=FakeModelClient().client(),
        small=SMALL,
        tokenizer=TOKENIZER,
        sql=cast("SqlEngine", object()),
        tenant_id=TENANT,
        workload_principal_id=WORKLOAD,
        clock=clock,
    )

    assert isinstance(worker, PostTurnWorker)
