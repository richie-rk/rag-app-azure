"""The dispatcher through the orchestration harness (`14-ingestion.md` section 5; ADRs 0034 to
0036; T-151 to T-154). The three dispatcher activities are stand-ins over a small in-memory
ledger that counts slots as the claim does in SQL; the item sub-orchestrations are stand-ins that
run a while and then record their outcome."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from docchat_core.ingestion.models import (
    DISPATCHER_INSTANCE_ID,
    ClaimedItem,
    ItemOperation,
    RunPriority,
    item_instance_id,
)
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_ingestion.orchestrators import ingestion_dispatcher as dispatcher
from docchat_ingestion.orchestrators.retry import OrchestrationContext
from docchat_testing.durable import OrchestrationHarness

OK: dict[str, Any] = {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}
FAILED: dict[str, Any] = {
    "ok": False,
    "errorCode": "internal_error",
    "retryable": True,
    "retryAfterSeconds": None,
}
RUN_ID = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")


def _settings(**overrides: int) -> dict[str, Any]:
    values = {
        "ingestion.max_parallel_items": 1,
        "ingestion.active_wake_seconds": 60,
        "ingestion.idle_wake_minutes": 60,
        "ingestion.scan_wait_minutes": 240,
        "ingestion.stale_upload_hours": 24,
        "ingestion.orphan_pending_minutes": 15,
        "ingestion.cleanup_retry_minutes": 15,
        "ingestion.max_item_claims": 3,
        "ingestion.retry_max_attempts": 7,
        "ingestion.retry_first_delay_seconds": 15,
        "ingestion.retry_max_delay_seconds": 480,
        "ingestion.activity_time_budget_seconds": 1200,
        "ingestion.max_pages_per_document": 2000,
        "ingestion.pagewise_pages_per_extract": 40,
        "ingestion.max_parse_seconds_per_document": 7200,
        "ingestion.embedding_batch_max_inputs": 64,
        "ingestion.embedding_batch_max_tokens": 60000,
        "ingestion.embedding_concurrency": 2,
        "ingestion.index_batch_max_documents": 100,
        "ingestion.index_batch_max_bytes": 8000000,
        "ingestion.table_summary_concurrency": 4,
        "ingestion.table_summary_max_input_tokens": 6000,
        "ingestion.table_summary_max_output_tokens": 300,
        "ingestion.run_retention_days": 180,
        "ingestion.durable_history_retention_days": 30,
    }
    for name, value in overrides.items():
        values[f"ingestion.{name}"] = value
    return IngestionOpsConfig.model_validate(values).model_dump(mode="json")


@dataclass
class Ledger:
    """The SQL side the stand-ins share: queued items, running slots, and what was asked."""

    clock_now: Any
    settings: dict[str, Any] | None = field(default_factory=_settings)
    valid: bool = True
    load_ok: bool = True
    sweep_ok: bool = True
    outstanding: bool = False
    queued: list[tuple[UUID, int]] = field(default_factory=list)
    running: dict[str, int] = field(default_factory=dict)
    claims: dict[str, int] = field(default_factory=dict)
    max_running: int = 0
    loads: list[datetime] = field(default_factory=list)
    sweeps: list[dict[str, Any]] = field(default_factory=list)
    claim_inputs: list[dict[str, Any]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    item_seconds: float = 120.0

    def load(self, payload: Any) -> dict[str, Any]:
        assert payload == {}
        self.loads.append(self.clock_now())
        if not self.load_ok:
            return FAILED
        if not self.valid:
            return {**OK, "valid": False, "settings": None}
        return {**OK, "valid": True, "settings": self.settings}

    def sweep(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sweeps.append(payload)
        if not self.sweep_ok:
            return FAILED
        expired, self.expired = self.expired, []
        for item_id in expired:
            self.running.pop(item_id, None)
        interactive = sum(1 for _, priority in self.queued if priority == 0)
        bulk = len(self.queued) - interactive
        return {
            **OK,
            "sweptAt": self.clock_now().isoformat(),
            "requeuedItemIds": expired,
            "failedItemIds": [],
            "hasOutstandingWork": self.outstanding or bool(self.queued) or bool(self.running),
            "queued": {"interactive": interactive, "bulk": bulk},
            "running": len(self.running),
            "oldestQueuedAgeSeconds": 30 if self.queued else None,
        }

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.claim_inputs.append(payload)
        free = payload["maxParallel"] - len(self.running)
        items: list[dict[str, Any]] = []
        while free > 0 and self.queued:
            item_id, priority = self.queued.pop(0)
            claim = self.claims.get(str(item_id), 0) + 1
            self.claims[str(item_id)] = claim
            self.running[str(item_id)] = claim
            free -= 1
            items.append(
                ClaimedItem(
                    item_id=item_id,
                    claim_number=claim,
                    instance_id=item_instance_id(item_id, claim),
                    operation=ItemOperation.INGEST,
                    priority=RunPriority(priority),
                    run_id=RUN_ID,
                    traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                ).model_dump(mode="json")
            )
        self.max_running = max(self.max_running, len(self.running))
        return {**OK, "items": items}

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.running.pop(payload["itemId"], None)
        return OK


def _harness(ledger_args: dict[str, Any] | None = None) -> tuple[OrchestrationHarness, Ledger]:
    harness = OrchestrationHarness()
    ledger = Ledger(clock_now=harness.clock.now, **(ledger_args or {}))

    def item(context: OrchestrationContext) -> Generator[Any, Any]:
        given = context.get_input()
        if ledger.item_seconds:
            yield context.create_timer(
                context.current_utc_datetime + timedelta(seconds=ledger.item_seconds)
            )
        yield context.call_activity("record_item_outcome", {"itemId": given["itemId"]})

    harness.register_orchestrator(dispatcher.NAME, dispatcher.ingestion_dispatcher)
    harness.register_orchestrator(dispatcher.DOCUMENT_ORCHESTRATOR, item)
    harness.register_orchestrator(dispatcher.INDEX_ORCHESTRATOR, item)
    harness.register_activity(dispatcher.LOAD_SETTINGS, ledger.load)
    harness.register_activity(dispatcher.SWEEP, ledger.sweep)
    harness.register_activity(dispatcher.CLAIM, ledger.claim)
    harness.register_activity("record_item_outcome", ledger.record)
    return harness, ledger


async def _start(harness: OrchestrationHarness, settings: dict[str, Any] | None = None) -> str:
    instance = await harness.start(
        dispatcher.NAME,
        {"generation": 0, "settings": settings},
        instance_id=DISPATCHER_INSTANCE_ID,
    )
    await harness.run_until_idle()
    return instance


async def _advance(harness: OrchestrationHarness, seconds: float) -> None:
    """Move the virtual clock, then give the event loop a turn: the harness replays
    synchronously, and a long test must not hold the loop past the guard's 100 ms."""
    await harness.advance(seconds)
    await asyncio.sleep(0)


def _timer_waits(harness: OrchestrationHarness, instance: str) -> list[float]:
    events = harness.history(instance)
    waits: list[float] = []
    for index, event in enumerate(events):
        if event.kind == "TimerCreated":
            started = next(
                e.timestamp for e in reversed(events[:index]) if e.kind == "OrchestratorStarted"
            )
            waits.append((event.payload - started).total_seconds())
    return waits


def _queue(ledger: Ledger, count: int, priority: int = 0) -> None:
    ledger.queued.extend((uuid4(), priority) for _ in range(count))


async def test_t151_the_dispatcher_never_exceeds_its_slots() -> None:
    harness, ledger = _harness({"settings": _settings(max_parallel_items=2)})
    _queue(ledger, 7)
    instance = await _start(harness)

    for _ in range(40):
        await _advance(harness, 30)

    assert ledger.max_running == 2
    assert ledger.queued == []
    assert ledger.running == {}
    assert all(item["maxParallel"] == 2 for item in ledger.claim_inputs)
    assert all(item["generation"] == 0 for item in ledger.claim_inputs)
    assert harness.status(instance).value == "Running"


async def test_t151_a_finished_sub_orchestration_wakes_the_dispatcher_at_once() -> None:
    harness, ledger = _harness({"item_seconds": 10.0})
    _queue(ledger, 1)
    await _start(harness)

    await _advance(harness, 10)

    started = ledger.loads[0]
    assert [(moment - started).total_seconds() for moment in ledger.loads] == [0, 10]


async def test_t151_the_run_queued_waiter_is_reused_until_it_completes() -> None:
    harness, ledger = _harness()
    instance = await _start(harness)
    await _advance(harness, 3600)
    assert len(ledger.loads) == 2

    await harness.raise_event(instance, dispatcher.RUN_QUEUED, {"runId": str(RUN_ID)})
    await harness.run_until_idle()

    assert len(ledger.loads) == 3
    await harness.raise_event(instance, dispatcher.RUN_QUEUED, {"runId": str(RUN_ID)})
    await harness.run_until_idle()
    assert len(ledger.loads) == 4


async def test_t151_waits_60_seconds_when_busy_and_60_minutes_when_idle() -> None:
    harness, ledger = _harness({"outstanding": True})
    instance = await _start(harness)
    await _advance(harness, 60)
    ledger.outstanding = False
    await _advance(harness, 60)
    await _advance(harness, 3600)

    assert _timer_waits(harness, instance) == [60, 60, 3600, 3600]


async def test_t151_the_custom_status_holds_every_field_identically_on_replay() -> None:
    harness, ledger = _harness({"settings": _settings(max_parallel_items=1)})
    _queue(ledger, 2, priority=1)
    _queue(ledger, 1, priority=0)
    instance = await _start(harness)

    status = harness.custom_status(instance)
    assert status == {
        "generation": 0,
        "lastSweepAt": ledger.sweeps and harness.clock.now().isoformat(),
        "outstanding": True,
        "queued": {"interactive": 1, "bulk": 1},
        "running": 1,
        "oldestQueuedAgeSeconds": 30,
        "configValid": True,
    }
    harness.crash()
    await harness.run_until_idle()

    assert harness.custom_status(instance) == status
    assert len(ledger.loads) == 1


async def test_a_failed_sweep_waits_60_seconds_and_claims_nothing() -> None:
    harness, ledger = _harness({"sweep_ok": False, "outstanding": False})
    _queue(ledger, 1)
    instance = await _start(harness)

    assert ledger.claim_inputs == []
    assert _timer_waits(harness, instance) == [60]
    status = harness.custom_status(instance)
    assert status["outstanding"] is True
    assert status["queued"] is None
    assert status["lastSweepAt"] is None
    assert status["configValid"] is True


async def test_an_expired_lease_stops_the_wait_for_its_orphan() -> None:
    harness, ledger = _harness({"item_seconds": 100_000.0})
    _queue(ledger, 1)
    instance = await _start(harness)
    (orphan,) = ledger.running
    ledger.expired = [orphan]

    await _advance(harness, 60)

    # The orphan left the running set uncounted, so the idle cadence returns.
    assert _timer_waits(harness, instance)[-1] == 3600


def test_t152_continue_as_new_after_60_wakes_with_the_last_valid_snapshot() -> None:
    # Sixty wakes of replay run on a loop of their own, off the test's guarded loop (see T-153).
    async def scenario() -> tuple[OrchestrationHarness, Ledger, str]:
        harness, ledger = _harness({"settings": _settings(idle_wake_minutes=5)})
        instance = await _start(harness)
        for _ in range(60):
            await harness.advance(300)
        return harness, ledger, instance

    harness, ledger, instance = asyncio.run(scenario())

    counts = harness.history_counts(instance)
    assert len(counts) == 2
    (started, *_) = harness.history(instance)
    assert started.payload == {"generation": 1, "settings": ledger.settings}
    assert ledger.sweeps[-1]["generation"] == 1
    assert len([item for item in ledger.claim_inputs if item["generation"] == 0]) == 60


def test_t152_a_draining_generation_waits_for_its_running_items_and_claims_nothing() -> None:
    async def scenario() -> None:
        harness, ledger = _harness({"item_seconds": 3600.0})
        instance = await _start(harness, _settings())
        for _ in range(58):
            await harness.advance(3600)
        _queue(ledger, 1)
        await harness.advance(3600)
        assert ledger.running
        claims_before_draining = len(ledger.claim_inputs)
        _queue(ledger, 1)

        await harness.advance(60)

        assert len(ledger.claim_inputs) == claims_before_draining
        assert len(harness.history_counts(instance)) == 1
        for _ in range(60):
            await harness.advance(60)
        assert len(harness.history_counts(instance)) == 2
        (started, *_) = harness.history(instance)
        assert started.payload["generation"] == 1

    asyncio.run(scenario())


class _Task:
    def __init__(self) -> None:
        self.is_completed = False


class _Context:
    """A minimal context that lets a test complete several sub-orchestrations in one wake, which
    the harness, finishing one task per step, never does."""

    def __init__(self, given: dict[str, Any]) -> None:
        self.given = given
        self.current_utc_datetime = datetime(2026, 1, 1, tzinfo=UTC)
        self.instance_id = DISPATCHER_INSTANCE_ID
        self.subs: list[_Task] = []
        self.continued: Any = None
        self.statuses: list[Any] = []

    def get_input(self) -> Any:
        return self.given

    def call_activity(self, name: str, input_: Any = None) -> Any:
        return (name, input_)

    def call_sub_orchestrator(
        self, name: str, input_: Any = None, instance_id: str | None = None
    ) -> _Task:
        task = _Task()
        self.subs.append(task)
        return task

    def create_timer(self, fire_at: datetime) -> _Task:
        return _Task()

    def wait_for_external_event(self, name: str) -> _Task:
        return _Task()

    def task_any(self, activities: Any) -> list[Any]:
        return list(activities)

    def continue_as_new(self, input_: Any) -> None:
        self.continued = input_

    def set_custom_status(self, status: Any) -> None:
        self.statuses.append(status)


def _claimed_pair() -> list[dict[str, Any]]:
    items = []
    for _ in range(2):
        item_id = uuid4()
        items.append(
            ClaimedItem(
                item_id=item_id,
                claim_number=1,
                instance_id=item_instance_id(item_id, 1),
                operation=ItemOperation.INGEST,
                priority=RunPriority.INTERACTIVE,
                run_id=RUN_ID,
            ).model_dump(mode="json")
        )
    return items


SWEEP_RESULT: dict[str, Any] = {
    **OK,
    "sweptAt": "2026-01-01T00:00:00+00:00",
    "requeuedItemIds": [],
    "failedItemIds": [],
    "hasOutstandingWork": True,
    "queued": {"interactive": 2, "bulk": 0},
    "running": 0,
    "oldestQueuedAgeSeconds": 1,
}


def _drive_until_reset(context: _Context, settings: dict[str, Any]) -> int:
    """Run the dispatcher's wakes by hand, completing every running sub-orchestration in each
    wake, until it continues as new; the number of claims it made."""
    generator = dispatcher.ingestion_dispatcher(context)
    claims = 0
    task = next(generator)
    try:
        while True:
            assert task[0] == dispatcher.LOAD_SETTINGS
            generator.send({**OK, "valid": True, "settings": settings})
            task = generator.send(SWEEP_RESULT)
            if isinstance(task, tuple) and task[0] == dispatcher.CLAIM:
                claims += 1
                task = generator.send({**OK, "items": _claimed_pair()})
            for sub in context.subs:
                sub.is_completed = True
            task = generator.send(task[-1])
    except StopIteration:
        return claims


def test_t152_continue_as_new_after_100_completed_items() -> None:
    settings = _settings(max_parallel_items=2)
    context = _Context({"generation": 4, "settings": settings})

    claims = _drive_until_reset(context, settings)

    assert claims == 50
    assert context.continued == {"generation": 5, "settings": settings}


def test_t153_every_generation_of_seven_idle_then_busy_days_stays_small() -> None:
    # The harness replays history synchronously, so the week runs on a loop of its own off the
    # test's guarded loop.
    async def week() -> list[int]:
        harness, ledger = _harness({"settings": _settings(active_wake_seconds=300)})
        instance = await _start(harness)
        elapsed = 0
        while elapsed < 6 * 86_400:
            await harness.advance(3600)
            elapsed += 3600
        ledger.outstanding = True
        while elapsed < 7 * 86_400:
            await harness.advance(300)
            elapsed += 300
        return harness.history_counts(instance)

    counts = asyncio.run(week())

    assert len(counts) > 2
    assert max(counts) < 1000


async def test_t154_an_invalid_value_keeps_the_last_valid_snapshot() -> None:
    harness, ledger = _harness({"settings": _settings(max_parallel_items=3)})
    instance = await _start(harness)
    ledger.valid = False
    ledger.settings = _settings(max_parallel_items=9)

    await _advance(harness, 3600)

    assert ledger.sweeps[-1]["settings"] == _settings(max_parallel_items=3)
    assert ledger.claim_inputs[-1]["maxParallel"] == 3
    assert harness.custom_status(instance)["configValid"] is True


async def test_t154_with_no_valid_snapshot_the_wake_sweeps_and_claims_nothing() -> None:
    harness, ledger = _harness({"valid": False})
    _queue(ledger, 1)
    instance = await _start(harness)

    assert ledger.sweeps == []
    assert ledger.claim_inputs == []
    assert harness.custom_status(instance)["configValid"] is False
    assert _timer_waits(harness, instance) == [60]

    ledger.valid = True
    await _advance(harness, 60)
    assert len(ledger.claim_inputs) == 1
    assert harness.custom_status(instance)["configValid"] is True


async def test_a_failed_load_keeps_the_snapshot_from_the_input() -> None:
    harness, ledger = _harness({"load_ok": False})
    await _start(harness, _settings(max_parallel_items=4))

    assert ledger.claim_inputs[-1]["maxParallel"] == 4


def test_items_go_to_the_orchestrator_of_their_operation() -> None:
    for operation, expected in [
        (ItemOperation.INGEST, dispatcher.DOCUMENT_ORCHESTRATOR),
        (ItemOperation.CLEANUP_DOCUMENT, dispatcher.DOCUMENT_ORCHESTRATOR),
        (ItemOperation.RERENDER, dispatcher.DOCUMENT_ORCHESTRATOR),
        (ItemOperation.CREATE_PHYSICAL_INDEX, dispatcher.INDEX_ORCHESTRATOR),
        (ItemOperation.DELETE_PHYSICAL_INDEX, dispatcher.INDEX_ORCHESTRATOR),
        (ItemOperation.PURGE_INDEX_BLOBS, dispatcher.INDEX_ORCHESTRATOR),
    ]:
        item_id = uuid4()
        claim = ClaimedItem(
            item_id=item_id,
            claim_number=2,
            instance_id=item_instance_id(item_id, 2),
            operation=operation,
            priority=RunPriority.BULK,
            run_id=RUN_ID,
        )
        assert dispatcher.orchestrator_for(claim) == expected


async def test_a_sub_orchestration_gets_its_instance_id_and_item_input() -> None:
    harness, ledger = _harness()
    _queue(ledger, 1)
    await _start(harness)

    (item_id, claim) = next(iter(ledger.running.items()))
    instance = f"docchat-item-{item_id}-{claim}"
    (started, *_) = harness.history(instance)
    assert started.payload["itemId"] == item_id
    assert started.payload["claimNumber"] == 1
    assert started.payload["operation"] == "ingest"
    assert started.payload["retry"] == {
        "maxAttempts": 7,
        "firstDelaySeconds": 15,
        "maxDelaySeconds": 480,
    }
    assert started.payload["settings"]["activityTimeBudgetSeconds"] == 1200
    assert started.payload["traceparent"].startswith("00-4bf92f35")


async def test_a_broken_input_fails_the_instance_without_its_values() -> None:
    harness, _ = _harness()
    instance = await harness.start(
        dispatcher.NAME, {"generation": -1, "cnry": "cnry-input-0a1b2c3d"}, instance_id="x"
    )
    await harness.run_until_idle()

    assert harness.status(instance).value == "Failed"
    assert "cnry-" not in str(harness.output(instance))
    assert "dispatcher invariant broken" in str(harness.output(instance))


@pytest.mark.parametrize(
    ("sweep", "claimed", "running", "expected"),
    [
        (None, 0, 0, {"queued": None, "running": None, "outstanding": True}),
        (
            {"queued": {"interactive": 2, "bulk": 0}, "running": 1, "hasOutstandingWork": False},
            1,
            1,
            {"queued": {"interactive": 1, "bulk": 0}, "running": 2, "outstanding": True},
        ),
        (
            {"queued": None, "running": None, "hasOutstandingWork": False},
            0,
            0,
            {"queued": None, "running": None, "outstanding": False},
        ),
    ],
)
def test_custom_status_is_built_from_recorded_outputs(
    sweep: dict[str, Any] | None, claimed: int, running: int, expected: dict[str, Any]
) -> None:
    item_id = uuid4()
    claims = [
        ClaimedItem(
            item_id=item_id,
            claim_number=1,
            instance_id=item_instance_id(item_id, 1),
            operation=ItemOperation.INGEST,
            priority=RunPriority.INTERACTIVE,
            run_id=RUN_ID,
        )
    ][:claimed]
    status = dispatcher.custom_status(
        generation=3,
        last_sweep_at="2026-01-01T00:00:00+00:00",
        sweep=sweep,
        claimed=claims,
        running=running,
        config_valid=True,
    )
    assert {key: status[key] for key in expected} == expected
    assert status["generation"] == 3


def test_a_missing_snapshot_is_null_input() -> None:
    assert dispatcher.snapshot_input(None) is None


def test_settle_counts_finished_tasks() -> None:
    @dataclass
    class Task:
        is_completed: bool

    running: dict[str, Any] = {"a": Task(True), "b": Task(False), "c": Task(True)}
    assert dispatcher.settle(running) == 2
    assert list(running) == ["b"]


def test_cancel_uses_the_timer_cancel_when_present() -> None:
    cancelled: list[bool] = []

    class Timer:
        def cancel(self) -> None:
            cancelled.append(True)

    dispatcher._cancel(Timer())
    dispatcher._cancel(object())
    assert cancelled == [True]
