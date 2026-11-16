"""Unit tests of the data-access layer (`01-repository.md` section 6.2; ADR 0026; T-521).

pyodbc's `connect` is replaced at the connection boundary by SQLite connections, and SQLite's
dialect stands in for SQL Server's, so units of work run real statements in the SQL threads;
driver errors are raised as pyodbc formats them.
"""

from __future__ import annotations

import asyncio
import sqlite3
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# The tests raise driver errors exactly as the connection boundary raises them.
import pyodbc  # type: ignore[import-not-found,unused-ignore]  # noqa: TID251
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from pydantic import SecretStr
from sqlalchemy import Connection, column, select, table
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import NullPool

from docchat_core.settings import LocalSettings, SettingsValidationError
from docchat_core.sql import engine as engine_module
from docchat_core.sql.engine import (
    REQUEST_PATH_REGISTRY,
    DriverError,
    SqlDeadlock,
    SqlEngine,
    SqlError,
    SqlTimeout,
    SqlUnavailable,
    SqlUniqueViolation,
    access_token_attribute,
    azure_connection_string,
    connection_string,
    create_sync_engine,
    describe_driver_error,
    emulator_connection_string,
    request_path,
    retry_delay,
    sql_error,
)
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.telemetry import SqlInstruments

LOCAL_PASSWORD = "Aa1!aaaaaaaaaaaaaaaaaaaa"


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        self._monotonic = 1_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


@dataclass
class FakeToken:
    token: str
    expires_on: int


@dataclass
class FakeCredential:
    clock: FakeClock
    lifetimes: list[int] = field(default_factory=lambda: [3600])
    calls: int = 0
    fail: bool = False

    async def get_token(self, *scopes: str) -> FakeToken:
        assert scopes == ("https://database.windows.net/.default",)
        self.calls += 1
        # Yield, so concurrent callers really overlap with the one fetch.
        await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("the identity endpoint said no")
        lifetime = self.lifetimes[min(self.calls, len(self.lifetimes)) - 1]
        expires = int(self.clock.now().timestamp()) + lifetime
        return FakeToken(token=f"token-{self.calls}", expires_on=expires)


class _DriverConnection:
    """A SQLite connection that also takes pyodbc's writable `timeout`."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.timeout: int | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dataclass
class ConnectCall:
    odbc: str
    autocommit: bool
    timeout: int
    attrs_before: dict[int, bytes] | None


@dataclass
class FakeDriver:
    path: Path
    calls: list[ConnectCall] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)
    connections: list[_DriverConnection] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    hang: threading.Event | None = None
    """When set, the next connect waits for it, as a login to an unresponsive server does."""

    def connect(
        self,
        odbc: str,
        *,
        autocommit: bool,
        timeout: int,
        attrs_before: dict[int, bytes] | None,
    ) -> _DriverConnection:
        self.calls.append(ConnectCall(odbc, autocommit, timeout, attrs_before))
        self.threads.append(threading.current_thread().name)
        hang, self.hang = self.hang, None
        if hang is not None:
            hang.wait(10)
        if self.failures:
            raise self.failures.pop(0)
        connection = _DriverConnection(sqlite3.connect(self.path, check_same_thread=False))
        self.connections.append(connection)
        return connection


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDriver:
    fake = FakeDriver(path=tmp_path / "sql.db")
    monkeypatch.setattr(pyodbc, "connect", fake.connect)
    # The SQLite dialect stands in for SQL Server's, so units of work run real statements.
    monkeypatch.setattr(engine_module, "_ENGINE_URL", "sqlite://")
    return fake


@dataclass
class Telemetry:
    tracer: Tracer
    spans: InMemorySpanExporter
    instruments: SqlInstruments
    reader: InMemoryMetricReader

    def finished_spans(self) -> tuple[ReadableSpan, ...]:
        return self.spans.get_finished_spans()

    def points(self, name: str) -> list[tuple[dict[str, Any], float]]:
        data = self.reader.get_metrics_data()
        found: list[tuple[dict[str, Any], float]] = []
        if data is None:
            return found
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != name:
                        continue
                    for point in metric.data.data_points:
                        # A histogram point carries a count, the others a value.
                        value: Any = getattr(point, "value", None)
                        if value is None:
                            value = getattr(point, "count", 0)
                        found.append((dict(point.attributes or {}), float(value)))
        return found


@pytest.fixture
def telemetry() -> Telemetry:
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    return Telemetry(
        tracer=tracer_provider.get_tracer("test"),
        spans=exporter,
        instruments=SqlInstruments(meter_provider.get_meter("test")),
        reader=reader,
    )


SERVER = "sql-docchat-dev-abc123.database.windows.net"


def driver_error(number: int, sqlstate: str = "42000", text: str = "Something failed.") -> Any:
    return pyodbc.Error(
        sqlstate,
        f"[{sqlstate}] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]{text} "
        f"({number}) (SQLExecDirectW)",
    )


DEADLOCK = (1205, "40001", "Transaction was deadlocked on lock resources.")
RESUMING = (40613, "08S01", "Database 'docchat' on server is not currently available.")
UNIQUE = (
    2627,
    "23000",
    (
        "Violation of UNIQUE KEY constraint 'UQ_projects_name'. Cannot insert duplicate key in "
        "object 'app.projects'. The duplicate key value is (Secret Project)."
    ),
)


def local_settings() -> LocalSettings:
    return LocalSettings(local_emulators=True, local_sql_sa_password=SecretStr(LOCAL_PASSWORD))


def make_engine(
    clock: FakeClock,
    telemetry: Telemetry,
    *,
    credential: FakeCredential | None = None,
    threads: int = 2,
    idle_minutes: int = 10,
    connect_timeout: float | None = None,
) -> SqlEngine:
    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    if credential is None:
        sql = SqlSettings(sql_threads=threads, sql_idle_release_minutes=idle_minutes)
        local = local_settings()
    else:
        sql = SqlSettings(sql_server=SERVER, sql_threads=threads)
        local = LocalSettings(local_emulators=False)
    if connect_timeout is not None:
        # Below the setting's 5-second minimum, so a test of the warm-up's bound stays short.
        sql = sql.model_copy(update={"sql_connect_timeout_seconds": connect_timeout})
    return SqlEngine(
        "api",
        sql=sql,
        local=local,
        credential=credential,
        clock=clock,
        tracer=telemetry.tracer,
        instruments=telemetry.instruments,
        sleep=sleep,
        jitter=lambda: 0.5,
    )


def failing_then(results: list[Exception | int]) -> Callable[[Connection], int]:
    calls = iter(results)

    def work(connection: Connection) -> int:
        del connection
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return work


# --- Connection strings and the token attribute ----------------------------------------------


def test_azure_connection_string_is_exact() -> None:
    assert azure_connection_string("api", server=SERVER, database="docchat") == (
        "Driver={ODBC Driver 18 for SQL Server};Server=tcp:" + SERVER + ",1433;"
        "Database=docchat;Encrypt=strict;ConnectRetryCount=0;APP=docchat-api"
    )


@pytest.mark.parametrize("service", ["api", "ingestion", "migrations", "grants", "ops"])
def test_every_service_names_itself(service: Any) -> None:
    assert connection_string(
        service, SqlSettings(sql_server=SERVER), LocalSettings(local_emulators=False)
    ).endswith(f";APP=docchat-{service}")


def test_unknown_service_is_refused() -> None:
    with pytest.raises(ValueError, match="service"):
        azure_connection_string("web", server=SERVER, database="docchat")  # type: ignore[arg-type]


def test_azure_form_refuses_another_host() -> None:
    with pytest.raises(ValueError, match="server"):
        azure_connection_string("api", server="evil.example.com", database="docchat")


def test_emulator_form_uses_sql_authentication() -> None:
    text = connection_string("migrations", SqlSettings(), local_settings())
    assert text == (
        "Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1,14330;Database=docchat;"
        f"UID=sa;PWD={LOCAL_PASSWORD};Encrypt=yes;TrustServerCertificate=yes;"
        "ConnectRetryCount=0;APP=docchat-migrations"
    )


def test_emulator_password_with_delimiters_is_braced() -> None:
    text = emulator_connection_string("api", password="a;b}c", database="docchat_test_0a1b2c3d")
    assert "PWD={a;b}}c};" in text
    assert "Database=docchat_test_0a1b2c3d;" in text


def test_emulator_form_refuses_odd_names() -> None:
    with pytest.raises(ValueError, match="database"):
        emulator_connection_string("api", password="x", database="docchat;DROP")


def test_server_required_outside_emulator_mode() -> None:
    with pytest.raises(SettingsValidationError) as raised:
        connection_string("api", SqlSettings(), LocalSettings(local_emulators=False))
    assert raised.value.settings == ("DOCCHAT_SQL_SERVER",)


def test_connection_string_reads_the_environment(settings_env: pytest.MonkeyPatch) -> None:
    settings_env.setenv("DOCCHAT_SQL_SERVER", SERVER)
    assert connection_string("ops").endswith("APP=docchat-ops")


def test_t521_access_token_attribute_structure() -> None:
    token = "eyJ0eXAi.payload.sig"
    encoded = token.encode("utf-16-le")
    attribute = access_token_attribute(token)
    assert attribute[:4] == struct.pack("<I", len(encoded))
    assert attribute[4:] == encoded
    assert len(attribute) == 4 + 2 * len(token)
    assert access_token_attribute("ab") == b"\x04\x00\x00\x00a\x00b\x00"


def test_create_sync_engine_needs_a_token_in_azure() -> None:
    with pytest.raises(ValueError, match="token"):
        create_sync_engine(
            "grants",
            access_token=None,
            sql=SqlSettings(sql_server=SERVER),
            local=LocalSettings(local_emulators=False),
        )


def test_create_sync_engine_passes_the_token(driver: FakeDriver) -> None:
    sync = create_sync_engine(
        "grants",
        access_token="tok",
        sql=SqlSettings(sql_server=SERVER),
        local=LocalSettings(local_emulators=False),
    )
    assert isinstance(sync.pool, NullPool)
    with sync.connect() as connection:
        assert connection.execute(select(1)).scalar_one() == 1
    call = driver.calls[0]
    assert call.odbc.endswith("APP=docchat-grants")
    assert call.attrs_before == {1256: access_token_attribute("tok")}
    assert call.timeout == 30
    assert call.autocommit is False
    assert driver.connections[0].timeout == 30
    sync.dispose()


def test_create_sync_engine_local_has_no_token(driver: FakeDriver) -> None:
    sync = create_sync_engine(
        "migrations", access_token="ignored", sql=SqlSettings(), local=local_settings()
    )
    with sync.connect():
        pass
    assert driver.calls[0].attrs_before is None
    sync.dispose()


# --- Error parsing -----------------------------------------------------------------------


def test_error_number_single_record() -> None:
    error = describe_driver_error(driver_error(*DEADLOCK[:2]))
    assert error == DriverError(sqlstate="40001", number=1205)
    assert error.transient


def test_error_number_two_record_format() -> None:
    error = pyodbc.Error(
        "23000",
        "[23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Violation of PRIMARY KEY "
        "constraint 'PK_users'. Cannot insert duplicate key in object 'app.users'. The duplicate "
        "key value is (x). (2627) (SQLExecDirectW); [01000] [Microsoft][ODBC Driver 18 for SQL "
        "Server][SQL Server]The statement has been terminated. (3621)",
    )
    described = describe_driver_error(error)
    assert described.number == 2627
    assert described.constraint == "PK_users"


def test_error_number_ignores_a_quoted_key_value() -> None:
    error = driver_error(
        2601,
        "23000",
        "Cannot insert duplicate key row in object 'app.documents' with unique index "
        "'UQ_documents_index_name'. The duplicate key value is (x (1205) (SQLExecDirectW) "
        "constraint 'PK_fake').",
    )
    described = describe_driver_error(error)
    assert described.number == 2601
    assert described.constraint == "UQ_documents_index_name"


def test_error_without_number_is_zero() -> None:
    assert describe_driver_error(pyodbc.Error("HYT00", "[HYT00] Query timeout expired")) == (
        DriverError(sqlstate="HYT00", number=0)
    )
    assert describe_driver_error(pyodbc.Error()).number == 0


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (DriverError("23000", 2627, "UQ_x"), SqlUniqueViolation),
        (DriverError("23000", 2601, "UX_x"), SqlUniqueViolation),
        (DriverError("40001", 1205), SqlDeadlock),
        (DriverError("HYT00", 0), SqlTimeout),
        (DriverError("08S01", 10054), SqlUnavailable),
        (DriverError("23000", 547), SqlError),
    ],
)
def test_sql_error_by_number(error: DriverError, kind: type[SqlError]) -> None:
    raised = sql_error(error)
    assert type(raised) is kind
    assert raised.number == error.number


def test_transient_numbers_and_sqlstate() -> None:
    for number in (1205, 40197, 40501, 40613, 49918, 49919, 49920, 4221, 10928, 10929):
        assert DriverError("", number).transient
    for number in (10053, 10054, 10060, 233, 64):
        assert DriverError("", number).transient
    assert DriverError("08S01", 0).transient
    assert not DriverError("23000", 2627).transient


def test_retry_delays_double_to_the_cap() -> None:
    error = DriverError("40001", 1205)
    delays = [retry_delay(attempt, error, 0.0, 1.0) for attempt in range(1, 7)]
    assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, None]
    assert retry_delay(5, error, 0.0, 1.2) == pytest.approx(4.8)


def test_retry_delay_of_a_resuming_database() -> None:
    error = DriverError("08S01", 40613)
    assert retry_delay(7, error, 0.0, 1.0) == 10.0
    assert retry_delay(12, error, 0.0, 1.0) is None
    assert retry_delay(3, error, 89.5, 1.0) is None


# --- Units of work -------------------------------------------------------------------------


async def test_t521_deadlock_reruns_the_unit(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    calls: list[int] = []

    def work(connection: Connection) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise driver_error(*DEADLOCK[:2], text=DEADLOCK[2])
        return "done"

    assert await sql.run(work, name="ensure_user", retry="transient") == "done"
    assert len(calls) == 2
    assert telemetry.points("docchat.sql.retries") == [({"error_number": 1205}, 1.0)]
    (span,) = telemetry.finished_spans()
    assert span.name == "docchat.sql.ensure_user"
    assert dict(span.attributes or {}) == {
        "db.system.name": "microsoft.sql_server",
        "docchat.sql.attempts": 2,
        "docchat.sql.error_number": 1205,
        "docchat.outcome": "ok",
    }
    await sql.dispose()


async def test_t521_resuming_database_budget(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    calls: list[int] = []

    def work(connection: Connection) -> None:
        calls.append(1)
        raise driver_error(*RESUMING[:2], text=RESUMING[2])

    started = fake_clock.monotonic()
    with pytest.raises(SqlUnavailable) as raised:
        await sql.run(work, name="read_config", retry="transient")
    assert raised.value.reason == "exhausted"
    assert raised.value.number == 40613
    assert len(calls) <= 12
    assert fake_clock.monotonic() - started <= 90
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 40613, "reason": "exhausted"}, 1.0)
    ]
    await sql.dispose()


async def test_other_transient_errors_stop_after_six_attempts(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    work = failing_then([driver_error(40501, "42000")] * 6)
    with pytest.raises(SqlUnavailable) as raised:
        await sql.run(work, name="read", retry="transient")
    assert raised.value.reason == "exhausted"
    (span,) = telemetry.finished_spans()
    assert span.attributes is not None
    assert span.attributes["docchat.sql.attempts"] == 6
    assert span.attributes["docchat.outcome"] == "error"
    await sql.dispose()


async def test_t521_retry_none_never_retries(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    calls: list[int] = []

    def work(connection: Connection) -> None:
        calls.append(1)
        raise driver_error(*DEADLOCK[:2])

    with pytest.raises(SqlDeadlock):
        await sql.run(work, name="write", retry="none")
    assert calls == [1]
    assert telemetry.points("docchat.sql.retries") == []
    await sql.dispose()


async def test_t521_unique_violation_names_the_constraint_and_no_text(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    work = failing_then([driver_error(*UNIQUE[:2], text=UNIQUE[2])])
    with pytest.raises(SqlUniqueViolation) as raised:
        await sql.run(work, name="create_project", retry="none")
    error = raised.value
    assert error.constraint == "UQ_projects_name"
    assert error.number == 2627
    assert "Secret Project" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    await sql.dispose()


async def test_timeout_maps_to_sql_timeout(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    work = failing_then([pyodbc.Error("HYT00", "[HYT00] Query timeout expired (0) (SQLExecute)")])
    with pytest.raises(SqlTimeout):
        await sql.run(work, name="slow", retry="transient")
    await sql.dispose()


async def test_other_errors_pass_through(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    work = failing_then([KeyError("bug")])
    with pytest.raises(KeyError):
        await sql.run(work, name="buggy", retry="transient")
    (span,) = telemetry.finished_spans()
    assert span.attributes is not None
    assert span.attributes["docchat.outcome"] == "error"
    await sql.dispose()


async def test_connect_failure_is_unavailable(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry)
    driver.failures.append(driver_error(18456, "28000", "Login failed for user."))
    with pytest.raises(SqlUnavailable) as raised:
        await sql.run(lambda connection: 1, name="read", retry="transient")
    assert raised.value.reason == "connection"
    assert raised.value.number == 18456
    await sql.dispose()


async def test_transient_connect_failure_is_retried(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry)
    driver.failures.append(driver_error(*RESUMING[:2]))
    assert await sql.run(lambda connection: 7, name="read", retry="transient") == 7
    assert len(driver.calls) == 2
    await sql.dispose()


async def test_pool_timeout_is_unavailable(
    driver: FakeDriver,
    fake_clock: FakeClock,
    telemetry: Telemetry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)

    def no_connection() -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    monkeypatch.setattr(sql._engine, "connect", no_connection)
    with pytest.raises(SqlUnavailable) as raised:
        await sql.run(lambda connection: 1, name="read", retry="transient")
    assert raised.value.reason == "pool_timeout"
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 0, "reason": "pool_timeout"}, 1.0)
    ]
    await sql.dispose()


async def test_work_runs_in_a_sql_thread_inside_a_transaction(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)

    def work(connection: Connection) -> str:
        assert connection.in_transaction()
        return threading.current_thread().name

    name = await sql.run(work, name="where", retry="transient")
    assert name.startswith("docchat-sql")
    assert telemetry.points("docchat.sql.pool.wait")
    assert telemetry.points("docchat.sql.duration")[0][0] == {"operation": "where", "outcome": "ok"}
    await sql.dispose()


async def test_fetch_and_execute(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    await sql.run(
        lambda connection: connection.exec_driver_sql("CREATE TABLE t (v INTEGER)"),
        name="setup",
        retry="none",
    )
    numbers = table("t", column("v"))
    assert await sql.execute(numbers.insert().values(v=1), name="insert_one", retry="none") == 1
    await sql.execute(numbers.insert(), [{"v": 2}], name="insert_two", retry="none")  # type: ignore[arg-type]
    rows = await sql.fetch_all(select(numbers.c.v).order_by(numbers.c.v), name="read_all")
    assert [row.v for row in rows] == [1, 2]
    one = await sql.fetch_one(
        select(numbers.c.v).where(numbers.c.v == 2), name="read_one", retry="none"
    )
    assert one is not None
    assert one.v == 2
    assert await sql.fetch_one(select(numbers.c.v).where(numbers.c.v == 9), name="none") is None
    await sql.dispose()


async def test_names_are_snake_case(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    with pytest.raises(ValueError, match="snake case"):
        await sql.run(lambda connection: 1, name="Bad Name", retry="none")
    await sql.dispose()


async def test_cancelled_caller_still_commits(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    await sql.run(
        lambda connection: connection.exec_driver_sql("CREATE TABLE c (v INTEGER)"),
        name="setup",
        retry="none",
    )
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def work(connection: Connection) -> None:
        connection.exec_driver_sql("INSERT INTO c (v) VALUES (1)")
        loop.call_soon_threadsafe(started.set)
        release.wait(5)

    task = asyncio.create_task(sql.run(work, name="slow_write", retry="none"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    count = 0
    for _ in range(100):
        count = await sql.run(
            lambda connection: connection.exec_driver_sql("SELECT COUNT(*) FROM c").scalar_one(),
            name="count",
            retry="none",
        )
        if count:
            break
        await asyncio.sleep(0.01)
    assert count == 1
    await sql.dispose()


async def test_no_more_units_than_sql_threads_are_handed_to_the_thread_pool(
    driver: FakeDriver,
    fake_clock: FakeClock,
    telemetry: Telemetry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry, threads=2)
    handed = threading.Semaphore(0)
    lock = threading.Lock()
    in_pool: list[int] = [0]
    most: list[int] = [0]
    submit = sql._executor.submit

    def counted(function: Callable[..., Any], *args: Any) -> Any:
        with lock:
            in_pool[0] += 1
            most[0] = max(most[0], in_pool[0])

        def finished(_: object) -> None:
            with lock:
                in_pool[0] -= 1

        future = submit(function, *args)
        future.add_done_callback(finished)
        return future

    monkeypatch.setattr(sql._executor, "submit", counted)

    def work(connection: Connection) -> int:
        handed.acquire(timeout=5)
        return 1

    units = [asyncio.create_task(sql.run(work, name="read", retry="transient")) for _ in range(10)]
    for _ in range(10):
        await asyncio.sleep(0.01)
        handed.release()
    assert await asyncio.gather(*units) == [1] * 10
    # The other eight waited on the loop, not in the thread pool's queue.
    assert most[0] == 2
    await sql.dispose()


# --- Warm-up -------------------------------------------------------------------------------


async def test_warm_up_opens_every_connection_each_in_its_own_sql_thread(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry, threads=3)

    assert await sql.warm() == 3

    assert len(driver.calls) == 3
    assert len(set(driver.threads)) == 3
    assert all(name.startswith("docchat-sql") for name in driver.threads)
    assert sql.checked_out() == 0
    (span,) = telemetry.finished_spans()
    assert span.name == "docchat.sql.warm_pool"
    assert dict(span.attributes or {}) == {
        "db.system.name": "microsoft.sql_server",
        "docchat.sql.attempts": 1,
        "docchat.sql.error_number": 0,
        "docchat.outcome": "ok",
    }
    assert telemetry.points("docchat.sql.duration")[0][0] == {
        "operation": "warm_pool",
        "outcome": "ok",
    }
    assert telemetry.points("docchat.sql.pool.wait") == [({"service": "api"}, 3.0)]
    assert telemetry.points("docchat.sql.failures") == []

    # Three units at once find three open connections: none is opened at first use.
    together = threading.Barrier(3, timeout=5)

    def overlapping(connection: Connection) -> int:
        together.wait()
        return 1

    results = await asyncio.gather(
        *(sql.run(overlapping, name="read", retry="transient") for _ in range(3))
    )
    assert results == [1, 1, 1]
    assert len(driver.calls) == 3
    await sql.dispose()


async def test_warm_up_takes_the_token_on_the_loop_once(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    credential = FakeCredential(clock=fake_clock)
    sql = make_engine(fake_clock, telemetry, credential=credential, threads=4)

    assert await sql.warm() == 4

    assert credential.calls == 1
    assert [call.attrs_before for call in driver.calls] == [
        {1256: access_token_attribute("token-1")}
    ] * 4
    await sql.dispose()


async def test_a_failed_checkout_ends_the_warm_up_without_raising(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry, threads=2)
    driver.failures.append(driver_error(18456, "28000", "Login failed for user."))
    loop = asyncio.get_running_loop()
    started = loop.time()

    # The other thread stops waiting at once instead of at the bound.
    assert await sql.warm() == 1

    assert loop.time() - started < 5
    assert sql.checked_out() == 0
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 18456, "reason": "not_retryable"}, 1.0)
    ]
    (span,) = telemetry.finished_spans()
    assert span.attributes is not None
    assert span.attributes["docchat.outcome"] == "error"
    assert span.attributes["docchat.sql.error_number"] == 18456
    assert not span.status.is_ok
    # The connection that didn't open opens at first use.
    assert await sql.run(lambda connection: 7, name="read", retry="transient") == 7
    await sql.dispose()


async def test_a_token_failure_ends_the_warm_up_without_raising(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    credential = FakeCredential(clock=fake_clock, fail=True)
    sql = make_engine(fake_clock, telemetry, credential=credential)

    assert await sql.warm() == 0

    assert driver.calls == []
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 0, "reason": "token"}, 1.0)
    ]
    await sql.dispose()


async def test_the_warm_up_waits_at_most_the_connect_timeout(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry, threads=2, connect_timeout=0.2)
    hang = threading.Event()
    driver.hang = hang
    loop = asyncio.get_running_loop()
    started = loop.time()

    assert await sql.warm() == 1

    assert loop.time() - started < 2
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 0, "reason": "not_retryable"}, 1.0)
    ]
    # The thread that opened stopped waiting and checked its connection in.
    for _ in range(100):
        if sql.checked_out() == 0:
            break
        await asyncio.sleep(0.01)
    assert sql.checked_out() == 0
    # The login that outlived the bound finishes in its thread; its connection joins the pool.
    hang.set()
    for _ in range(100):
        if len(driver.connections) == 2 and sql.checked_out() == 0:
            break
        await asyncio.sleep(0.01)
    assert len(driver.connections) == 2
    await sql.dispose()


async def test_a_cancelled_warm_up_checks_its_connections_in(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry, threads=2)
    hang = threading.Event()
    driver.hang = hang
    task = asyncio.create_task(sql.warm())
    for _ in range(100):
        if sql.checked_out() == 1:
            break
        await asyncio.sleep(0.01)
    assert sql.checked_out() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(100):
        if sql.checked_out() == 0:
            break
        await asyncio.sleep(0.01)
    assert sql.checked_out() == 0
    assert telemetry.points("docchat.sql.failures") == []
    # The login that outlived the warm-up finishes in its thread; its connection joins the pool,
    # which the disposal then closes.
    hang.set()
    for _ in range(100):
        if len(driver.connections) == 2 and sql.checked_out() == 0:
            break
        await asyncio.sleep(0.01)
    assert len(driver.connections) == 2
    assert sql.checked_out() == 0
    await sql.dispose()


# --- Tokens --------------------------------------------------------------------------------


async def test_t521_one_token_request_for_fifty_units(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    credential = FakeCredential(clock=fake_clock)
    sql = make_engine(fake_clock, telemetry, credential=credential, threads=8)
    results = await asyncio.gather(
        *(sql.run(lambda connection: 1, name="read", retry="transient") for _ in range(50))
    )
    assert results == [1] * 50
    assert credential.calls == 1
    assert {call.attrs_before[1256] for call in driver.calls if call.attrs_before} == {
        access_token_attribute("token-1")
    }
    await sql.dispose()


async def test_token_close_to_expiry_is_refreshed(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    credential = FakeCredential(clock=fake_clock, lifetimes=[200, 3600])
    sql = make_engine(fake_clock, telemetry, credential=credential)
    assert await sql.ensure_token() == "token-1"
    assert await sql.ensure_token() == "token-2"
    assert await sql.ensure_token() == "token-2"
    fake_clock.advance(3600 - 301)
    assert await sql.ensure_token() == "token-2"
    fake_clock.advance(2)
    assert await sql.ensure_token() == "token-3"
    assert credential.calls == 3
    await sql.dispose()


async def test_token_failure_is_unavailable(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    credential = FakeCredential(clock=fake_clock, fail=True)
    sql = make_engine(fake_clock, telemetry, credential=credential)
    with pytest.raises(SqlUnavailable) as raised:
        await sql.run(lambda connection: 1, name="read", retry="transient")
    assert raised.value.reason == "token"
    assert raised.value.__context__ is None
    assert telemetry.points("docchat.sql.failures") == [
        ({"error_number": 0, "reason": "token"}, 1.0)
    ]
    await sql.dispose()


async def test_emulator_engine_has_no_token(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry)
    assert await sql.ensure_token() is None
    await sql.run(lambda connection: 1, name="read", retry="transient")
    assert driver.calls[0].attrs_before is None
    await sql.dispose()


def test_azure_engine_needs_the_credential() -> None:
    with pytest.raises(ValueError, match="credential"):
        SqlEngine(
            "api",
            sql=SqlSettings(sql_server=SERVER),
            local=LocalSettings(local_emulators=False),
            credential=None,
        )


# --- Idle release --------------------------------------------------------------------------


async def test_idle_pool_is_disposed(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    sql = make_engine(fake_clock, telemetry, idle_minutes=10)
    await sql.run(lambda connection: 1, name="read", retry="transient")
    assert not await sql.release_idle()
    fake_clock.advance(9 * 60)
    assert not await sql.release_idle()
    fake_clock.advance(60)
    assert await sql.release_idle()
    assert telemetry.points("docchat.sql.idle_releases") == [({"service": "api"}, 1.0)]
    # Nothing is left open, so the next check has nothing to release.
    fake_clock.advance(60)
    assert not await sql.release_idle()
    await sql.run(lambda connection: 1, name="read", retry="transient")
    assert len(driver.calls) == 2
    await sql.dispose()


async def test_idle_release_loop_checks_every_minute(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry, idle_minutes=1)
    await sql.run(lambda connection: 1, name="read", retry="transient")
    task = asyncio.create_task(sql.run_idle_release())
    for _ in range(100):
        if telemetry.points("docchat.sql.idle_releases"):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert telemetry.points("docchat.sql.idle_releases") == [({"service": "api"}, 1.0)]
    await sql.dispose()


async def test_in_use_gauge_reports_checked_out_connections(
    driver: FakeDriver, fake_clock: FakeClock, telemetry: Telemetry
) -> None:
    del driver
    sql = make_engine(fake_clock, telemetry)
    seen: list[list[tuple[dict[str, Any], float]]] = []

    def work(connection: Connection) -> None:
        seen.append(telemetry.points("docchat.sql.pool.in_use"))

    await sql.run(work, name="read", retry="transient")
    assert seen == [[({"service": "api"}, 1.0)]]
    assert sql.checked_out() == 0
    assert sql.service == "api"
    await sql.dispose()


# --- The request-path registry -------------------------------------------------------------


def test_request_path_registers_without_changing_the_function() -> None:
    def find_project(project_id: str) -> str:
        return project_id

    registered = request_path(example_params={"project_id": "p"})(find_project)
    assert registered is find_project
    key = f"{__name__}.test_request_path_registers_without_changing_the_function.<locals>"
    entry = REQUEST_PATH_REGISTRY[key + ".find_project"]
    assert entry.function is find_project
    assert dict(entry.example_params) == {"project_id": "p"}
    with pytest.raises(TypeError):
        entry.example_params["x"] = 1  # type: ignore[index]


def test_driver_pooling_is_off() -> None:
    assert pyodbc.pooling is False
