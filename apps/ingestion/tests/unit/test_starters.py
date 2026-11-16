"""The three triggers and `ensure_dispatcher` (`14-ingestion.md` sections 3.4 to 3.6 and 4.10):
the queue starter (T-142), the keep-alive tick and its watchdog over the orchestration harness's
client double (T-155, and T-156's three cases with a stand-in dispatcher), and the daily
maintenance."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import azure.durable_functions as df
import pytest
from azure.core.exceptions import HttpResponseError

from docchat_core.ingestion.models import DISPATCHER_INSTANCE_ID
from docchat_core.telemetry import TestExporters
from docchat_ingestion import starters
from docchat_ingestion.activities import runtime
from docchat_ingestion.boundary import DispatcherUnavailableError
from docchat_ingestion.orchestrators.retry import OrchestrationContext
from docchat_testing.durable import OrchestrationHarness

RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
MESSAGE_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _events(exporters: TestExporters, name: str) -> list[dict[str, Any]]:
    return [
        dict(item.log_record.attributes or {})
        for item in exporters.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name") == name
    ]


def _records(exporters: TestExporters, name: str) -> list[dict[str, Any]]:
    return [
        dict(item.log_record.attributes or {})
        for item in exporters.logs.get_finished_logs()
        if item.log_record.body == name
    ]


def _gauges(exporters: TestExporters) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    found: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    data: Any = exporters.metrics.get_metrics_data()
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found[metric.name] = [
                    (point.value, dict(point.attributes or {})) for point in metric.data.data_points
                ]
    return found


# --- A stand-in dispatcher under the harness ---------------------------------------------------


class Dispatcher:
    """A dispatcher stand-in: each generation sets the custom status the test holds, then waits."""

    def __init__(self, harness: OrchestrationHarness) -> None:
        self.harness = harness
        self.status: dict[str, Any] | None = None
        self.starts: list[Any] = []

    def __call__(self, context: OrchestrationContext) -> Generator[Any, Any]:
        if not context.is_replaying:  # type: ignore[attr-defined]
            self.starts.append(context.get_input())
        if self.status is not None:
            context.set_custom_status(dict(self.status))
        yield context.wait_for_external_event("never")


def _harness() -> tuple[OrchestrationHarness, Dispatcher]:
    harness = OrchestrationHarness()
    dispatcher = Dispatcher(harness)
    harness.register_orchestrator("ingestion_dispatcher", dispatcher)
    return harness, dispatcher


def _status(harness: OrchestrationHarness, **overrides: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "generation": 4,
        "lastSweepAt": harness.clock.now().isoformat(),
        "outstanding": True,
        "queued": {"interactive": 2, "bulk": 7},
        "running": 1,
        "oldestQueuedAgeSeconds": 95,
        "configValid": True,
    }
    status.update(overrides)
    return status


async def _tick(harness: OrchestrationHarness) -> bool:
    client = cast(starters.DurableClient, harness.client())
    return await starters.keepalive_tick(client, clock=harness.clock, sleep=harness.clock.sleep)


@pytest.fixture
def no_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keep-alive never builds the process, so it can't open a SQL connection."""

    async def refused() -> Any:
        raise AssertionError("the keep-alive reached the process's resources")

    monkeypatch.setattr(runtime, "runtime", refused)
    monkeypatch.setattr(runtime, "build_process", refused)


async def test_ensure_dispatcher_starts_generation_0_once() -> None:
    harness, dispatcher = _harness()
    client = cast(starters.DurableClient, harness.client())

    assert await starters.ensure_dispatcher(client) is True
    assert await starters.ensure_dispatcher(client) is False
    assert dispatcher.starts == [{"generation": 0}]


async def test_ensure_dispatcher_replaces_an_ended_instance() -> None:
    harness, dispatcher = _harness()
    client = cast(starters.DurableClient, harness.client())
    await starters.ensure_dispatcher(client)
    await harness.terminate(DISPATCHER_INSTANCE_ID, "test")

    assert await starters.ensure_dispatcher(client) is True
    assert len(dispatcher.starts) == 2


async def test_a_lost_start_race_counts_as_started_by_another() -> None:
    class Racing:
        def __init__(self) -> None:
            self.reads = 0

        async def get_status(self, instance_id: str, *options: bool) -> Any:
            self.reads += 1
            running = SimpleNamespace(value="Running")
            return SimpleNamespace(runtime_status=None if self.reads == 1 else running)

        async def start_new(
            self, name: str, instance_id: str | None = None, client_input: Any = None
        ) -> str:
            raise RuntimeError("conflict")

    assert await starters.ensure_dispatcher(cast(starters.DurableClient, Racing())) is False


async def test_a_failed_start_without_a_winner_raises() -> None:
    class Failing:
        async def get_status(self, instance_id: str, *options: bool) -> Any:
            return None

        async def start_new(
            self, name: str, instance_id: str | None = None, client_input: Any = None
        ) -> str:
            raise RuntimeError("storage down")

    with pytest.raises(RuntimeError, match="storage down"):
        await starters.ensure_dispatcher(cast(starters.DurableClient, Failing()))


async def test_t155_the_keepalive_records_the_four_gauges(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, dispatcher = _harness()
    dispatcher.status = _status(harness)
    harness.clock.advance(120)

    assert await _tick(harness) is False

    gauges = _gauges(telemetry)
    assert gauges["docchat.ingestion.dispatcher.heartbeat_age"] == [(120.0, {})]
    assert sorted(gauges["docchat.ingestion.items.queued"], key=lambda pair: pair[0]) == [
        (2, {"priority": "interactive"}),
        (7, {"priority": "bulk"}),
    ]
    assert gauges["docchat.ingestion.items.running"] == [(1, {})]
    assert gauges["docchat.ingestion.queue.oldest_age"] == [(95, {})]


async def test_t155_null_values_are_skipped(telemetry: TestExporters, no_sql: None) -> None:
    harness, dispatcher = _harness()
    dispatcher.status = _status(harness, queued=None, running=None, oldestQueuedAgeSeconds=None)

    await _tick(harness)

    gauges = _gauges(telemetry)
    assert "docchat.ingestion.items.queued" not in gauges
    assert "docchat.ingestion.items.running" not in gauges
    assert "docchat.ingestion.queue.oldest_age" not in gauges


async def test_t155_t156_a_heartbeat_11_minutes_old_with_outstanding_work_restarts(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, dispatcher = _harness()
    dispatcher.status = _status(harness, outstanding=True)
    await _tick(harness)
    harness.clock.advance(11 * 60)

    assert await _tick(harness) is True

    assert dispatcher.starts == [{"generation": 0}, {"generation": 0}]
    assert _events(telemetry, "ingestion_dispatcher_restarted") == [
        {
            "microsoft.custom_event.name": "ingestion_dispatcher_restarted",
            "reason": "heartbeat_stale",
            "oldGeneration": 4,
        }
    ]


async def test_t155_t156_an_idle_dispatcher_restarts_only_after_75_minutes(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, dispatcher = _harness()
    dispatcher.status = _status(harness, outstanding=False, queued={"interactive": 0, "bulk": 0})
    await _tick(harness)

    harness.clock.advance(74 * 60)
    assert await _tick(harness) is False
    harness.clock.advance(2 * 60)
    assert await _tick(harness) is True
    assert len(dispatcher.starts) == 2


async def test_t155_a_dispatcher_waiting_for_valid_settings_is_left_running(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, dispatcher = _harness()
    dispatcher.status = _status(harness, configValid=False)
    await _tick(harness)
    harness.clock.advance(3 * 3600)

    assert await _tick(harness) is False
    assert len(dispatcher.starts) == 1


async def test_t156_a_slow_terminate_is_left_to_the_next_tick(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, dispatcher = _harness()
    harness.terminate_delay_seconds = 61
    dispatcher.status = _status(harness)
    await _tick(harness)
    harness.clock.advance(11 * 60)

    assert await _tick(harness) is False
    assert _events(telemetry, "ingestion_dispatcher_restarted") == []
    assert len(dispatcher.starts) == 1

    harness.clock.advance(5 * 60)
    await _tick(harness)
    assert len(dispatcher.starts) == 2


async def test_a_new_generation_without_a_custom_status_ages_from_its_last_update(
    telemetry: TestExporters, no_sql: None
) -> None:
    harness, _ = _harness()
    await _tick(harness)
    harness.clock.advance(11 * 60)

    assert await _tick(harness) is True
    (event,) = _events(telemetry, "ingestion_dispatcher_restarted")
    assert "oldGeneration" not in event


async def test_a_failed_tick_is_job_failed_and_returns(telemetry: TestExporters) -> None:
    class Broken:
        async def get_status(self, instance_id: str, *options: bool) -> Any:
            raise OSError("cnry-hub-0a1b2c3d")

    await starters.run_keepalive(cast(starters.DurableClient, Broken()))

    (record,) = _records(telemetry, "job_failed")
    assert record["error.type"] == "builtins.OSError"
    assert "cnry-" not in str(telemetry.logs.get_finished_logs())


def test_custom_status_text_is_decoded() -> None:
    status = SimpleNamespace(custom_status=json.dumps({"generation": 1}))
    assert starters._custom_status(status) == {"generation": 1}
    assert starters._custom_status(SimpleNamespace(custom_status="not json")) is None
    assert starters._instant("garbage") is None
    naive = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    assert starters._instant(naive) == datetime(2026, 1, 1, tzinfo=UTC)


def test_the_watchdog_needs_a_heartbeat() -> None:
    assert starters.heartbeat_stale(True, None, datetime(2026, 1, 1, tzinfo=UTC)) is False


# --- The queue starter -------------------------------------------------------------------------


class Client:
    """A Durable client double that records the starter's calls."""

    def __init__(
        self,
        *,
        status: str | None = None,
        raise_failure: Exception | None = None,
        status_failure: Exception | None = None,
    ) -> None:
        self.status = status
        self.raise_failure = raise_failure
        self.status_failure = status_failure
        self.started: list[tuple[str, str | None, Any]] = []
        self.raised: list[tuple[str, str, Any]] = []

    async def get_status(self, instance_id: str, *options: bool) -> Any:
        if self.status_failure is not None:
            raise self.status_failure
        if self.status is None:
            return SimpleNamespace(runtime_status=None)
        return SimpleNamespace(runtime_status=SimpleNamespace(value=self.status))

    async def start_new(
        self, name: str, instance_id: str | None = None, client_input: Any = None
    ) -> str:
        self.started.append((name, instance_id, client_input))
        self.status = "Running"
        return str(instance_id)

    async def raise_event(self, instance_id: str, event_name: str, event_data: Any = None) -> None:
        if self.raise_failure is not None:
            raise self.raise_failure
        self.raised.append((instance_id, event_name, event_data))

    async def terminate(self, instance_id: str, reason: str) -> None:
        raise AssertionError("the starter never terminates")


def _message(**overrides: Any) -> bytes:
    body: dict[str, Any] = {"runId": str(RUN), "traceparent": TRACEPARENT, "tracestate": "a=1"}
    body.update(overrides)
    return json.dumps(body).encode()


async def test_t142_a_message_starts_the_dispatcher_and_raises_run_queued(
    telemetry: TestExporters,
) -> None:
    client = Client()

    started = await starters.start_from_message(
        _message(), MESSAGE_ID, 1, cast(starters.DurableClient, client)
    )

    assert started is True
    assert client.started == [("ingestion_dispatcher", DISPATCHER_INSTANCE_ID, {"generation": 0})]
    assert client.raised == [(DISPATCHER_INSTANCE_ID, "run_queued", {"runId": str(RUN)})]
    (span,) = telemetry.spans.get_finished_spans()
    assert span.name == "docchat.ingestion.queue_message"
    assert span.kind.name == "CONSUMER"
    assert format(span.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    attributes = dict(span.attributes or {})
    assert attributes["docchat.run_id"] == str(RUN)
    assert attributes["docchat.ingestion.dequeue_count"] == 1
    assert attributes["docchat.ingestion.dispatcher_started"] is True


async def test_a_running_dispatcher_only_gets_the_hint(telemetry: TestExporters) -> None:
    client = Client(status="ContinuedAsNew")

    started = await starters.start_from_message(
        _message(tracestate=""), None, 3, cast(starters.DurableClient, client)
    )

    assert started is False
    assert client.started == []
    assert len(client.raised) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        _message(runId="cnry-run-0a1b2c3d"),
        _message(extra=1),
        _message(traceparent="00-x"),
    ],
)
async def test_t142_a_malformed_message_is_dropped_with_ingestion_message_invalid(
    telemetry: TestExporters, body: bytes
) -> None:
    client = Client()

    assert (
        await starters.start_from_message(body, MESSAGE_ID, 1, cast(starters.DurableClient, client))
        is False
    )

    assert _events(telemetry, "ingestion_message_invalid") == [
        {"microsoft.custom_event.name": "ingestion_message_invalid", "messageId": MESSAGE_ID}
    ]
    assert client.started == client.raised == []
    assert "cnry-" not in str(telemetry.logs.get_finished_logs())


async def test_a_malformed_message_without_an_id_still_emits_the_event(
    telemetry: TestExporters,
) -> None:
    await starters.start_from_message(b"{}", None, 1, cast(starters.DurableClient, Client()))

    assert len(_events(telemetry, "ingestion_message_invalid")) == 1


async def test_t142_a_durable_client_failure_is_logged_once_and_raised_without_a_cause(
    telemetry: TestExporters,
) -> None:
    client = Client(status_failure=OSError("cnry-hub-0a1b2c3d"))

    with pytest.raises(DispatcherUnavailableError) as raised:
        await starters.start_from_message(
            _message(), MESSAGE_ID, 2, cast(starters.DurableClient, client)
        )

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    (record,) = _records(telemetry, "job_failed")
    assert record == {"error.type": "builtins.OSError", "docchat.error_code": "internal_error"}
    exported = str(telemetry.logs.get_finished_logs()) + "".join(
        span.to_json() for span in telemetry.spans.get_finished_spans()
    )
    assert "cnry-" not in exported


@pytest.mark.parametrize(
    "message",
    [
        f"No instance with ID {DISPATCHER_INSTANCE_ID} found.",
        f"Instance with ID {DISPATCHER_INSTANCE_ID} is gone: either completed or failed",
    ],
)
async def test_a_gone_instance_answer_is_ignored(message: str) -> None:
    client = Client(status="Running", raise_failure=Exception(message))

    assert (
        await starters.start_from_message(
            _message(), MESSAGE_ID, 1, cast(starters.DurableClient, client)
        )
        is False
    )


async def test_another_raise_event_failure_is_dispatcher_unavailable() -> None:
    client = Client(status="Running", raise_failure=Exception("Webhook returned 500"))

    with pytest.raises(DispatcherUnavailableError):
        await starters.start_from_message(
            _message(), MESSAGE_ID, 1, cast(starters.DurableClient, client)
        )


# --- Daily maintenance -------------------------------------------------------------------------


class Purger:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure = failure

    async def purge_instance_history_by(self, **kwargs: Any) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(kwargs)


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


async def test_maintenance_runs_its_four_steps(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    open_canary = "_scanner-canary/9f506172-8394-4ea5-8fb6-d7e8f9a0b1c2.pdf"
    documents = fakes.FakeContainer(open_canary)
    sql = fakes.FakeSql(
        fakes.Rows(rowcount=500),
        fakes.Rows(rowcount=3),
        fakes.Rows(rowcount=0),
        fakes.Rows(rowcount=2),
        fakes.Rows(rowcount=0),
        fakes.Rows(
            [fakes.row(blob_path=open_canary), fakes.row(blob_path="_scanner-canary/absent.pdf")]
        ),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=0),
    )
    process = fakes.process(sql=sql, documents=documents, scanning_enabled=True)
    purger = Purger()

    await starters.run_maintenance(cast(starters.HistoryPurger, purger), _provider(process))

    (purge,) = purger.calls
    assert purge["created_time_from"] == datetime(2020, 1, 1, tzinfo=UTC)
    assert purge["created_time_to"] == process.clock.now() - timedelta(days=30)
    assert [status.value for status in purge["runtime_status"]] == [
        "Completed",
        "Failed",
        "Terminated",
    ]
    names = sql.names()
    assert names.count("maintenance_run_items") == 3
    assert names.count("maintenance_runs") == 2
    assert "maintenance_canary_write" in names
    assert names.count("maintenance_canaries") == 2
    executed = [item.sql for item in sql.connection.executed]
    assert "DELETE FROM app.ingestion_run_items" in executed[0]
    assert "TOP __[POSTCOMPILE_param_1]" in executed[0]
    assert "build_run_id = app.ingestion_runs.id" in executed[3]
    assert open_canary in documents.deleted
    assert len(documents.uploads) == 1
    assert _records(telemetry, "maintenance_failed") == []


async def test_a_failed_step_is_a_warning_and_the_next_step_runs(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    documents = fakes.FakeContainer("_scanner-canary/a.pdf")
    documents.delete_failure = HttpResponseError("refused")
    sql = fakes.FakeSql(
        fakes.Rows(rowcount=0),
        fakes.Rows(rowcount=0),
        fakes.Rows([fakes.row(blob_path="_scanner-canary/a.pdf")]),
    )
    process = fakes.process(sql=sql, documents=documents, scanning_enabled=False)

    purger = cast(starters.HistoryPurger, Purger(OSError("hub down")))
    await starters.run_maintenance(purger, _provider(process))

    assert [
        record["docchat.ingestion.step"] for record in _records(telemetry, "maintenance_failed")
    ] == [
        "durable_history",
        "canary_retention",
    ]
    assert "maintenance_canaries" not in sql.names()
    assert documents.uploads == []


async def test_maintenance_without_its_process_is_job_failed(telemetry: TestExporters) -> None:
    async def broken() -> Any:
        raise RuntimeError("no process")

    await starters.run_maintenance(cast(starters.HistoryPurger, Purger()), broken)

    assert len(_records(telemetry, "job_failed")) == 1


def test_the_triggers_are_registered_without_an_http_trigger() -> None:
    app = df.DFApp()
    app.register_functions(starters.bp)
    functions = {function.get_function_name(): function for function in app.get_functions()}

    assert set(functions) == {
        "ingestion_queue_starter",
        "ingestion_keepalive",
        "ingestion_maintenance",
    }
    bindings = {
        name: {binding.type for binding in function.get_bindings()}
        for name, function in functions.items()
    }
    assert bindings["ingestion_queue_starter"] == {"queueTrigger", "durableClient"}
    assert bindings["ingestion_keepalive"] == {"timerTrigger", "durableClient"}
    queue_binding: Any = next(
        binding
        for binding in functions["ingestion_queue_starter"].get_bindings()
        if binding.type == "queueTrigger"
    )
    assert queue_binding.queue_name == "ingestion-requests"
    assert queue_binding.connection == "DOCCHAT_INGESTION_QUEUE"
    timers: dict[str | None, Any] = {
        name: next(binding for binding in function.get_bindings() if binding.type == "timerTrigger")
        for name, function in functions.items()
        if name != "ingestion_queue_starter"
    }
    assert timers["ingestion_keepalive"].schedule == "0 */5 * * * *"
    assert timers["ingestion_maintenance"].schedule == "0 40 2 * * *"
    assert all(timer.run_on_startup is False for timer in timers.values())
