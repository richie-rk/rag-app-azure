"""The data-access layer against the SQL Server container (`01-repository.md` section 6.2 and
acceptance criterion 16; ADR 0026; T-522), its start-up warm-up included (section 6.2 item 11),
and the SQL layer telemetry test (T-490, `85-observability.md` section 3; `01-repository.md`
section 6.2 item 9)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, Integer, cast, literal, select, update

from docchat_core.sql.engine import SERVICES, SqlEngine, SqlError
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import ops_config
from docchat_testing.builders import canary
from docchat_testing.telemetry_capture import TelemetryCapture

type SqlEngineFactory = Callable[..., SqlEngine]

SESSIONS_OF = (
    "SELECT COUNT(*) FROM sys.dm_exec_sessions "
    "WHERE program_name = 'docchat-api' AND database_id = DB_ID()"
)


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 30, tzinfo=UTC)
        self._monotonic = 1_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


async def test_t522_every_session_names_its_service(make_sql_engine: SqlEngineFactory) -> None:
    for service in SERVICES:
        sql = make_sql_engine(service)
        name = await sql.run(
            lambda connection: connection.exec_driver_sql("SELECT APP_NAME()").scalar_one(),
            name="app_name",
            retry="transient",
        )
        assert name == f"docchat-{service}"


async def test_t522_two_deadlocked_transactions_both_complete(
    make_sql_engine: SqlEngineFactory,
) -> None:
    sql = make_sql_engine("api")
    keys = ("quota.tokens_per_day", "quota.tokens_per_month")
    barrier = threading.Barrier(2, timeout=10)
    attempts = {"first": 0, "second": 0}

    def crossed(label: str, order: tuple[str, str]) -> Any:
        def work(connection: Connection) -> None:
            attempts[label] += 1
            for position, key in enumerate(order):
                connection.execute(
                    update(ops_config)
                    .where(ops_config.c.config_key == key)
                    .values(value_json=ops_config.c.value_json)
                )
                if position == 0 and attempts[label] == 1:
                    # Both hold their first row before either asks for the other's.
                    barrier.wait()

        return work

    await asyncio.gather(
        sql.run(crossed("first", keys), name="crossed_first", retry="transient"),
        sql.run(crossed("second", keys[::-1]), name="crossed_second", retry="transient"),
    )
    assert sorted(attempts.values()) == [1, 2]


async def test_t522_idle_pool_is_released_and_reconnects(
    make_sql_engine: SqlEngineFactory, admin: Engine
) -> None:
    clock = FakeClock()
    sql = make_sql_engine("api", clock=clock)

    def sessions() -> int:
        # A login and a query of the test's own engine: off the loop (84-testing.md 2.8).
        with admin.connect() as connection:
            return int(connection.exec_driver_sql(SESSIONS_OF).scalar_one())

    await sql.run(lambda connection: 1, name="touch", retry="transient")
    assert await asyncio.to_thread(sessions) >= 1
    clock.advance(10 * 60)
    assert await sql.release_idle()
    assert await asyncio.to_thread(sessions) == 0
    assert await sql.run(lambda connection: 2, name="touch", retry="transient") == 2


async def test_t522_warm_up_opens_every_pooled_connection(
    make_sql_engine: SqlEngineFactory, admin: Engine
) -> None:
    sql = make_sql_engine("api")

    def sessions() -> int:
        # A login and a query of the test's own engine: off the loop (84-testing.md 2.8).
        with admin.connect() as connection:
            return int(connection.exec_driver_sql(SESSIONS_OF).scalar_one())

    assert await sql.warm() == SqlSettings().sql_threads
    assert await asyncio.to_thread(sessions) == SqlSettings().sql_threads
    assert sql.checked_out() == 0
    # A unit of work takes one of the open connections: no session is added.
    assert await sql.run(lambda connection: 1, name="touch", retry="transient") == 1
    assert await asyncio.to_thread(sessions) == SqlSettings().sql_threads


async def test_t522_concurrent_reads_never_block_the_loop(
    make_sql_engine: SqlEngineFactory,
) -> None:
    sql = make_sql_engine("api")
    # Warmed as each process's start-up warms its engine (01-repository.md section 6.2 item 11).
    assert await sql.warm() == SqlSettings().sql_threads
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    watcher = asyncio.create_task(ticker())
    statement = select(ops_config.c.value_json).where(
        ops_config.c.config_key == "api.write_requests_per_minute"
    )
    rows = await asyncio.gather(*(sql.fetch_one(statement, name="read_one") for _ in range(200)))
    stop.set()
    await watcher
    assert all(row is not None for row in rows)
    assert max(gaps) < 0.1 + 0.01


async def test_t522_cancelled_caller_still_commits(
    make_sql_engine: SqlEngineFactory, admin: Engine
) -> None:
    sql = make_sql_engine("api")
    key = "feedback.comments_enabled"
    reason = "integration test: cancelled caller"

    def write(connection: Connection) -> None:
        connection.execute(
            update(ops_config).where(ops_config.c.config_key == key).values(change_reason=reason)
        )
        connection.exec_driver_sql("WAITFOR DELAY '00:00:01'")

    task = asyncio.create_task(sql.run(write, name="slow_write", retry="none"))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(1.5)

    def stored_then_cleared() -> Any:
        # A login and two statements of the test's own engine: off the loop (84-testing.md 2.8).
        with admin.connect() as connection:
            stored = connection.execute(
                select(ops_config.c.change_reason).where(ops_config.c.config_key == key)
            ).scalar_one()
            connection.execute(
                update(ops_config).where(ops_config.c.config_key == key).values(change_reason=None)
            )
            connection.commit()
        return stored

    assert await asyncio.to_thread(stored_then_cleared) == reason


# T-490

STATEMENT_WORDS = ("SELECT ", "FROM app.", "app.ops_config", "CAST(", "config_key")
"""Pieces of the two statements' text; record names such as `ops_config_unreachable` aren't."""


async def test_t490_sql_telemetry_holds_no_statement_text_and_no_parameter_value(
    make_sql_engine: SqlEngineFactory, telemetry_capture: TelemetryCapture
) -> None:
    sql = make_sql_engine("api")
    value = canary("question")
    read = select(ops_config.c.value_json).where(ops_config.c.config_key == value)
    units = {"docchat.sql.t490_read", "docchat.sql.t490_convert"}

    assert await sql.fetch_one(read, name="t490_read") is None
    # SQL Server quotes the value it can't convert in its message (error 245), so the failure
    # carries the canary into the layer, which must keep it out of every record.
    with pytest.raises(SqlError):
        await sql.run(
            lambda connection: connection.execute(select(cast(literal(value), Integer))).scalar(),
            name="t490_convert",
            retry="none",
        )

    spans = [span for span in telemetry_capture.spans() if span.name in units]
    assert {span.name for span in spans} == units

    def exported_texts() -> list[str]:
        return [
            text
            for item in telemetry_capture.items()
            if not item.label.startswith("span ")
            or any(f"'{unit}'" in item.label for unit in units)
            for _, text in item.strings
        ]

    # Reading the whole session's telemetry is blocking work: off the loop (84-testing.md 2.8).
    texts = await asyncio.to_thread(exported_texts)
    assert [text for text in texts if value in text or "cnry-" in text] == []
    assert [text for text in texts if any(word in text for word in STATEMENT_WORDS)] == []
