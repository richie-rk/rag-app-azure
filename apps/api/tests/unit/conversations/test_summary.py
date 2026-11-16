"""The rolling summary: the trigger, the fold set, the call's messages and output, and the rebuild
with its lease, quota deferral, back-off and conflicts (T-308, T-313's summary half, T-314, the
unit half of T-312; `13-conversations-and-memory.md` sections 6.1 to 6.3, 6.6 and 6.7)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import openai
import pytest
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.chat.calls import ChatDeploymentSettings
from docchat_api.chat.prompts import MarkerCollisionError
from docchat_api.conversations import summary as summary_module
from docchat_api.conversations.model_calls import CallReply, SmallModel
from docchat_api.conversations.summary import (
    LoadedTurn,
    SummaryRebuilder,
    accepted_text,
    backoff,
    kept_verbatim,
    pick_fold_set,
    post_process,
    rebuild_due,
    sentence_cut,
    summary_data,
    summary_system,
)
from docchat_api.usage import QuotaCheck, UsageStoreUnavailable
from docchat_core.cosmos.containers import CONVERSATIONS
from docchat_core.cosmos.conversations import (
    InternalErrorCode,
    ModelCall,
    ModelCallUsage,
    ReasoningEffort,
    SessionDocument,
    SessionHistory,
    SessionSummary,
    SummaryJob,
)
from docchat_core.opsconfig import (
    API_AREAS,
    OpsConfig,
    OpsConfigStore,
    RowReader,
    SummaryOps,
    efforts_context,
)
from docchat_testing.builders.cosmos import session_document, turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import (
    DEFAULT_SUMMARY,
    PLAIN_TEXT,
    CharacterTokenizer,
    FakeModelClient,
    completion,
)
from docchat_testing.fakes.cosmos import FakeContainer

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TENANT = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
EFFORTS = frozenset({"none", "low", "medium"})
NONCE = "5c1e9a0d47b3f826"
TOKENIZER = CharacterTokenizer()
SMALL = ChatDeploymentSettings(
    deployment="chat-small",
    model="gpt-5.6-terra",
    model_version="2026-07-09",
    reasoning_effort=ReasoningEffort.NONE,
    prompt_cache="disabled",
    tokenizer=TOKENIZER,
)


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


class FakeRows(Protocol):
    rows: dict[str, str]


async def load_ops(fake_sql: FakeRows, **values: str) -> OpsConfig:
    """The seeded ops config with `values` (stored JSON text by key) changed."""
    fake_sql.rows.update(values)
    store = OpsConfigStore(cast("RowReader", fake_sql), API_AREAS, chat_small_efforts=EFFORTS)
    return await store.start()


def summary_ops(**values: int) -> SummaryOps:
    seeded: dict[str, object] = {
        "summary.trigger_tokens": 3_000,
        "summary.keep_recent_tokens": 1_200,
        "summary.max_input_tokens": 12_000,
        "summary.max_summary_tokens": 800,
        "summary.max_completion_tokens": 2_000,
        "summary.reasoning_effort": "low",
        "summary.timeout_seconds": 30,
        "summary.lease_seconds": 120,
        "summary.max_backoff_minutes": 60,
    }
    seeded.update({f"summary.{key}": value for key, value in values.items()})
    return SummaryOps.model_validate(seeded, context=efforts_context(EFFORTS))


def turns(*tokens: int) -> list[LoadedTurn]:
    return [
        LoadedTurn(seq, f"Question {seq}?", f"Answer {seq}.", count)
        for seq, count in enumerate(tokens, start=1)
    ]


class Quota:
    def __init__(self) -> None:
        self.allowed = True
        self.unavailable = False
        self.checks = 0

    async def __call__(self, oid: UUID) -> QuotaCheck:
        self.checks += 1
        if self.unavailable:
            raise UsageStoreUnavailable
        if self.allowed:
            return QuotaCheck(allowed=True, exceeded=None, retry_after_seconds=None)
        return QuotaCheck(allowed=False, exceeded="day", retry_after_seconds=60)


class Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[ModelCallUsage, dict[str, Any]]] = []

    async def _done(self) -> None:
        return None

    def __call__(self, oid: UUID, record: ModelCallUsage, **labels: Any) -> Any:
        self.records.append((record, labels))
        return self._done()


@dataclass
class World:
    clock: FakeClock
    conversations: FakeContainer
    model: FakeModelClient
    quota: Quota
    recorder: Recorder
    snapshot: OpsConfig
    user: UUID = field(default_factory=uuid4)
    session_id: UUID = field(default_factory=uuid4)

    @property
    def rebuilder(self) -> SummaryRebuilder:
        model = SmallModel(
            self.model.client(),
            SMALL,
            clock=self.clock,
            recorder=self.recorder,
            sleep=self.clock.sleep,
        )
        return SummaryRebuilder(
            self.conversations,
            model=model,
            tokenizer=TOKENIZER,
            tenant_id=TENANT,
            clock=self.clock,
            quota=self.quota,
        )

    @property
    def key(self) -> list[str]:
        return [str(self.user), str(self.session_id)]

    def seed(self, count: int = 9, tokens: int = 400, **session: Any) -> SessionDocument:
        """A session with `count` completed turns of `tokens` history tokens each."""
        values: dict[str, Any] = {
            "turn_count": count,
            "history": SessionHistory(unsummarised_tokens=count * tokens, unsummarised_turns=count),
            "created_at": START - timedelta(hours=1),
            "updated_at": START - timedelta(minutes=1),
            "last_turn_at": START - timedelta(minutes=1),
        }
        document = session_document(
            user_id=self.user, session_id=self.session_id, **(values | session)
        )
        self.conversations.put(document.to_item())
        for seq in range(1, count + 1):
            turn = turn_document(
                user_id=self.user,
                session_id=self.session_id,
                seq=seq,
                question=f"Question {seq}?",
                answer=f"Answer {seq} \N{MATHEMATICAL LEFT WHITE SQUARE BRACKET}1"
                "\N{MATHEMATICAL RIGHT WHITE SQUARE BRACKET}.",
                history_tokens=tokens,
                created_at=START - timedelta(minutes=30),
                ended_at=START - timedelta(minutes=30),
                updated_at=START - timedelta(minutes=30),
                deadline_at=START,
            )
            self.conversations.put(turn.to_item())
        return document

    def stored(self) -> SessionDocument:
        item = self.conversations.get(self.key, str(self.session_id))
        assert item is not None
        return SessionDocument.from_item(item)

    async def rebuild(self) -> str:
        item = self.conversations.get(self.key, str(self.session_id))
        assert item is not None
        return await self.rebuilder.rebuild(
            SessionDocument.from_item(item), str(item["_etag"]), self.snapshot
        )

    def on_replace(self, number: int, action: Callable[[], object]) -> None:
        """Run `action` before the `number`-th `replace_item` from now (1 is the claim)."""
        calls = 0

        def hook() -> None:
            nonlocal calls
            calls += 1
            if calls == number:
                action()

        self.conversations.before("replace_item", hook, times=number)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
async def world(fake_sql: FakeRows, clock: FakeClock) -> World:
    return World(
        clock=clock,
        conversations=FakeContainer.of(CONVERSATIONS, clock=clock),
        model=FakeModelClient(),
        quota=Quota(),
        recorder=Recorder(),
        snapshot=await load_ops(fake_sql),
    )


def span(captured: Captured) -> dict[str, Any]:
    (found,) = captured.finished_spans("docchat.summary.rebuild")
    return dict(found.attributes or {})


def status_error(status: int) -> Exception:
    response = cast("Any", type("R", (), {"status_code": status, "headers": {}, "request": None}))
    return openai.InternalServerError("down", response=response, body=None)


# --- Section 6.1: the trigger -------------------------------------------------------------------


def test_a_rebuild_is_due_only_above_the_trigger_without_a_lease_or_back_off() -> None:
    ops = summary_ops()
    above = session_document(history=SessionHistory(unsummarised_tokens=3_001))
    at = session_document(history=SessionHistory(unsummarised_tokens=3_000))
    leased = above.model_copy(
        update={"summary_job": SummaryJob(lease_expires_at=START + timedelta(seconds=1))}
    )
    backing_off = above.model_copy(
        update={"summary_job": SummaryJob(not_before=START + timedelta(minutes=1))}
    )
    expired = above.model_copy(
        update={
            "summary_job": SummaryJob(
                lease_expires_at=START - timedelta(seconds=1), not_before=START
            )
        }
    )

    assert rebuild_due(above, ops, START)
    assert not rebuild_due(at, ops, START)
    assert not rebuild_due(leased, ops, START)
    assert not rebuild_due(backing_off, ops, START)
    assert rebuild_due(expired, ops, START)


# --- T-308: the fold set -----------------------------------------------------------------------


async def test_t308_the_newest_turn_is_always_kept() -> None:
    fold = await pick_fold_set(
        turns(100, 100, 5_000), summary_tokens=0, ops=summary_ops(), tokenizer=TOKENIZER
    )

    assert [turn.seq for turn in fold.turns] == [1, 2]
    assert kept_verbatim(turns(5_000), 1_200) == 0


async def test_t308_keep_recent_has_no_turn_count_bound() -> None:
    fold = await pick_fold_set(
        turns(*[25] * 40), summary_tokens=0, ops=summary_ops(), tokenizer=TOKENIZER
    )

    assert fold.turns == ()


async def test_t308_the_spec_example_folds_the_six_oldest_turns() -> None:
    fold = await pick_fold_set(
        turns(*[400] * 9), summary_tokens=800, ops=summary_ops(), tokenizer=TOKENIZER
    )

    assert [turn.seq for turn in fold.turns] == [1, 2, 3, 4, 5, 6]
    assert (fold.folded_tokens, fold.input_tokens, fold.newest_seq) == (2_400, 2_400, 6)
    assert not fold.cut


async def test_t308_only_the_oldest_prefix_that_fits_is_folded() -> None:
    ops = summary_ops(trigger_tokens=1_000, keep_recent_tokens=0, max_input_tokens=2_000)

    fold = await pick_fold_set(
        turns(500, 500, 500, 500, 100), summary_tokens=800, ops=ops, tokenizer=TOKENIZER
    )

    assert [turn.seq for turn in fold.turns] == [1, 2]
    assert fold.folded_tokens == 1_000


async def test_t308_a_single_oversized_turn_is_cut_at_a_sentence_end() -> None:
    ops = summary_ops(trigger_tokens=1_000, keep_recent_tokens=0, max_input_tokens=2_000)
    answer = "A full sentence here. " * 200
    oversized = [LoadedTurn(1, "Q?", answer, 5_000), LoadedTurn(2, "Next?", "Yes.", 10)]

    fold = await pick_fold_set(oversized, summary_tokens=800, ops=ops, tokenizer=TOKENIZER)

    ((cut,),) = (fold.turns,)
    assert fold.cut
    assert cut.answer.endswith("here.")
    assert len("Q?") + len(cut.answer) <= 1_200
    assert answer.startswith(cut.answer)
    assert (fold.folded_tokens, fold.input_tokens) == (5_000, 2 + len(cut.answer))


async def test_an_empty_history_folds_nothing() -> None:
    fold = await pick_fold_set([], summary_tokens=0, ops=summary_ops(), tokenizer=TOKENIZER)

    assert fold.turns == ()


def test_the_sentence_cut() -> None:
    assert sentence_cut("One. Two. Three.", TOKENIZER, 16) == "One. Two. Three."
    assert sentence_cut("One. Two. Three.", TOKENIZER, 12) == "One. Two."
    assert sentence_cut("abcdefghij", TOKENIZER, 4) == "abcd"
    assert sentence_cut("Version 3.5 is out", TOKENIZER, 10) == "Version 3."
    exclaim, ask = "\N{FULLWIDTH EXCLAMATION MARK}", "\N{FULLWIDTH QUESTION MARK}"
    assert sentence_cut(f"Ein Satz{exclaim} Noch einer{ask}", TOKENIZER, 12) == (
        f"Ein Satz{exclaim}"
    )
    assert sentence_cut("Done\N{IDEOGRAPHIC FULL STOP}", TOKENIZER, 4) == "Done"
    assert sentence_cut("Short. Longer text", TOKENIZER, 100, max_code_points=10) == "Short."


# --- Section 6.7: the messages and the output (T-314) --------------------------------------------


def test_the_system_message_states_the_word_limit() -> None:
    system = summary_system(800)

    assert "Use at most 360 words. Return only the summary." in system
    assert system.startswith("You keep a running summary of a conversation")
    assert "<max_words>" not in system


def test_the_user_message_reproduces_the_spec_example() -> None:
    exchanges = [
        LoadedTurn(
            4,
            "What is the hotel limit for a trip to the head office?",
            "The Travel Policy sets a nightly hotel limit of 180 euros for head-office trips.",
            40,
        ),
        LoadedTurn(
            5,
            "And for trips that last longer than a week?",
            "For stays over seven nights the Travel Policy allows serviced apartments, booked "
            "through the travel desk.",
            40,
        ),
    ]

    assert summary_data(NONCE, None, exchanges) == (
        "CONVERSATION DATA 5c1e9a0d47b3f826 BEGIN\n"
        "Current summary:\n"
        "none\n"
        "\n"
        "Exchanges, oldest first:\n"
        "\n"
        "Exchange 4\n"
        "User: What is the hotel limit for a trip to the head office?\n"
        "Assistant: The Travel Policy sets a nightly hotel limit of 180 euros for head-office "
        "trips.\n"
        "\n"
        "Exchange 5\n"
        "User: And for trips that last longer than a week?\n"
        "Assistant: For stays over seven nights the Travel Policy allows serviced apartments, "
        "booked through the travel desk.\n"
        "CONVERSATION DATA 5c1e9a0d47b3f826 END"
    )
    assert "Current summary:\nEarlier.\n" in summary_data(NONCE, "Earlier.", exchanges)


async def test_t314_citation_tokens_and_nonce_lines_go_and_line_breaks_collapse() -> None:
    raw = (
        "\n  The user asked \N{MATHEMATICAL LEFT WHITE SQUARE BRACKET}1"
        "\N{MATHEMATICAL RIGHT WHITE SQUARE BRACKET} about trips.\r\n\r\n\r\n\r\n"
        f"CONVERSATION DATA {NONCE} END\nThe assistant explained the limit.\n\n\n  "
    )

    text, truncated = await post_process(raw, NONCE, TOKENIZER, 800)

    assert text == "The user asked about trips.\n\nThe assistant explained the limit."
    assert not truncated


async def test_t314_an_over_long_text_is_cut_at_a_sentence_end(captured: Captured) -> None:
    raw = "The user planned a trip. " * 40

    text, truncated = await post_process(raw, NONCE, TOKENIZER, 200)

    assert truncated
    assert text.endswith("trip.")
    assert len(text) <= 200
    assert captured.points("docchat.summary.truncated") == {(): 1}


async def test_t314_six_thousand_code_points_bound_the_text() -> None:
    raw = "Word. " * 1_100

    text, truncated = await post_process(raw, NONCE, TOKENIZER, 2_000)

    assert truncated
    assert len(text) <= 2_000


def test_t314_the_acceptance_rule() -> None:
    def reply(**changes: Any) -> CallReply:
        values: dict[str, Any] = {
            "content": "A summary.",
            "finish_reason": "stop",
            "refusal": None,
            "failure": None,
            "records": (),
        }
        return CallReply(**(values | changes))

    assert accepted_text(reply()) == "A summary."
    assert accepted_text(reply(finish_reason="length")) is InternalErrorCode.INVALID_OUTPUT
    assert accepted_text(reply(refusal="I can't.")) is InternalErrorCode.INVALID_OUTPUT
    assert accepted_text(reply(content=None)) is InternalErrorCode.INVALID_OUTPUT
    assert (
        accepted_text(reply(finish_reason="content_filter")) is InternalErrorCode.CONTENT_FILTERED
    )
    assert (
        accepted_text(reply(content=None, failure=InternalErrorCode.MODEL_UNAVAILABLE))
        is InternalErrorCode.MODEL_UNAVAILABLE
    )


def test_the_back_off_doubles_up_to_its_maximum() -> None:
    ops = summary_ops(max_backoff_minutes=60)

    assert [backoff(count, ops) for count in (1, 2, 3, 4)] == [
        timedelta(minutes=minutes) for minutes in (1, 2, 4, 8)
    ]
    assert backoff(7, ops) == timedelta(minutes=60)
    assert backoff(1_000, ops) == timedelta(minutes=60)


# --- Section 6.6: the rebuild ------------------------------------------------------------------


async def test_a_rebuild_folds_writes_the_summary_and_reduces_the_counters(
    world: World, captured: Captured
) -> None:
    world.seed()

    outcome = await world.rebuild()

    assert outcome == "succeeded"
    stored = world.stored()
    assert stored.summary.text == DEFAULT_SUMMARY
    assert stored.summary.tokens == len(DEFAULT_SUMMARY)
    assert (stored.summary.covers_through_seq, stored.summary.version) == (6, 1)
    assert stored.summary.deployment == "chat-small"
    assert stored.summary.updated_at == START
    assert stored.summary.last_usage is not None
    assert stored.summary.last_usage.call is ModelCall.SUMMARY
    assert stored.history == SessionHistory(unsummarised_tokens=1_200, unsummarised_turns=3)
    job = stored.summary_job
    assert (job.lease_expires_at, job.consecutive_failures, job.not_before) == (None, 0, None)
    assert (job.last_attempt_at, job.last_succeeded_at, job.last_error_code) == (
        START,
        START,
        None,
    )
    ((record, labels),) = world.recorder.records
    assert record.call is ModelCall.SUMMARY
    assert labels == {"session_id": world.session_id, "turn_id": None}
    attributes = span(captured)
    assert attributes["docchat.outcome"] == "succeeded"
    assert attributes["docchat.summary.folded_turns"] == 6
    assert attributes["docchat.summary.input_tokens"] == 2_400
    assert attributes["docchat.summary.output_tokens"] == len(DEFAULT_SUMMARY)
    assert "docchat.error_code" not in attributes
    assert captured.points("docchat.summary.rebuilds") == {(("outcome", "succeeded"),): 1}
    assert all("response_hook" in call.kwargs for call in world.conversations.calls)


async def test_the_request_holds_only_the_folded_turns_without_citation_tokens(
    world: World,
) -> None:
    world.seed(summary=SessionSummary(text="Earlier notes.", tokens=14, covers_through_seq=0))

    await world.rebuild()

    (request,) = world.model.text_requests()
    system, user = request["messages"]
    assert system["content"] == summary_system(800)
    data = user["content"]
    assert data.startswith("CONVERSATION DATA ")
    assert "Current summary:\nEarlier notes.\n" in data
    assert all(f"Exchange {seq}\n" in data for seq in range(1, 7))
    assert "Exchange 7" not in data
    assert "\N{MATHEMATICAL LEFT WHITE SQUARE BRACKET}" not in data
    nonce = data.split()[2]
    assert len(nonce) == 16
    assert data.endswith(f"CONVERSATION DATA {nonce} END")
    assert request["reasoning_effort"] == "low"
    assert request["max_completion_tokens"] == 2_000


async def test_a_claim_lost_to_another_instance_is_lease_taken(
    world: World, captured: Captured
) -> None:
    world.seed()
    lease = {"leaseExpiresAt": "2026-10-30T12:02:00.000Z", "consecutiveFailures": 0}
    world.on_replace(
        1, lambda: world.conversations.touch(world.key, str(world.session_id), summaryJob=lease)
    )

    outcome = await world.rebuild()

    assert outcome == "lease_taken"
    assert world.model.requests == []
    assert span(captured)["docchat.outcome"] == "lease_taken"
    assert captured.points("docchat.conversations.store_conflicts") == {
        (("operation", "summary"),): 1
    }


async def test_a_claim_conflict_that_leaves_the_session_due_claims_again(world: World) -> None:
    world.seed()
    world.on_replace(
        1,
        lambda: world.conversations.touch(
            world.key, str(world.session_id), updatedAt="2026-10-30T11:59:30.000Z"
        ),
    )

    assert await world.rebuild() == "succeeded"


async def test_nothing_to_fold_recomputes_stale_counters_with_the_release(
    world: World, captured: Captured
) -> None:
    world.seed(
        count=2,
        history=SessionHistory(unsummarised_tokens=5_000, unsummarised_turns=9),
        summary_job=SummaryJob(consecutive_failures=2),
    )

    outcome = await world.rebuild()

    assert outcome == "nothing_to_fold"
    assert world.model.requests == []
    stored = world.stored()
    assert stored.history == SessionHistory(unsummarised_tokens=800, unsummarised_turns=2)
    assert stored.summary_job.lease_expires_at is None
    assert stored.summary_job.consecutive_failures == 2
    assert stored.summary_job.last_attempt_at == START
    assert captured.points("docchat.summary.rebuilds") == {(("outcome", "nothing_to_fold"),): 1}


@pytest.mark.parametrize("unavailable", [False, True])
async def test_t313_a_quota_deferral_makes_no_call_and_releases_once(
    world: World, captured: Captured, unavailable: bool
) -> None:
    failures = SummaryJob(
        consecutive_failures=2,
        not_before=START - timedelta(minutes=1),
        last_error_code=InternalErrorCode.MODEL_UNAVAILABLE,
    )
    world.seed(summary_job=failures)
    world.quota.allowed = False
    world.quota.unavailable = unavailable

    outcome = await world.rebuild()

    assert outcome == "deferred_quota"
    assert world.model.requests == []
    assert world.quota.checks == 1
    job = world.stored().summary_job
    assert job.lease_expires_at is None
    assert (job.consecutive_failures, job.not_before, job.last_error_code) == (
        2,
        START - timedelta(minutes=1),
        InternalErrorCode.MODEL_UNAVAILABLE,
    )
    assert len(world.conversations.calls_of("replace_item")) == 2
    assert span(captured)["docchat.outcome"] == "deferred_quota"


async def test_t312_failures_back_off_one_two_and_four_minutes_and_a_success_resets(
    world: World, captured: Captured
) -> None:
    world.seed()
    for minutes in (1, 2, 4):
        world.model.replies[PLAIN_TEXT].extend([status_error(503), status_error(503)])

        assert await world.rebuild() == "failed"

        job = world.stored().summary_job
        assert job.lease_expires_at is None
        assert job.last_error_code is InternalErrorCode.MODEL_UNAVAILABLE
        assert job.not_before == world.clock.now() + timedelta(minutes=minutes)
        world.clock.advance_to(job.not_before)

    assert await world.rebuild() == "succeeded"

    job = world.stored().summary_job
    assert (job.consecutive_failures, job.not_before, job.last_error_code) == (0, None, None)
    assert captured.points("docchat.summary.rebuilds") == {
        (("outcome", "failed"),): 3,
        (("outcome", "succeeded"),): 1,
    }
    first = captured.finished_spans("docchat.summary.rebuild")[0]
    failed = dict(first.attributes or {})
    assert failed["docchat.error_code"] == "model_unavailable"


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (completion("Cut", finish_reason="length"), InternalErrorCode.INVALID_OUTPUT),
        (completion(None, refusal="I can't help."), InternalErrorCode.INVALID_OUTPUT),
        (completion("", finish_reason="content_filter"), InternalErrorCode.CONTENT_FILTERED),
        (completion(f"Only a {NONCE} line"), InternalErrorCode.INVALID_OUTPUT),
    ],
)
async def test_an_output_that_fails_acceptance_fails_the_rebuild(
    world: World, monkeypatch: pytest.MonkeyPatch, reply: Any, code: InternalErrorCode
) -> None:
    world.seed()
    monkeypatch.setattr(summary_module, "draw_marker", lambda _texts: NONCE)
    world.model.replies[PLAIN_TEXT].append(reply)

    assert await world.rebuild() == "failed"

    stored = world.stored()
    assert stored.summary_job.last_error_code is code
    assert stored.summary_job.consecutive_failures == 1
    assert stored.summary.text is None
    assert len(world.recorder.records) == 1


async def test_a_marker_that_keeps_colliding_fails_without_a_call(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.seed()

    def collide(_texts: object) -> str:
        raise MarkerCollisionError

    monkeypatch.setattr(summary_module, "draw_marker", collide)

    assert await world.rebuild() == "failed"
    assert world.model.requests == []
    assert world.stored().summary_job.last_error_code is InternalErrorCode.INVALID_OUTPUT


async def test_a_write_conflict_rereads_and_reapplies_on_the_fresh_counters(
    world: World, captured: Captured
) -> None:
    world.seed()
    later = {"unsummarisedTokens": 4_100, "unsummarisedTurns": 10}
    world.on_replace(
        2, lambda: world.conversations.touch(world.key, str(world.session_id), history=later)
    )

    assert await world.rebuild() == "succeeded"

    assert world.stored().history == SessionHistory(unsummarised_tokens=1_700, unsummarised_turns=4)
    assert captured.points("docchat.conversations.store_conflicts") == {
        (("operation", "summary"),): 1
    }


async def test_a_result_another_rebuild_overtook_is_discarded(world: World) -> None:
    world.seed()
    newer = {"text": "Newer.", "tokens": 6, "coversThroughSeq": 7, "version": 1}
    world.on_replace(
        2, lambda: world.conversations.touch(world.key, str(world.session_id), summary=newer)
    )

    assert await world.rebuild() == "lease_taken"

    stored = world.stored()
    assert stored.summary.text == "Newer."
    assert stored.summary.covers_through_seq == 7


async def test_write_conflicts_that_never_settle_fail_with_store_conflict(world: World) -> None:
    world.seed()
    calls = 0

    def concurrent_writer() -> None:
        nonlocal calls
        calls += 1
        if 2 <= calls <= 6:  # every write after the claim
            world.conversations.touch(
                world.key, str(world.session_id), updatedAt=f"2026-10-30T11:59:5{calls}.000Z"
            )

    world.conversations.before("replace_item", concurrent_writer, times=10)

    assert await world.rebuild() == "failed"

    job = world.stored().summary_job
    assert job.last_error_code is InternalErrorCode.STORE_CONFLICT
    assert (job.lease_expires_at, job.consecutive_failures) == (None, 1)
