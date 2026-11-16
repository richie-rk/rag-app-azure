"""`OrchestrationHarness`, the in-process Durable Functions runtime of the tests (`84-testing.md`
section 2.12).

The ingestion orchestrators are generator functions of the Durable Functions Python SDK 1.7: they
yield tasks and receive results, and the host replays them from the start, feeding recorded
results back, every time new history arrives. The harness does the same in process, so a test
proves what the real host would do: it supplies the context members the orchestrators use,
records every scheduled action as history, runs activities (their real functions, or scripted
results and failures), fires timers on a virtual clock, and replays the generator from the start
after each completed task. Because every result a replay sees comes from history, a simulated
crash (an activity cut short, the in-memory state discarded) proves that no completed activity
runs a second time. `client()` is the Durable client double that the timer functions' bodies
(`ingestion_keepalive`, `ingestion_maintenance`, `ensure_dispatcher`) take; the harness runs on
the clock those functions take as their port, so their waits pass in virtual time.

The orchestrators generate no IDs (`new_uuid` is deliberately absent): the activity that writes a
row generates its ID and returns it.
"""

from __future__ import annotations

import inspect
import itertools
import json
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

# The SDK ships no `py.typed`; its two status types are used as plain values here.
from azure.durable_functions import OrchestrationRuntimeStatus
from azure.durable_functions.models.DurableOrchestrationStatus import (
    DurableOrchestrationStatus,
)

from docchat_testing.clock import FakeClock

type OrchestratorFunction = Callable[[HarnessContext], Any]
type ActivityFunction = Callable[[Any], Any]
type TaskKind = Literal["activity", "timer", "event", "sub", "any", "all"]
type EventKind = Literal[
    "ExecutionStarted",
    "OrchestratorStarted",
    "TaskScheduled",
    "TaskCompleted",
    "TaskFailed",
    "TaskRetried",
    "TimerCreated",
    "TimerFired",
    "SubOrchestrationCreated",
    "SubOrchestrationCompleted",
    "SubOrchestrationFailed",
    "EventRaised",
    "ExecutionCompleted",
    "ExecutionFailed",
    "ContinuedAsNew",
    "ExecutionTerminated",
]

MAX_STEPS: Final = 100_000
"""A bound on the work one `run_until_idle` does, so an orchestration that never waits fails the
test instead of hanging it."""

_SCHEDULE_EVENTS: Final[dict[str, EventKind]] = {
    "activity": "TaskScheduled",
    "timer": "TimerCreated",
    "sub": "SubOrchestrationCreated",
}
_COMPLETIONS: Final = frozenset(
    {
        "TaskCompleted",
        "TaskFailed",
        "TimerFired",
        "SubOrchestrationCompleted",
        "SubOrchestrationFailed",
    }
)
_ENDED: Final = frozenset(
    {
        OrchestrationRuntimeStatus.Completed,
        OrchestrationRuntimeStatus.Failed,
        OrchestrationRuntimeStatus.Terminated,
    }
)


class NonDeterminismError(Exception):
    """A replay scheduled something other than what the history records at that position."""


class ActivityFailedError(Exception):
    """Raised inside the orchestrator for a failed activity. As on the real host, it carries the
    failure as text, never the exception object, which can't cross the process boundary."""

    def __init__(self, name: str, details: str) -> None:
        super().__init__(f"Activity function '{name}' failed: {details}")
        self.activity = name
        self.details = details


class SubOrchestrationFailedError(Exception):
    """Raised inside the orchestrator for a failed sub-orchestration."""

    def __init__(self, name: str, details: str) -> None:
        super().__init__(f"Sub-orchestration '{name}' failed: {details}")
        self.orchestration = name
        self.details = details


class HarnessError(Exception):
    """The test asked the harness for something it can't do (an unregistered function, an
    orchestration that never waits)."""


class _SimulatedCrash(BaseException):
    """Unwinds an activity cut short by a simulated host crash."""


def _wire(value: Any) -> Any:
    """What crosses the host boundary: inputs and outputs travel as JSON."""
    return None if value is None else json.loads(json.dumps(value))


def _details(error: BaseException) -> str:
    kind = type(error)
    return f"{kind.__module__}.{kind.__qualname__}: {error}"


@dataclass(slots=True)
class HistoryEvent:
    """One history event of one generation of an instance."""

    kind: EventKind
    timestamp: datetime
    seq: int | None = None
    name: str | None = None
    payload: Any = None
    target: str | None = None
    """A sub-orchestration's instance ID."""
    failed: bool = False
    played: bool = False


# --- Tasks and the context ---------------------------------------------------------------------


@dataclass(eq=False, slots=True)
class HarnessTask:
    """A task an orchestrator yields. Once it completes in a replay, `result` holds its value
    (`task_any` returns the winning task itself, as the SDK does)."""

    kind: TaskKind
    name: str | None = None
    input: Any = None
    fire_at: datetime | None = None
    retry: Any = None
    instance_id: str | None = None
    children: tuple[HarnessTask, ...] = ()
    seq: int | None = None
    result: Any = None
    is_completed: bool = False
    is_faulted: bool = False


@dataclass(slots=True)
class _Outcome:
    ok: bool
    value: Any
    timestamp: datetime
    played: bool
    position: int


class HarnessContext:
    """The `DurableOrchestrationContext` members the orchestrators use: `call_activity`,
    `call_activity_with_retry`, `call_sub_orchestrator`, `create_timer`,
    `wait_for_external_event`, `task_any`, `task_all`, `continue_as_new`,
    `current_utc_datetime`, `is_replaying`, `instance_id`, `get_input` and
    `set_custom_status`."""

    def __init__(self, instance: _Instance, now: datetime) -> None:
        self._instance = instance
        self._current = instance.started_at
        self._now = now
        self._next_seq = 0
        self._event_counts: dict[str, int] = {}
        # The code before the first yield ran in an earlier episode unless this is the first.
        self.is_replaying = instance.episodes > 0
        self.custom_status: Any = instance.custom_status
        self.will_continue_as_new = False
        self.continue_input: Any = None

    @property
    def instance_id(self) -> str:
        return self._instance.instance_id

    @property
    def current_utc_datetime(self) -> datetime:
        """Deterministic: the time of the history the replay has reached, never the clock."""
        return self._current

    def get_input(self, expected_type: type | None = None) -> Any:
        del expected_type  # The SDK's decoding hint; the harness's input is already JSON.
        return _wire(self._instance.input)

    def set_custom_status(self, status: Any) -> None:
        self.custom_status = _wire(status)

    def continue_as_new(self, input_: Any) -> None:
        self.will_continue_as_new = True
        self.continue_input = _wire(input_)

    @staticmethod
    def _name(function: Any) -> str:
        # The v2 programming model passes the decorated function; its name is the activity's.
        return function if isinstance(function, str) else str(function.__name__)

    def call_activity(self, name: Any, input_: Any = None) -> HarnessTask:
        return HarnessTask("activity", name=self._name(name), input=_wire(input_))

    def call_activity_with_retry(
        self, name: Any, retry_options: Any, input_: Any = None
    ) -> HarnessTask:
        return HarnessTask(
            "activity", name=self._name(name), input=_wire(input_), retry=retry_options
        )

    def call_sub_orchestrator(
        self, name: Any, input_: Any = None, instance_id: str | None = None
    ) -> HarnessTask:
        return HarnessTask(
            "sub", name=self._name(name), input=_wire(input_), instance_id=instance_id
        )

    def create_timer(self, fire_at: datetime) -> HarnessTask:
        if fire_at.utcoffset() != timedelta(0):
            raise ValueError("timers fire at timezone-aware UTC instants")
        return HarnessTask("timer", fire_at=fire_at)

    def wait_for_external_event(self, name: str) -> HarnessTask:
        return HarnessTask("event", name=name)

    def task_any(self, activities: Iterable[HarnessTask]) -> HarnessTask:
        return HarnessTask("any", children=tuple(activities))

    def task_all(self, activities: Iterable[HarnessTask]) -> HarnessTask:
        return HarnessTask("all", children=tuple(activities))

    def schedule(self, task: HarnessTask) -> None:
        """Give a yielded task, or a composite's children, its position in the history."""
        if task.kind in {"any", "all"}:
            for child in task.children:
                self.schedule(child)
            return
        if task.seq is not None:
            return
        if task.kind == "event":
            # The k-th wait for an event takes the k-th raise of it, as on the host, where a wait
            # abandoned by `task_any` still claims the next event of its name.
            name = str(task.name)
            task.seq = self._event_counts.get(name, 0)
            self._event_counts[name] = task.seq + 1
            return
        task.seq = self._next_seq
        self._next_seq += 1
        self._instance.record_schedule(task, self._now)

    def _resolve_any(self, task: HarnessTask) -> _Outcome | None:
        # The first child to complete in history order wins; its task object is the result.
        finished = [
            (child, outcome)
            for child in task.children
            if (outcome := self.resolve(child)) is not None
        ]
        if not finished:
            return None
        winner, first = min(finished, key=lambda pair: pair[1].position)
        return _Outcome(True, winner, first.timestamp, first.played, first.position)

    def _resolve_all(self, task: HarnessTask) -> _Outcome | None:
        # The first failure in history order fails the whole; otherwise every child must finish.
        outcomes = [self.resolve(child) for child in task.children]
        settled = [outcome for outcome in outcomes if outcome is not None]
        failures = [outcome for outcome in settled if not outcome.ok]
        if failures:
            first = min(failures, key=lambda outcome: outcome.position)
            return _Outcome(False, first.value, first.timestamp, first.played, first.position)
        if len(settled) < len(outcomes):
            return None
        values = [outcome.value for outcome in settled]
        last = max(settled, key=lambda outcome: outcome.position, default=None)
        if last is None:
            return _Outcome(True, values, self._current, True, -1)
        return _Outcome(True, values, last.timestamp, last.played, last.position)

    def resolve(self, task: HarnessTask) -> _Outcome | None:
        """The task's outcome as the history holds it, or `None` while it is pending."""
        if task.kind == "any":
            return self._resolve_any(task)
        if task.kind == "all":
            return self._resolve_all(task)
        outcome = self._instance.outcome_of(task)
        if outcome is not None:
            task.is_completed = True
            task.is_faulted = not outcome.ok
            task.result = outcome.value if outcome.ok else None
        return outcome

    def consumed(self, outcome: _Outcome) -> None:
        self._current = max(self._current, outcome.timestamp)
        self.is_replaying = outcome.played


# --- Instances --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Instance:
    instance_id: str
    name: str
    input: Any
    created_at: datetime
    started_at: datetime
    order: int
    parent: tuple[str, int] | None = None
    history: list[HistoryEvent] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)
    status: OrchestrationRuntimeStatus = OrchestrationRuntimeStatus.Pending
    output: Any = None
    custom_status: Any = None
    episodes: int = 0
    needs_episode: bool = True
    terminate_at: datetime | None = None
    terminate_reason: str | None = None
    retries: dict[int, Any] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    retry_at: dict[int, datetime] = field(default_factory=dict)

    @property
    def last_updated(self) -> datetime:
        return self.history[-1].timestamp if self.history else self.created_at

    def record_schedule(self, task: HarnessTask, now: datetime) -> None:
        if task.seq is None:
            raise HarnessError("a task is scheduled with its position")
        kind = _SCHEDULE_EVENTS[task.kind]
        payload: Any = task.fire_at if task.kind == "timer" else task.input
        if task.retry is not None:
            self.retries[task.seq] = task.retry
        recorded = next(
            (
                event
                for event in self.history
                if event.seq == task.seq and event.kind in _SCHEDULE_EVENTS.values()
            ),
            None,
        )
        if recorded is not None:
            if (recorded.kind, recorded.name, recorded.payload) != (kind, task.name, payload):
                raise NonDeterminismError(
                    f"instance {self.instance_id}: position {task.seq} replayed as {kind} "
                    f"{task.name!r} but the history holds {recorded.kind} {recorded.name!r}"
                )
            return
        target = None
        if task.kind == "sub":
            target = task.instance_id or f"{self.instance_id}:{task.seq}"
        self.history.append(
            HistoryEvent(kind, now, seq=task.seq, name=task.name, payload=payload, target=target)
        )

    def outcome_of(self, task: HarnessTask) -> _Outcome | None:
        if task.kind == "event":
            raised = [
                (position, event)
                for position, event in enumerate(self.history)
                if event.kind == "EventRaised" and event.name == task.name
            ]
            index = task.seq or 0
            if index >= len(raised):
                return None
            position, event = raised[index]
            return _Outcome(True, _wire(event.payload), event.timestamp, event.played, position)
        for position, event in enumerate(self.history):
            if event.kind not in _COMPLETIONS or event.seq != task.seq:
                continue
            if not event.failed:
                return _Outcome(True, _wire(event.payload), event.timestamp, event.played, position)
            error: Exception = (
                SubOrchestrationFailedError(str(event.name), str(event.payload))
                if event.kind == "SubOrchestrationFailed"
                else ActivityFailedError(str(event.name), str(event.payload))
            )
            return _Outcome(False, error, event.timestamp, event.played, position)
        return None

    def completed_seqs(self) -> set[int]:
        return {
            event.seq
            for event in self.history
            if event.kind in _COMPLETIONS and event.seq is not None
        }


# --- The harness ------------------------------------------------------------------------------


class OrchestrationHarness:
    """Runs orchestrations in process, replaying them from history as the Durable host does."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self.terminate_delay_seconds = 0.0
        """A virtual delay before a `terminate` takes effect; 0 means at once."""
        self._orchestrators: dict[str, OrchestratorFunction] = {}
        self._activities: dict[str, ActivityFunction] = {}
        self._scripts: dict[str, list[Any]] = {}
        self._failures: dict[tuple[str, int], BaseException] = {}
        self._crashes: set[tuple[str, int]] = set()
        self._instances: dict[str, _Instance] = {}
        self._starts: dict[str, int] = {}
        self._completions: dict[str, int] = {}
        self._ids = itertools.count(1)

    # Registration and scripting.

    def register_orchestrator(self, name: str, function: OrchestratorFunction) -> None:
        self._orchestrators[name] = function

    def register_activity(self, name: str, function: ActivityFunction) -> None:
        """The activity's real function, called with its input; it may be a coroutine function."""
        self._activities[name] = function

    def script_activity(self, name: str, *outcomes: Any) -> None:
        """Results for the next calls of `name`, in order, before its real function runs; an
        exception instance is that call's failure."""
        self._scripts.setdefault(name, []).extend(outcomes)

    def fail_activity(self, name: str, *, call: int, error: BaseException) -> None:
        """Fail the `call`-th execution (1-based) of `name` with `error`."""
        self._failures[(name, call)] = error

    def crash_in_activity(self, name: str, *, call: int) -> None:
        """Simulate a host crash while the `call`-th execution of `name` runs: the activity has
        started, its completion never reaches the history, every instance's in-memory state is
        discarded, and the next run replays from history and runs the activity again."""
        self._crashes.add((name, call))

    def activity_starts(self, name: str) -> int:
        return self._starts.get(name, 0)

    def activity_completions(self, name: str) -> int:
        return self._completions.get(name, 0)

    # Instances.

    async def start(self, name: str, input_: Any = None, *, instance_id: str | None = None) -> str:
        """Create an instance of a registered orchestrator; `run_until_idle` drives it."""
        return self._create(name, input_, instance_id)

    def _create(
        self,
        name: str,
        input_: Any,
        instance_id: str | None,
        parent: tuple[str, int] | None = None,
    ) -> str:
        if name not in self._orchestrators:
            raise HarnessError(f"no orchestrator named {name!r} is registered")
        identifier = instance_id if instance_id is not None else f"instance-{next(self._ids)}"
        existing = self._instances.get(identifier)
        if existing is not None and existing.status not in _ENDED:
            raise HarnessError(f"instance {identifier} is already running")
        now = self.clock.now()
        instance = _Instance(
            instance_id=identifier,
            name=name,
            input=_wire(input_),
            created_at=now,
            started_at=now,
            order=len(self._instances),
            parent=parent,
        )
        instance.history.append(HistoryEvent("ExecutionStarted", now, payload=instance.input))
        self._instances[identifier] = instance
        return identifier

    def _instance(self, instance_id: str) -> _Instance:
        try:
            return self._instances[instance_id]
        except KeyError:
            raise HarnessError(f"no instance {instance_id}") from None

    def status(self, instance_id: str) -> OrchestrationRuntimeStatus:
        return self._instance(instance_id).status

    def output(self, instance_id: str) -> Any:
        return self._instance(instance_id).output

    def custom_status(self, instance_id: str) -> Any:
        return self._instance(instance_id).custom_status

    def history(self, instance_id: str) -> list[HistoryEvent]:
        """The current generation's history."""
        return list(self._instance(instance_id).history)

    def history_counts(self, instance_id: str) -> list[int]:
        """The number of history events of each generation, the current one last."""
        instance = self._instance(instance_id)
        return [*instance.generations, len(instance.history)]

    def orchestration_status(self, instance_id: str) -> DurableOrchestrationStatus:
        """The SDK's status object for an instance; falsy for an unknown one, as on the host."""
        instance = self._instances.get(instance_id)
        if instance is None:
            return DurableOrchestrationStatus()
        return DurableOrchestrationStatus(
            name=instance.name,
            instanceId=instance.instance_id,
            createdTime=instance.created_at.isoformat(),
            lastUpdatedTime=instance.last_updated.isoformat(),
            input=instance.input,
            output=instance.output,
            runtimeStatus=OrchestrationRuntimeStatus(instance.status.value),
            customStatus=instance.custom_status,
        )

    async def raise_event(self, instance_id: str, name: str, data: Any = None) -> None:
        instance = self._instance(instance_id)
        if instance.status in _ENDED:
            return
        instance.history.append(
            HistoryEvent("EventRaised", self.clock.now(), name=name, payload=_wire(data))
        )
        instance.needs_episode = True

    async def terminate(self, instance_id: str, reason: str) -> None:
        """Terminate an instance, at once or after `terminate_delay_seconds` of virtual time."""
        instance = self._instance(instance_id)
        if instance.status in _ENDED:
            return
        instance.terminate_at = self.clock.now() + timedelta(seconds=self.terminate_delay_seconds)
        instance.terminate_reason = reason
        self._apply_terminations()

    def crash(self) -> None:
        """Discard the in-memory state of every running instance: the next run replays each from
        its history, as a new worker does after a host restart."""
        for instance in self._running():
            instance.needs_episode = True

    # Driving.

    async def advance(self, seconds: float) -> None:
        """Move the virtual clock, then run whatever became due."""
        self.clock.advance(seconds)
        await self.run_until_idle()

    async def run_until_idle(self, *, max_completions: int | None = None) -> int:
        """Run episodes and due work until every instance waits on time or an event, or has
        ended; returns the number of tasks completed. `max_completions` stops early, for a test
        that crashes the host part-way."""
        completed = 0
        for _ in range(MAX_STEPS):
            self._apply_terminations()
            for instance in self._running():
                if instance.needs_episode:
                    self._episode(instance)
            if any(instance.needs_episode for instance in self._running()):
                continue
            if max_completions is not None and completed >= max_completions:
                return completed
            if not await self._step():
                return completed
            completed += 1
        raise HarnessError("the orchestrations never became idle")

    async def run_to_completion(
        self, instance_id: str, *, max_virtual_seconds: float = 7 * 86_400
    ) -> OrchestrationRuntimeStatus:
        """Run, moving the clock to each next due timer or retry, until the instance ends, waits
        only on an external event, or the time budget is spent."""
        deadline = self.clock.now() + timedelta(seconds=max_virtual_seconds)
        while True:
            await self.run_until_idle()
            instance = self._instance(instance_id)
            due = self._next_due()
            if instance.status in _ENDED or due is None or due > deadline:
                return instance.status
            self.clock.advance_to(due)

    def _running(self) -> list[_Instance]:
        return sorted(
            (instance for instance in self._instances.values() if instance.status not in _ENDED),
            key=lambda instance: instance.order,
        )

    def _apply_terminations(self) -> None:
        now = self.clock.now()
        for instance in self._running():
            if instance.terminate_at is not None and instance.terminate_at <= now:
                instance.history.append(
                    HistoryEvent("ExecutionTerminated", now, payload=instance.terminate_reason)
                )
                instance.status = OrchestrationRuntimeStatus.Terminated
                instance.output = instance.terminate_reason
                self._report_to_parent(instance)

    def _next_due(self) -> datetime | None:
        moments: list[datetime] = []
        for instance in self._running():
            moments.extend(instance.retry_at.values())
            if instance.terminate_at is not None:
                moments.append(instance.terminate_at)
            completed = instance.completed_seqs()
            moments.extend(
                event.payload
                for event in instance.history
                if event.kind == "TimerCreated" and event.seq not in completed
            )
        return min(moments, default=None)

    def _episode(self, instance: _Instance) -> None:
        """One replay of the orchestrator from the start against its history."""
        now = self.clock.now()
        instance.history.append(HistoryEvent("OrchestratorStarted", now))
        context = HarnessContext(instance, now)
        instance.status = OrchestrationRuntimeStatus.Running
        instance.needs_episode = False
        try:
            produced = self._orchestrators[instance.name](context)
            if inspect.isgenerator(produced):
                self._drive(instance, context, produced)
            else:
                self._finish(instance, context, produced)
        except (NonDeterminismError, HarnessError):
            raise
        except Exception as error:  # noqa: BLE001 - an orchestrator's failure ends its instance
            instance.history.append(
                HistoryEvent("ExecutionFailed", now, payload=_details(error), failed=True)
            )
            instance.status = OrchestrationRuntimeStatus.Failed
            instance.output = _details(error)
            self._report_to_parent(instance)
        finally:
            instance.custom_status = context.custom_status
            instance.episodes += 1
            for event in instance.history:
                event.played = True

    def _drive(
        self, instance: _Instance, context: HarnessContext, generator: Generator[Any, Any, Any]
    ) -> None:
        sending: Any = None
        throwing: BaseException | None = None
        while True:
            try:
                yielded = generator.send(sending) if throwing is None else generator.throw(throwing)
            except StopIteration as stop:
                self._finish(instance, context, stop.value)
                return
            if not isinstance(yielded, HarnessTask):
                raise HarnessError(f"orchestrator {instance.name} yielded {yielded!r}, not a task")
            context.schedule(yielded)
            outcome = context.resolve(yielded)
            if outcome is None:
                self._start_children(instance)
                return
            context.consumed(outcome)
            sending, throwing = (outcome.value, None) if outcome.ok else (None, outcome.value)

    def _finish(self, instance: _Instance, context: HarnessContext, result: Any) -> None:
        now = self.clock.now()
        if context.will_continue_as_new:
            instance.history.append(
                HistoryEvent("ContinuedAsNew", now, payload=context.continue_input)
            )
            instance.generations.append(len(instance.history))
            instance.history = [
                HistoryEvent("ExecutionStarted", now, payload=context.continue_input)
            ]
            instance.input = context.continue_input
            instance.started_at = now
            instance.retries.clear()
            instance.attempts.clear()
            instance.retry_at.clear()
            instance.episodes = 0
            instance.needs_episode = True
            return
        instance.output = _wire(result)
        instance.history.append(HistoryEvent("ExecutionCompleted", now, payload=instance.output))
        instance.status = OrchestrationRuntimeStatus.Completed
        self._report_to_parent(instance)

    def _start_children(self, instance: _Instance) -> None:
        completed = instance.completed_seqs()
        for event in instance.history:
            if event.kind != "SubOrchestrationCreated" or event.seq in completed:
                continue
            if event.target is not None and event.target not in self._instances:
                self._create(
                    str(event.name),
                    event.payload,
                    event.target,
                    (instance.instance_id, event.seq if event.seq is not None else -1),
                )

    def _report_to_parent(self, child: _Instance) -> None:
        if child.parent is None:
            return
        parent_id, seq = child.parent
        parent = self._instances.get(parent_id)
        if parent is None or parent.status in _ENDED:
            return
        failed = child.status is not OrchestrationRuntimeStatus.Completed
        parent.history.append(
            HistoryEvent(
                "SubOrchestrationFailed" if failed else "SubOrchestrationCompleted",
                self.clock.now(),
                seq=seq,
                name=child.name,
                payload=child.output,
                failed=failed,
            )
        )
        parent.needs_episode = True

    async def _step(self) -> bool:
        """Complete one piece of due work, a timer or an activity; `False` when none is due."""
        now = self.clock.now()
        for instance in self._running():
            completed = instance.completed_seqs()
            for event in list(instance.history):
                if event.seq is None or event.seq in completed:
                    continue
                if event.kind == "TimerCreated" and event.payload <= now:
                    instance.history.append(HistoryEvent("TimerFired", now, seq=event.seq))
                    instance.needs_episode = True
                    return True
                if event.kind == "TaskScheduled":
                    due = instance.retry_at.get(event.seq)
                    if due is None or due <= now:
                        await self._execute(instance, event)
                        return True
        return False

    async def _execute(self, instance: _Instance, scheduled: HistoryEvent) -> None:
        name = str(scheduled.name)
        seq = scheduled.seq if scheduled.seq is not None else -1
        call = self._starts.get(name, 0) + 1
        self._starts[name] = call
        instance.retry_at.pop(seq, None)
        try:
            value = await self._invoke(name, call, scheduled.payload)
        except _SimulatedCrash:
            self.crash()
            return
        except Exception as error:  # noqa: BLE001 - an activity's failure becomes history
            attempts = instance.attempts.get(seq, 0) + 1
            instance.attempts[seq] = attempts
            retry = instance.retries.get(seq)
            if retry is not None and attempts < int(retry.max_number_of_attempts):
                interval = timedelta(milliseconds=int(retry.first_retry_interval_in_milliseconds))
                instance.retry_at[seq] = self.clock.now() + interval
                instance.history.append(
                    HistoryEvent(
                        "TaskRetried", self.clock.now(), name=name, payload=_details(error)
                    )
                )
                return
            instance.history.append(
                HistoryEvent(
                    "TaskFailed",
                    self.clock.now(),
                    seq=seq,
                    name=name,
                    payload=_details(error),
                    failed=True,
                )
            )
        else:
            self._completions[name] = self._completions.get(name, 0) + 1
            instance.history.append(
                HistoryEvent("TaskCompleted", self.clock.now(), seq=seq, name=name, payload=value)
            )
        instance.needs_episode = True

    async def _invoke(self, name: str, call: int, payload: Any) -> Any:
        if (name, call) in self._crashes:
            self._crashes.discard((name, call))
            raise _SimulatedCrash
        failure = self._failures.pop((name, call), None)
        if failure is not None:
            raise failure
        script = self._scripts.get(name)
        if script:
            outcome = script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return _wire(outcome)
        function = self._activities.get(name)
        if function is None:
            raise HarnessError(f"no activity named {name!r} is registered or scripted")
        result = function(_wire(payload))
        if inspect.isawaitable(result):
            result = await result
        return _wire(result)

    def client(self) -> DurableClientDouble:
        return DurableClientDouble(self)


class DurableClientDouble:
    """The Durable client the timer functions use, over the harness's instances. Each call lets
    the harness catch up first, so a status read after a virtual wait sees what became due."""

    def __init__(self, harness: OrchestrationHarness) -> None:
        self._harness = harness

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: Any = None,
    ) -> str:
        identifier = await self._harness.start(
            orchestration_function_name, client_input, instance_id=instance_id
        )
        await self._harness.run_until_idle()
        return identifier

    async def get_status(self, instance_id: str, *_options: bool) -> DurableOrchestrationStatus:
        """`runtime_status` is `Running` while the generator waits on a task and `Terminated`
        once a terminate has taken effect; `custom_status` and `last_updated_time` as the host
        reports them."""
        await self._harness.run_until_idle()
        return self._harness.orchestration_status(instance_id)

    async def raise_event(
        self, instance_id: str, event_name: str, event_data: Any = None, *_hub: str | None
    ) -> None:
        await self._harness.raise_event(instance_id, event_name, event_data)
        await self._harness.run_until_idle()

    async def terminate(self, instance_id: str, reason: str) -> None:
        await self._harness.terminate(instance_id, reason)
        await self._harness.run_until_idle()
