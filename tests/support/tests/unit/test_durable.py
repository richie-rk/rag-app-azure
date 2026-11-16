"""`OrchestrationHarness`: T-568, the orchestration harness unit test (`84-testing.md` section
2.12), and the context members, retries, events, sub-orchestrations and client double."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.durable_functions import (
    OrchestrationRuntimeStatus,
    RetryOptions,
)

from docchat_testing.clock import FakeClock
from docchat_testing.durable import (
    ActivityFailedError,
    HarnessContext,
    HarnessError,
    HarnessTask,
    NonDeterminismError,
    OrchestrationHarness,
)

START = datetime(2026, 3, 1, 9, tzinfo=UTC)
RUNNING = OrchestrationRuntimeStatus.Running
COMPLETED = OrchestrationRuntimeStatus.Completed
FAILED = OrchestrationRuntimeStatus.Failed
TERMINATED = OrchestrationRuntimeStatus.Terminated

type Orchestration = Generator[HarnessTask, Any, Any]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def harness(clock: FakeClock) -> OrchestrationHarness:
    return OrchestrationHarness(clock)


def pipeline(context: HarnessContext) -> Orchestration:
    """Three activities in a row, then a fan-out of two, recording what each replay sees."""
    document = context.get_input()["document"]
    parsed = yield context.call_activity("parse", document)
    chunks = yield context.call_activity("chunk", parsed)
    vectors = yield context.task_all([context.call_activity("embed", chunk) for chunk in chunks])
    yield context.call_activity("publish", vectors)
    return {"published": len(vectors)}


def _register_pipeline(harness: OrchestrationHarness, log: list[str]) -> None:
    def parse(document: str) -> str:
        log.append("parse")
        return f"parsed:{document}"

    async def chunk(parsed: str) -> list[str]:
        log.append("chunk")
        return [f"{parsed}#1", f"{parsed}#2"]

    def embed(chunk: str) -> list[float]:
        log.append("embed")
        return [float(len(chunk))]

    def publish(vectors: list[list[float]]) -> int:
        log.append("publish")
        return len(vectors)

    harness.register_orchestrator("pipeline", pipeline)
    for name, function in (
        ("parse", parse),
        ("chunk", chunk),
        ("embed", embed),
        ("publish", publish),
    ):
        harness.register_activity(name, function)


# T-568


async def test_t568_a_crash_replays_identically_and_reruns_no_completed_activity(
    harness: OrchestrationHarness,
) -> None:
    log: list[str] = []
    _register_pipeline(harness, log)
    harness.crash_in_activity("embed", call=2)

    instance = await harness.start("pipeline", {"document": "d1"})
    await harness.run_until_idle()

    assert harness.status(instance) is COMPLETED
    assert harness.output(instance) == {"published": 2}
    # The interrupted embed ran twice; every activity that completed ran exactly once.
    assert log == ["parse", "chunk", "embed", "embed", "publish"]
    assert harness.activity_starts("embed") == 3
    assert {name: harness.activity_completions(name) for name in ("parse", "chunk", "publish")} == {
        "parse": 1,
        "chunk": 1,
        "publish": 1,
    }
    assert harness.activity_completions("embed") == 2


async def test_t568_a_crash_between_steps_replays_from_history(
    harness: OrchestrationHarness,
) -> None:
    log: list[str] = []
    _register_pipeline(harness, log)
    instance = await harness.start("pipeline", {"document": "d2"})

    assert await harness.run_until_idle(max_completions=2) == 2
    scheduled_before = [e.name for e in harness.history(instance) if e.kind == "TaskScheduled"]
    harness.crash()
    await harness.run_until_idle()

    assert log == ["parse", "chunk", "embed", "embed", "publish"]
    assert harness.output(instance) == {"published": 2}
    scheduled_after = [e.name for e in harness.history(instance) if e.kind == "TaskScheduled"]
    assert scheduled_after[: len(scheduled_before)] == scheduled_before


async def test_t568_every_completed_task_triggers_a_replay_from_the_start(
    harness: OrchestrationHarness,
) -> None:
    starts: list[bool] = []

    def counting(context: HarnessContext) -> Orchestration:
        starts.append(context.is_replaying)
        first = yield context.call_activity("step", 1)
        second = yield context.call_activity("step", first)
        return second

    harness.register_orchestrator("counting", counting)
    harness.register_activity("step", lambda value: value + 1)

    instance = await harness.start("counting")
    await harness.run_until_idle()

    assert harness.output(instance) == 3
    # One episode to schedule the first step and one after each of the two completions.
    assert starts == [False, True, True]


async def test_t568_virtual_timers_fire_when_the_clock_reaches_them(
    harness: OrchestrationHarness, clock: FakeClock
) -> None:
    seen: list[datetime] = []

    def sleeper(context: HarnessContext) -> Orchestration:
        yield context.create_timer(context.current_utc_datetime + timedelta(minutes=10))
        seen.append(context.current_utc_datetime)
        return "woke"

    harness.register_orchestrator("sleeper", sleeper)
    instance = await harness.start("sleeper")
    await harness.run_until_idle()

    assert harness.status(instance) is RUNNING
    await harness.advance(599)
    assert harness.status(instance) is RUNNING
    await harness.advance(1)
    assert harness.status(instance) is COMPLETED
    assert seen[-1] == START + timedelta(minutes=10)
    assert clock.now() == START + timedelta(minutes=10)


async def test_t568_history_events_are_counted_per_generation(
    harness: OrchestrationHarness,
) -> None:
    def eternal(context: HarnessContext) -> Orchestration:
        round_ = context.get_input()
        yield context.call_activity("tick", round_)
        if round_ < 2:
            context.continue_as_new(round_ + 1)
            return None
        return "done"

    harness.register_orchestrator("eternal", eternal)
    harness.register_activity("tick", lambda value: value)

    instance = await harness.start("eternal", 0)
    await harness.run_until_idle()

    assert harness.output(instance) == "done"
    # ExecutionStarted, two OrchestratorStarted, TaskScheduled, TaskCompleted, then
    # ContinuedAsNew or ExecutionCompleted: six events in each of the three generations.
    assert harness.history_counts(instance) == [6, 6, 6]


# The context and the replay


async def test_a_replay_that_schedules_something_else_is_non_deterministic(
    harness: OrchestrationHarness,
) -> None:
    calls = 0

    def drifting(context: HarnessContext) -> Orchestration:
        nonlocal calls
        calls += 1
        yield context.call_activity("a" if calls == 1 else "b")
        return None

    harness.register_orchestrator("drifting", drifting)
    harness.register_activity("a", lambda _: None)
    harness.register_activity("b", lambda _: None)
    await harness.start("drifting")

    with pytest.raises(NonDeterminismError, match="position 0 replayed as TaskScheduled 'b'"):
        await harness.run_until_idle()


async def test_a_failed_activity_is_thrown_into_the_orchestrator(
    harness: OrchestrationHarness,
) -> None:
    def careful(context: HarnessContext) -> Orchestration:
        try:
            yield context.call_activity("flaky", "x")
        except ActivityFailedError as error:
            return error.details
        return "no failure"

    harness.register_orchestrator("careful", careful)
    harness.register_activity("flaky", lambda value: value)
    harness.fail_activity("flaky", call=1, error=TimeoutError("slow"))

    instance = await harness.start("careful")
    await harness.run_until_idle()

    assert harness.output(instance) == "builtins.TimeoutError: slow"


async def test_an_unhandled_failure_fails_the_instance(harness: OrchestrationHarness) -> None:
    def careless(context: HarnessContext) -> Orchestration:
        yield context.call_activity("broken")
        return None

    harness.register_orchestrator("careless", careless)
    harness.script_activity("broken", LookupError("gone"))

    instance = await harness.start("careless")
    await harness.run_until_idle()

    assert harness.status(instance) is FAILED
    assert "Activity function 'broken' failed: builtins.LookupError: gone" in str(
        harness.output(instance)
    )


async def test_retries_wait_their_interval_in_virtual_time(
    harness: OrchestrationHarness, clock: FakeClock
) -> None:
    def retrying(context: HarnessContext) -> Orchestration:
        return (
            yield context.call_activity_with_retry(
                "flaky",
                RetryOptions(first_retry_interval_in_milliseconds=5_000, max_number_of_attempts=3),
                "x",
            )
        )

    harness.register_orchestrator("retrying", retrying)
    harness.script_activity("flaky", ConnectionError("1"), ConnectionError("2"), "ok")

    instance = await harness.start("retrying")
    status = await harness.run_to_completion(instance)

    assert status is COMPLETED
    assert harness.output(instance) == "ok"
    assert harness.activity_starts("flaky") == 3
    assert clock.now() == START + timedelta(seconds=10)


async def test_retries_give_up_after_the_last_attempt(harness: OrchestrationHarness) -> None:
    def retrying(context: HarnessContext) -> Orchestration:
        return (
            yield context.call_activity_with_retry(
                "flaky",
                RetryOptions(first_retry_interval_in_milliseconds=1_000, max_number_of_attempts=2),
            )
        )

    harness.register_orchestrator("retrying", retrying)
    harness.script_activity("flaky", ConnectionError("1"), ConnectionError("2"), "never")

    instance = await harness.start("retrying")

    assert await harness.run_to_completion(instance) is FAILED
    assert harness.activity_starts("flaky") == 2


async def test_external_events_and_task_any(harness: OrchestrationHarness) -> None:
    def waiting(context: HarnessContext) -> Orchestration:
        timeout = context.create_timer(context.current_utc_datetime + timedelta(hours=1))
        approval = context.wait_for_external_event("approved")
        winner = yield context.task_any([approval, timeout])
        if winner is timeout:
            return "timed out"
        return winner.result

    harness.register_orchestrator("waiting", waiting)
    approved = await harness.start("waiting")
    expired = await harness.start("waiting")
    await harness.run_until_idle()

    await harness.raise_event(approved, "approved", {"by": "carol"})
    await harness.run_until_idle()
    await harness.advance(3_600)

    assert harness.output(approved) == {"by": "carol"}
    assert harness.output(expired) == "timed out"


async def test_task_all_fails_with_the_first_failure(harness: OrchestrationHarness) -> None:
    def fan_out(context: HarnessContext) -> Orchestration:
        try:
            yield context.task_all([context.call_activity("work", n) for n in range(3)])
        except ActivityFailedError as error:
            return error.activity
        return None

    harness.register_orchestrator("fan_out", fan_out)
    harness.script_activity("work", 0, ValueError("bad"), 2)

    instance = await harness.start("fan_out")
    await harness.run_until_idle()

    assert harness.output(instance) == "work"


async def test_sub_orchestrations_run_and_report_back(harness: OrchestrationHarness) -> None:
    def child(context: HarnessContext) -> Orchestration:
        value = yield context.call_activity("double", context.get_input())
        return value

    def parent(context: HarnessContext) -> Orchestration:
        results = yield context.task_all(
            [context.call_sub_orchestrator("child", n) for n in (1, 2)]
            + [context.call_sub_orchestrator("child", 5, instance_id="named-child")]
        )
        return results

    harness.register_orchestrator("child", child)
    harness.register_orchestrator("parent", parent)
    harness.register_activity("double", lambda value: value * 2)

    instance = await harness.start("parent", instance_id="parent-1")
    await harness.run_until_idle()

    assert harness.output(instance) == [2, 4, 10]
    assert harness.status("parent-1:0") is COMPLETED
    assert harness.output("named-child") == 10


async def test_inputs_and_outputs_cross_as_json(harness: OrchestrationHarness) -> None:
    def echo(context: HarnessContext) -> Orchestration:
        value = yield context.call_activity("echo", (1, 2))
        return value

    harness.register_orchestrator("echo", echo)
    harness.register_activity("echo", lambda value: {"seen": value})

    instance = await harness.start("echo")
    await harness.run_until_idle()

    assert harness.output(instance) == {"seen": [1, 2]}
    with pytest.raises(TypeError):
        await harness.start("echo", object())


async def test_unknown_functions_are_refused(harness: OrchestrationHarness) -> None:
    def calls_missing(context: HarnessContext) -> Orchestration:
        yield context.call_activity("missing")

    harness.register_orchestrator("calls_missing", calls_missing)

    with pytest.raises(HarnessError, match="no orchestrator named 'nope'"):
        await harness.start("nope")
    instance = await harness.start("calls_missing")
    await harness.run_until_idle()
    assert "no activity named 'missing'" in str(harness.output(instance))


# The client double


async def test_the_client_reports_status_custom_status_and_update_time(
    harness: OrchestrationHarness,
) -> None:
    def dispatcher(context: HarnessContext) -> Orchestration:
        context.set_custom_status({"phase": "waiting"})
        yield context.wait_for_external_event("work")
        context.set_custom_status({"phase": "done"})
        return None

    harness.register_orchestrator("dispatcher", dispatcher)
    client = harness.client()

    instance = await client.start_new("dispatcher", instance_id="dispatcher")
    running = await client.get_status(instance)
    await harness.advance(30)
    await client.raise_event(instance, "work", None)
    done = await client.get_status(instance)

    assert running.runtime_status is RUNNING
    assert running.custom_status == {"phase": "waiting"}
    assert running.instance_id == "dispatcher"
    assert done.runtime_status is COMPLETED
    assert done.custom_status == {"phase": "done"}
    assert done.last_updated_time == START + timedelta(seconds=30)
    assert not await client.get_status("no-such-instance")


async def test_a_terminate_takes_effect_at_once_or_after_its_virtual_delay(
    harness: OrchestrationHarness, clock: FakeClock
) -> None:
    def forever(context: HarnessContext) -> Orchestration:
        yield context.wait_for_external_event("never")

    harness.register_orchestrator("forever", forever)
    client = harness.client()
    at_once = await client.start_new("forever")
    delayed = await client.start_new("forever")

    await client.terminate(at_once, "replaced")
    harness.terminate_delay_seconds = 20
    await client.terminate(delayed, "replaced")

    assert (await client.get_status(at_once)).runtime_status is TERMINATED
    assert (await client.get_status(delayed)).runtime_status is RUNNING
    await clock.sleep(20)
    assert (await client.get_status(delayed)).runtime_status is TERMINATED


async def test_a_plain_function_orchestrator_completes_at_once(
    harness: OrchestrationHarness,
) -> None:
    harness.register_orchestrator("plain", lambda context: context.instance_id)

    instance = await harness.start("plain", instance_id="p")
    await harness.run_until_idle()

    assert harness.output(instance) == "p"


async def test_timers_need_utc_instants(harness: OrchestrationHarness) -> None:
    def naive(context: HarnessContext) -> Orchestration:
        yield context.create_timer(datetime(2026, 3, 1))  # noqa: DTZ001 - the refused case

    harness.register_orchestrator("naive", naive)

    instance = await harness.start("naive")
    await harness.run_until_idle()

    assert harness.status(instance) is FAILED
    assert "ValueError: timers fire at timezone-aware UTC instants" in str(harness.output(instance))
