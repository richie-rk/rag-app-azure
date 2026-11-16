"""The ingestion app's three triggers and `ensure_dispatcher()` (`14-ingestion.md` sections 3.4 to
3.6, 4.10 and 5.5; ADRs 0035 to 0039 and 0144).

- `ingestion_queue_starter` turns a queue message into a hint: it makes sure the dispatcher runs
  and raises `run_queued`. The message's run is in SQL already, so a lost, duplicated or replayed
  message only delays or repeats a wake.
- `ingestion_keepalive` runs every 5 minutes, makes sure the dispatcher runs, records the four
  dispatcher gauges from its custom status and restarts a dispatcher whose heartbeat is stale. It
  reads Durable status only and never opens a SQL connection, so an idle serverless database can
  pause.
- `ingestion_maintenance` runs at 02:40 UTC: Durable history purge, run retention, the daily
  scanner canary and canary retention, each step on its own.

Every body runs inside the failure boundary; only the queue starter re-raises, and only for its
Durable client calls, so the message is retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import azure.durable_functions as df
import azure.functions as func
from azure.durable_functions import OrchestrationRuntimeStatus
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind
from pydantic import ValidationError
from sqlalchemy import Connection, delete, select

from docchat_core.ingestion.models import DISPATCHER_INSTANCE_ID, IngestionQueueMessage
from docchat_core.models import Clock, SystemClock
from docchat_core.sql.tables.ingestion import (
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
)
from docchat_core.telemetry import (
    LOGGER_NAME,
    emit_event,
    log_event,
    qualified_type_name,
    set_attributes,
    start_span,
)
from docchat_core.telemetry.instruments import gauge
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.scan_results import delete_absent_ok, write_canary
from docchat_ingestion.activities.support import dateadd, literals, remote_context
from docchat_ingestion.boundary import guard
from docchat_ingestion.orchestrators.ingestion_dispatcher import NAME as DISPATCHER
from docchat_ingestion.orchestrators.ingestion_dispatcher import RUN_QUEUED

QUEUE_NAME: Final = "ingestion-requests"
QUEUE_CONNECTION: Final = "DOCCHAT_INGESTION_QUEUE"
"""The app-setting prefix of the trigger's identity-based connection (section 18.1)."""
KEEPALIVE_SCHEDULE: Final = "0 */5 * * * *"
MAINTENANCE_SCHEDULE: Final = "0 40 2 * * *"
QUEUE_SPAN: Final = "docchat.ingestion.queue_message"

WATCHDOG_OUTSTANDING: Final = timedelta(minutes=10)
WATCHDOG_IDLE: Final = timedelta(minutes=75)
"""The longest idle wake (60 minutes) plus 15 minutes."""
TERMINATE_POLL_SECONDS: Final = 2.0
TERMINATE_WAIT_SECONDS: Final = 60.0

HISTORY_FROM: Final = datetime(2020, 1, 1, tzinfo=UTC)
RETENTION_BATCH: Final = 500
CANARY_RETENTION_DAYS: Final = 90

_TERMINAL: Final = frozenset({"Completed", "Failed", "Terminated", "Canceled"})
_STARTED: Final = frozenset({"Running", "Pending"})
_FINISHED_RUNS: Final = ("completed", "completed_with_errors", "failed")

HEARTBEAT_AGE: Final = gauge("docchat.ingestion.dispatcher.heartbeat_age")
ITEMS_QUEUED: Final = gauge("docchat.ingestion.items.queued")
ITEMS_RUNNING: Final = gauge("docchat.ingestion.items.running")
QUEUE_OLDEST_AGE: Final = gauge("docchat.ingestion.queue.oldest_age")

logger: Final = logging.getLogger(LOGGER_NAME)


class OrchestrationStatus(Protocol):
    """What the starters read of an instance's status."""

    @property
    def runtime_status(self) -> Any: ...

    @property
    def custom_status(self) -> Any: ...

    @property
    def last_updated_time(self) -> datetime | None: ...

    def __bool__(self) -> bool: ...


class DurableClient(Protocol):
    """The Durable client binding, seen through the calls the starters make."""

    async def get_status(self, instance_id: str, *options: bool) -> Any: ...

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: Any = None,
    ) -> str: ...

    async def raise_event(
        self, instance_id: str, event_name: str, event_data: Any = None
    ) -> None: ...

    async def terminate(self, instance_id: str, reason: str) -> None: ...


class HistoryPurger(Protocol):
    async def purge_instance_history_by(
        self,
        created_time_from: datetime | None = None,
        created_time_to: datetime | None = None,
        runtime_status: list[Any] | None = None,
    ) -> Any: ...


def status_name(status: Any) -> str | None:
    """The runtime status's name, or `None` when no instance exists."""
    if not status:
        return None
    runtime_status = getattr(status, "runtime_status", None)
    if runtime_status is None:
        return None
    value = getattr(runtime_status, "value", runtime_status)
    return str(value)


# --- ensure_dispatcher -------------------------------------------------------------------------


async def start_dispatcher(client: DurableClient) -> bool:
    """Start generation 0; when the start fails but another starter won the race (the instance
    reads `Running` or `Pending`), that counts as success and `False` is returned."""
    try:
        await client.start_new(DISPATCHER, DISPATCHER_INSTANCE_ID, {"generation": 0})
    except Exception:
        if status_name(await client.get_status(DISPATCHER_INSTANCE_ID)) in _STARTED:
            return False
        raise
    return True


async def ensure_dispatcher(client: DurableClient) -> bool:
    """Start the dispatcher when no instance exists or the last one ended; `True` when this call
    started it. Every other status counts as running, a generation in the middle of its
    continue-as-new reset included, so no second instance starts during a reset."""
    name = status_name(await client.get_status(DISPATCHER_INSTANCE_ID))
    if name is not None and name not in _TERMINAL:
        return False
    return await start_dispatcher(client)


# --- The queue starter -------------------------------------------------------------------------


def _gone(error: Exception) -> bool:
    # The SDK answers a not-found (404) or completed (410) instance with these plain messages; the
    # text is only compared, never recorded.
    text = str(error)
    return text.startswith("No instance with ID") or "is gone: either completed or failed" in text


async def raise_run_queued(client: DurableClient, run_id: str) -> None:
    """The `run_queued` hint; an instance that is gone is ignored, because a starting instance
    sweeps before it waits."""
    try:
        await client.raise_event(DISPATCHER_INSTANCE_ID, RUN_QUEUED, {"runId": run_id})
    except Exception as error:
        if not _gone(error):
            raise


def decode_message(body: bytes) -> IngestionQueueMessage | None:
    """The message, or `None` for one that can never become valid."""
    try:
        return IngestionQueueMessage.validate_wire(body.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError):
        return None


async def start_from_message(
    body: bytes, message_id: str | None, dequeue_count: int | None, client: DurableClient
) -> bool:
    """The starter's body; `True` when it started the dispatcher. A Durable client failure is
    recorded once and raised as `DispatcherUnavailableError` with no cause."""
    async with guard("ingestion_queue_starter") as boundary:
        message = decode_message(body)
        if message is None:
            emit_event(
                "ingestion_message_invalid",
                **({} if message_id is None else {"messageId": message_id}),
            )
            return False
        parent = remote_context(message.traceparent, message.tracestate)
        token = None if parent is None else otel_context.attach(parent)
        try:
            attributes: dict[str, object] = {"docchat.run_id": str(message.run_id)}
            if dequeue_count is not None and dequeue_count >= 1:
                attributes["docchat.ingestion.dequeue_count"] = dequeue_count
            with start_span(QUEUE_SPAN, kind=SpanKind.CONSUMER, **attributes) as span:
                try:
                    started = await ensure_dispatcher(client)
                    set_attributes(span, **{"docchat.ingestion.dispatcher_started": started})
                    await raise_run_queued(client, str(message.run_id))
                except Exception as failure:  # noqa: BLE001 - recorded once, raised first-party
                    raise boundary.escalate(failure) from None
                return started
        finally:
            if token is not None:
                otel_context.detach(token)
    return False


async def ingestion_queue_starter(msg: func.QueueMessage, client: DurableClient) -> None:
    await start_from_message(msg.get_body(), msg.id, msg.dequeue_count, client)


# --- The keep-alive and the watchdog -----------------------------------------------------------


def _custom_status(status: Any) -> Mapping[str, Any] | None:
    custom = getattr(status, "custom_status", None)
    if isinstance(custom, str):
        with contextlib.suppress(ValueError):
            custom = json.loads(custom)
    return custom if isinstance(custom, Mapping) else None


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def record_gauges(
    custom: Mapping[str, Any] | None, heartbeat: datetime | None, now: datetime
) -> None:
    """The four dispatcher gauges, skipping every null value."""
    if heartbeat is not None:
        HEARTBEAT_AGE.set(max(0.0, (now - heartbeat).total_seconds()))
    if custom is None:
        return
    queued = custom.get("queued")
    if isinstance(queued, Mapping):
        for priority in ("interactive", "bulk"):
            value = queued.get(priority)
            if isinstance(value, int):
                ITEMS_QUEUED.set(value, priority=priority)
    running = custom.get("running")
    if isinstance(running, int):
        ITEMS_RUNNING.set(running)
    oldest = custom.get("oldestQueuedAgeSeconds")
    if isinstance(oldest, int | float):
        QUEUE_OLDEST_AGE.set(oldest)


def heartbeat_stale(outstanding: bool, heartbeat: datetime | None, now: datetime) -> bool:
    """The watchdog's rule: older than 10 minutes while work is outstanding, 75 otherwise."""
    if heartbeat is None:
        return False
    limit = WATCHDOG_OUTSTANDING if outstanding else WATCHDOG_IDLE
    return now - heartbeat > limit


async def restart_dispatcher(
    client: DurableClient,
    old_generation: object,
    *,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Terminate, wait at most 60 seconds for `Terminated`, start a new instance and emit the
    restart event; an instance that isn't `Terminated` in time is left to the next tick."""
    await client.terminate(DISPATCHER_INSTANCE_ID, "watchdog")
    give_up_at = clock.monotonic() + TERMINATE_WAIT_SECONDS
    while status_name(await client.get_status(DISPATCHER_INSTANCE_ID)) != "Terminated":
        if clock.monotonic() >= give_up_at:
            return False
        await sleep(TERMINATE_POLL_SECONDS)
    await start_dispatcher(client)
    properties: dict[str, object] = {"reason": "heartbeat_stale"}
    if isinstance(old_generation, int) and old_generation >= 0:
        properties["oldGeneration"] = old_generation
    emit_event("ingestion_dispatcher_restarted", **properties)
    return True


async def keepalive_tick(
    client: DurableClient, *, clock: Clock, sleep: Callable[[float], Awaitable[None]]
) -> bool:
    """One tick: `ensure_dispatcher`, the gauges and the watchdog; `True` when it restarted a
    stale dispatcher."""
    await ensure_dispatcher(client)
    status = await client.get_status(DISPATCHER_INSTANCE_ID, False, False, False)
    now = clock.now()
    custom = _custom_status(status)
    heartbeat = None if custom is None else _instant(custom.get("lastSweepAt"))
    outstanding = True if custom is None else bool(custom.get("outstanding", True))
    if heartbeat is None:
        # A new generation whose first wake hasn't ended, or no sweep yet: counted as outstanding.
        heartbeat = _instant(getattr(status, "last_updated_time", None))
        outstanding = True
    record_gauges(custom, heartbeat, now)
    if status_name(status) != "Running":
        return False
    if custom is not None and custom.get("configValid") is False:
        # Waiting for a valid ops-config area; restarting would change nothing.
        return False
    if not heartbeat_stale(outstanding, heartbeat, now):
        return False
    generation = None if custom is None else custom.get("generation")
    return await restart_dispatcher(client, generation, clock=clock, sleep=sleep)


async def run_keepalive(
    client: DurableClient,
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The keep-alive inside its failure boundary; a failed tick is logged and the next tick,
    5 minutes later, starts afresh."""
    async with guard("ingestion_keepalive"):
        await keepalive_tick(client, clock=SystemClock() if clock is None else clock, sleep=sleep)


async def ingestion_keepalive(timer: func.TimerRequest, client: DurableClient) -> None:
    await run_keepalive(client)


# --- Daily maintenance -------------------------------------------------------------------------


def delete_old_items(connection: Connection, retention_days: int) -> int:
    """One batch of the items of finished runs past retention."""
    runs = ingestion_runs
    doomed = (
        select(ingestion_run_items.c.id)
        .join(runs, runs.c.id == ingestion_run_items.c.run_id)
        .where(
            runs.c.state.in_(literals(_FINISHED_RUNS)),
            runs.c.completed_at < dateadd("day", -retention_days),
        )
        .limit(RETENTION_BATCH)
    )
    return int(
        connection.execute(
            delete(ingestion_run_items).where(ingestion_run_items.c.id.in_(doomed))
        ).rowcount
    )


def delete_old_runs(connection: Connection, retention_days: int) -> int:
    """One batch of finished runs past retention with no item left and no physical index that
    still names them as its build run (error 547 otherwise)."""
    runs = ingestion_runs
    doomed = (
        select(runs.c.id)
        .where(
            runs.c.state.in_(literals(_FINISHED_RUNS)),
            runs.c.completed_at < dateadd("day", -retention_days),
            ~select(ingestion_run_items.c.id)
            .where(ingestion_run_items.c.run_id == runs.c.id)
            .exists(),
            ~select(physical_indexes.c.id)
            .where(physical_indexes.c.build_run_id == runs.c.id)
            .exists(),
        )
        .limit(RETENTION_BATCH)
    )
    return int(connection.execute(delete(runs).where(runs.c.id.in_(doomed))).rowcount)


def expired_open_canaries(connection: Connection) -> list[str]:
    rows = connection.execute(
        select(scan_canaries.c.blob_path).where(
            scan_canaries.c.result.is_(None),
            scan_canaries.c.written_at < dateadd("day", -CANARY_RETENTION_DAYS),
        )
    ).all()
    return [row.blob_path for row in rows]


def delete_old_canaries(connection: Connection) -> int:
    doomed = (
        select(scan_canaries.c.id)
        .where(scan_canaries.c.written_at < dateadd("day", -CANARY_RETENTION_DAYS))
        .limit(RETENTION_BATCH)
    )
    return int(
        connection.execute(delete(scan_canaries).where(scan_canaries.c.id.in_(doomed))).rowcount
    )


async def _repeat(process: IngestionProcess, work: Callable[[Connection], int], name: str) -> None:
    while await process.sql.run(work, name=name, retry="transient"):
        pass


async def purge_durable_history(process: IngestionProcess, client: HistoryPurger) -> None:
    retention = (await process.ops.snapshot()).ingestion.durable_history_retention_days
    await client.purge_instance_history_by(
        created_time_from=HISTORY_FROM,
        created_time_to=process.clock.now() - timedelta(days=retention),
        runtime_status=[
            OrchestrationRuntimeStatus.Completed,
            OrchestrationRuntimeStatus.Failed,
            OrchestrationRuntimeStatus.Terminated,
        ],
    )


async def purge_runs(process: IngestionProcess) -> None:
    retention = (await process.ops.snapshot()).ingestion.run_retention_days
    await _repeat(process, lambda c: delete_old_items(c, retention), "maintenance_run_items")
    await _repeat(process, lambda c: delete_old_runs(c, retention), "maintenance_runs")


async def write_daily_canary(process: IngestionProcess) -> None:
    if process.scanning_enabled:
        await write_canary(process, statement="maintenance_canary_write")


async def retain_canaries(process: IngestionProcess) -> None:
    """Delete the blobs of canaries still open after 90 days, then the rows; a failed blob
    delete ends the step before any row is deleted, so the next day retries with every path."""
    paths = await process.sql.run(
        expired_open_canaries, name="maintenance_open_canaries", retry="transient"
    )
    for path in paths:
        await delete_absent_ok(process.documents, path)
    await _repeat(process, delete_old_canaries, "maintenance_canaries")


async def _step(name: str, work: Callable[[], Awaitable[None]]) -> None:
    try:
        await work()
    except Exception as failure:  # noqa: BLE001 - recorded once as the step's warning
        log_event(
            logger,
            "maintenance_failed",
            logging.WARNING,
            **{"docchat.ingestion.step": name, "error.type": qualified_type_name(type(failure))},
        )


async def maintenance(process: IngestionProcess, client: HistoryPurger) -> None:
    """The four steps, each on its own; a failed step is retried the next day."""
    await _step("durable_history", lambda: purge_durable_history(process, client))
    await _step("runs", lambda: purge_runs(process))
    await _step("scanner_canary", lambda: write_daily_canary(process))
    await _step("canary_retention", lambda: retain_canaries(process))


async def run_maintenance(client: HistoryPurger, provider: ProcessProvider = runtime) -> None:
    async with guard("ingestion_maintenance"):
        await maintenance(await provider(), client)


async def ingestion_maintenance(timer: func.TimerRequest, client: HistoryPurger) -> None:
    await run_maintenance(client)


bp: Final = df.Blueprint()
"""The three triggers; `docchat_ingestion.function_app` registers this blueprint."""

# Applied as calls, so the module's names stay the plain functions the tests call.
bp.queue_trigger(arg_name="msg", queue_name=QUEUE_NAME, connection=QUEUE_CONNECTION)(
    bp.durable_client_input(client_name="client")(ingestion_queue_starter)
)
bp.timer_trigger(schedule=KEEPALIVE_SCHEDULE, arg_name="timer", run_on_startup=False)(
    bp.durable_client_input(client_name="client")(ingestion_keepalive)
)
bp.timer_trigger(schedule=MAINTENANCE_SCHEDULE, arg_name="timer", run_on_startup=False)(
    bp.durable_client_input(client_name="client")(ingestion_maintenance)
)
