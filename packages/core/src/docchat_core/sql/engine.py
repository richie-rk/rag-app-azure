"""The async SQL data-access layer: the only way any Python process reaches Azure SQL or the local
SQL Server (`01-repository.md` section 6.2; `80-data-model.md` section 2.1; ADR 0026).

No GA async SQL Server driver exists, so SQLAlchemy Core over `pyodbc` runs in a thread pool that
serves SQL only. The event loop fetches Entra access tokens and waits between retries; the SQL
threads open connections, run units of work and nothing else. The synchronous tools (Alembic,
the grants step, the deploy job's grants check) share the connection-string and token-structure
helpers here.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import math
import random
import re
import struct
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

# pyodbc ships a top-level `pyodbc.pyi` without `py.typed`, which mypy doesn't read from
# site-packages, so the driver is untyped here and only here.
import pyodbc  # type: ignore[import-not-found,unused-ignore]
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from sqlalchemy import Connection, Engine, Row, create_engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy.sql.base import Executable

from docchat_core.models.clock import Clock, SystemClock
from docchat_core.settings import LocalSettings, load_settings
from docchat_core.sql.settings import SQL_SERVER_PATTERN, SqlSettings, check_sql_server
from docchat_core.sql.telemetry import INSTRUMENTS, SqlInstruments

# Rule 1: the driver manager's pool would be a second pool that `engine.dispose()` can't empty, so
# the idle release could never let a serverless database pause; the token is also in its key.
pyodbc.pooling = False

type Service = Literal["api", "ingestion", "migrations", "grants", "ops"]
type Retry = Literal["transient", "none"]
type UnavailableReason = Literal["exhausted", "connection", "pool_timeout", "token"]

SERVICES: Final[tuple[Service, ...]] = ("api", "ingestion", "migrations", "grants", "ops")

ODBC_DRIVER: Final = "ODBC Driver 18 for SQL Server"
SQL_PORT: Final = 1433
LOCAL_SQL_SERVER: Final = "127.0.0.1,14330"
LOCAL_SQL_USER: Final = "sa"
TOKEN_SCOPE: Final = "https://database.windows.net/.default"  # noqa: S105 - a scope, not a secret
SQL_COPT_SS_ACCESS_TOKEN: Final = 1256
TOKEN_REFRESH_MARGIN_SECONDS: Final = 300

TRANSIENT_ERROR_NUMBERS: Final = frozenset(
    {
        1205,
        40197,
        40501,
        40613,
        49918,
        49919,
        49920,
        4221,
        10928,
        10929,
        10053,
        10054,
        10060,
        233,
        64,
    }
)
TRANSIENT_SQLSTATES: Final = frozenset({"08S01"})
DEADLOCK_ERROR: Final = 1205
UNIQUE_VIOLATION_ERRORS: Final = frozenset({2627, 2601})
TIMEOUT_SQLSTATE: Final = "HYT00"
DATABASE_RESUMING_ERROR: Final = 40613

MAX_ATTEMPTS: Final = 6
RETRY_BASE_SECONDS: Final = 0.25
RETRY_MAX_DELAY_SECONDS: Final = 8.0
RESUME_MAX_ATTEMPTS: Final = 12
RESUME_MAX_DELAY_SECONDS: Final = 10.0
RESUME_BUDGET_SECONDS: Final = 90.0
JITTER_LOW: Final = 0.8
JITTER_SPAN: Final = 0.4

IDLE_CHECK_INTERVAL_SECONDS: Final = 60
WARM_UP_NAME: Final = "warm_pool"
"""The warm-up's name in its span `docchat.sql.warm_pool` and in `docchat.sql.duration`."""

SPAN_PREFIX: Final = "docchat.sql."
DB_SYSTEM_ATTRIBUTE: Final = "db.system.name"
DB_SYSTEM: Final = "microsoft.sql_server"
ATTEMPTS_ATTRIBUTE: Final = "docchat.sql.attempts"
ERROR_NUMBER_ATTRIBUTE: Final = "docchat.sql.error_number"
OUTCOME_ATTRIBUTE: Final = "docchat.outcome"

_ENGINE_URL = "mssql+pyodbc://"
_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SERVER_RE: Final = re.compile(SQL_SERVER_PATTERN)
_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9_]{1,128}$")
# pyodbc ends each diagnostic record with "(<native error>) (SQL<function>)"; the last match is
# the error's own number even when a quoted key value repeats the pattern earlier in the text.
_ERROR_NUMBER_RE: Final = re.compile(r"\((\d+)\) \(SQL\w+\)")
_CONSTRAINT_RE: Final = re.compile(r"(?:constraint|unique index) '([A-Za-z0-9_]+)'")
_DUPLICATE_VALUE_MARKER: Final = "The duplicate key value is"


# --- Errors ---------------------------------------------------------------------------------


class SqlError(Exception):
    """A failed unit of work, identified by number and SQLSTATE only.

    The server's message text is never kept, because a unique-violation message quotes the
    duplicate key value.
    """

    def __init__(self, number: int, sqlstate: str, constraint: str | None = None) -> None:
        super().__init__(f"SQL error {number} (SQLSTATE {sqlstate or 'none'})")
        self.number = number
        self.sqlstate = sqlstate
        self.constraint = constraint


class SqlUniqueViolation(SqlError):  # noqa: N818 - the name 01-repository.md 6.2 gives
    """Errors 2627 and 2601; `constraint` names the key or unique index."""


class SqlDeadlock(SqlError):  # noqa: N818 - the name 01-repository.md 6.2 gives
    """Error 1205 when the unit of work wasn't retried."""


class SqlTimeout(SqlError):  # noqa: N818 - the name 01-repository.md 6.2 gives
    """SQLSTATE `HYT00`: the statement outran `DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS`."""


class SqlUnavailable(SqlError):  # noqa: N818 - the name 01-repository.md 6.2 gives
    """The database couldn't serve the unit of work: transient retries exhausted, a connection
    failure, a pool timeout or a failed token request."""

    def __init__(self, reason: UnavailableReason, number: int = 0, sqlstate: str = "") -> None:
        super().__init__(number, sqlstate)
        self.reason: UnavailableReason = reason


@dataclass(frozen=True)
class DriverError:
    """What the layer keeps of a driver error: SQLSTATE, native number and, for a unique
    violation, the constraint name."""

    sqlstate: str
    number: int
    constraint: str | None = None

    @property
    def transient(self) -> bool:
        return self.number in TRANSIENT_ERROR_NUMBERS or self.sqlstate in TRANSIENT_SQLSTATES


def describe_driver_error(error: pyodbc.Error) -> DriverError:
    """Read SQLSTATE (`args[0]`), the native number and the constraint from a pyodbc error."""
    args = error.args
    sqlstate = args[0] if args and isinstance(args[0], str) else ""
    message = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
    numbers = _ERROR_NUMBER_RE.findall(message)
    number = int(numbers[-1]) if numbers else 0
    constraint = None
    if number in UNIQUE_VIOLATION_ERRORS:
        found = _CONSTRAINT_RE.search(message.split(_DUPLICATE_VALUE_MARKER, 1)[0])
        constraint = found.group(1) if found else None
    return DriverError(sqlstate=sqlstate, number=number, constraint=constraint)


def driver_error_of(error: BaseException) -> pyodbc.Error | None:
    """The pyodbc error behind an exception, whether SQLAlchemy wrapped it or not."""
    if isinstance(error, DBAPIError) and isinstance(error.orig, pyodbc.Error):
        return error.orig
    if isinstance(error, pyodbc.Error):
        return error
    return None


def sql_error(error: DriverError) -> SqlError:
    """The typed exception for a driver error that isn't retried."""
    if error.number in UNIQUE_VIOLATION_ERRORS:
        return SqlUniqueViolation(error.number, error.sqlstate, error.constraint)
    if error.number == DEADLOCK_ERROR:
        return SqlDeadlock(error.number, error.sqlstate)
    if error.sqlstate == TIMEOUT_SQLSTATE:
        return SqlTimeout(error.number, error.sqlstate)
    if error.transient:
        # A transient error that the unit's retry choice doesn't re-run still means the database
        # couldn't serve it, which callers answer as unavailable rather than as a bug.
        return SqlUnavailable("connection", error.number, error.sqlstate)
    return SqlError(error.number, error.sqlstate)


def retry_delay(attempt: int, error: DriverError, elapsed: float, factor: float) -> float | None:
    """The wait after failed attempt `attempt`, or `None` when its budget is spent.

    `elapsed` is the time since the unit of work started; `factor` the random jitter factor
    from 0.8 to 1.2. Error 40613 (a database resuming from auto-pause) has its own budget of 12
    attempts, waits capped at 10 s and 90 s in total; every other transient error 6 attempts
    and waits capped at 8 s.
    """
    exponential = math.ldexp(RETRY_BASE_SECONDS, attempt - 1)
    if error.number == DATABASE_RESUMING_ERROR:
        if attempt >= RESUME_MAX_ATTEMPTS:
            return None
        delay = min(RESUME_MAX_DELAY_SECONDS, exponential) * factor
        return delay if elapsed + delay <= RESUME_BUDGET_SECONDS else None
    if attempt >= MAX_ATTEMPTS:
        return None
    return min(RETRY_MAX_DELAY_SECONDS, exponential) * factor


# --- Connection strings and tokens -----------------------------------------------------------


def _odbc_value(value: str) -> str:
    # ODBC's rule: a value holding a delimiter is wrapped in braces with each `}` doubled; the
    # generated local `sa` password may hold `;`, `{` or `}`.
    if any(char in value for char in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _check_service(service: str) -> None:
    if service not in SERVICES:
        raise ValueError("unknown SQL service name")


def azure_connection_string(service: Service, *, server: str, database: str) -> str:
    """The Azure form: Entra token authentication (no credential in the string), TDS 8.0 and
    `APP=docchat-<service>`, so `APP_NAME()` and the SQL audit name the component."""
    _check_service(service)
    if not _SERVER_RE.fullmatch(server) or not _IDENTIFIER_RE.fullmatch(database):
        raise ValueError("not a SQL server or database name")
    # ConnectRetryCount=0 leaves every reconnect to the layer's counted, traced retries.
    return (
        f"Driver={{{ODBC_DRIVER}}};Server=tcp:{server},{SQL_PORT};Database={database};"
        f"Encrypt=strict;ConnectRetryCount=0;APP=docchat-{service}"
    )


def emulator_connection_string(
    service: Service,
    *,
    password: str,
    database: str,
    user: str = LOCAL_SQL_USER,
    server: str = LOCAL_SQL_SERVER,
) -> str:
    """The local-emulator form: SQL authentication against the `compose.yaml` SQL Server, which
    has no Entra authentication and a self-signed certificate. Test fixtures pass their own
    database and per-role login."""
    _check_service(service)
    if not _IDENTIFIER_RE.fullmatch(database) or not _IDENTIFIER_RE.fullmatch(user):
        raise ValueError("not a SQL database or login name")
    if server != LOCAL_SQL_SERVER and not re.fullmatch(r"[A-Za-z0-9.-]+,\d{1,5}", server):
        raise ValueError("not a SQL server address")
    return (
        f"Driver={{{ODBC_DRIVER}}};Server={server};Database={database};"
        f"UID={_odbc_value(user)};PWD={_odbc_value(password)};"
        f"Encrypt=yes;TrustServerCertificate=yes;ConnectRetryCount=0;APP=docchat-{service}"
    )


def connection_string(
    service: Service, sql: SqlSettings | None = None, local: LocalSettings | None = None
) -> str:
    """The only builder of the configured target's connection string; settings default to the
    process environment's."""
    sql = load_settings(SqlSettings) if sql is None else sql
    local = load_settings(LocalSettings) if local is None else local
    check_sql_server(sql, local)
    if local.local_emulators and local.local_sql_sa_password is not None:
        return emulator_connection_string(
            service,
            password=local.local_sql_sa_password.get_secret_value(),
            database=sql.sql_database,
        )
    if sql.sql_server is None:
        raise ValueError("no SQL server is configured")
    return azure_connection_string(service, server=sql.sql_server, database=sql.sql_database)


def access_token_attribute(token: str) -> bytes:
    """Pre-connect attribute 1256 (`SQL_COPT_SS_ACCESS_TOKEN`): a 4-byte little-endian length
    followed by the token in UTF-16-LE, with no terminator."""
    encoded = token.encode("utf-16-le")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)


def open_connection(
    odbc: str, *, access_token: str | None, login_timeout: int, command_timeout: int
) -> pyodbc.Connection:
    """The one call of `pyodbc.connect`; the token, when given, travels only as attribute 1256."""
    attrs_before = (
        {SQL_COPT_SS_ACCESS_TOKEN: access_token_attribute(access_token)}
        if access_token is not None
        else None
    )
    connection = pyodbc.connect(
        odbc, autocommit=False, timeout=login_timeout, attrs_before=attrs_before
    )
    connection.timeout = command_timeout
    return connection


def create_sync_engine(
    service: Service,
    *,
    access_token: str | None,
    sql: SqlSettings | None = None,
    local: LocalSettings | None = None,
) -> Engine:
    """An engine for the synchronous tools, which run outside any event loop: the migrations
    (`migrations`) and the grants step and check (`grants`). Outside local-emulator mode the
    caller passes the token it took from the Azure CLI credential."""
    sql = load_settings(SqlSettings) if sql is None else sql
    local = load_settings(LocalSettings) if local is None else local
    odbc = connection_string(service, sql, local)
    token = None if local.local_emulators else access_token
    if token is None and not local.local_emulators:
        raise ValueError("an Azure SQL connection needs an access token")
    creator = partial(
        open_connection,
        odbc,
        access_token=token,
        login_timeout=sql.sql_connect_timeout_seconds,
        command_timeout=sql.sql_command_timeout_seconds,
    )
    return create_engine(_ENGINE_URL, creator=creator, poolclass=NullPool)


# --- The request-path registry --------------------------------------------------------------


@dataclass(frozen=True)
class RequestPathEntry:
    """A repository function on a request path and the parameters the plan audit runs it with."""

    function: Callable[..., Any]
    example_params: Mapping[str, Any]


REQUEST_PATH_REGISTRY: Final[dict[str, RequestPathEntry]] = {}
"""Every `@request_path` function by qualified name, read by the query-plan audit."""


def request_path[F: Callable[..., Any]](*, example_params: Mapping[str, Any]) -> Callable[[F], F]:
    """Register a repository function for the query-plan audit; no run-time effect."""

    def register(function: F) -> F:
        key = f"{function.__module__}.{function.__qualname__}"
        REQUEST_PATH_REGISTRY[key] = RequestPathEntry(
            function=function, example_params=MappingProxyType(dict(example_params))
        )
        return function

    return register


# --- The async engine ------------------------------------------------------------------------


class AccessTokenLike(Protocol):
    @property
    def token(self) -> str: ...

    @property
    def expires_on(self) -> int: ...


class AsyncTokenSource(Protocol):
    """The process credential of `docchat_core.clients` (`ManagedIdentityCredential`, or
    `AzureCliCredential` in `local`), seen only through `get_token`."""

    def get_token(self, *scopes: str) -> Awaitable[AccessTokenLike]: ...


class _PoolTimeoutError(Exception):
    """No pooled connection within `DOCCHAT_SQL_POOL_TIMEOUT_SECONDS`."""


class _ConnectError(Exception):
    """The connection couldn't be opened or checked out; nothing of the unit ran."""

    def __init__(self, error: DriverError) -> None:
        super().__init__("connection failed")
        self.error = error


class _WorkError(Exception):
    """A driver error inside the unit of work; its transaction is rolled back."""

    def __init__(self, error: DriverError) -> None:
        super().__init__("unit of work failed")
        self.error = error


@dataclass
class _Progress:
    attempts: int = 0
    error_number: int = 0


@dataclass
class _PoolState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    checked_out: int = 0
    open_connections: int = 0
    last_activity: float = 0.0


class SqlEngine:
    """One per process: the connection pool, the SQL thread pool, the token cache and the
    idle release. Created in the API lifespan (each worker, after the fork) or at the ingestion
    worker's first invocation, warmed there (`warm`), and disposed at shutdown."""

    def __init__(
        self,
        service: Service,
        *,
        sql: SqlSettings,
        local: LocalSettings,
        credential: AsyncTokenSource | None,
        clock: Clock | None = None,
        odbc_connection_string: str | None = None,
        tracer: Tracer | None = None,
        instruments: SqlInstruments | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        _check_service(service)
        uses_token = not local.local_emulators and odbc_connection_string is None
        if uses_token and credential is None:
            raise ValueError("an Azure SQL connection needs the process credential")
        self._service: Service = service
        self._sql = sql
        self._odbc = (
            connection_string(service, sql, local)
            if odbc_connection_string is None
            else odbc_connection_string
        )
        self._credential = credential if uses_token else None
        self._clock: Clock = SystemClock() if clock is None else clock
        self._tracer = trace.get_tracer(__name__) if tracer is None else tracer
        self._instruments = INSTRUMENTS if instruments is None else instruments
        self._sleep = sleep
        self._jitter = jitter
        self._token: AccessTokenLike | None = None
        self._token_lock = asyncio.Lock()
        self._pool_state = _PoolState(last_activity=self._clock.monotonic())
        self._executor = ThreadPoolExecutor(
            max_workers=sql.sql_threads, thread_name_prefix="docchat-sql"
        )
        # At most one piece of work per SQL thread is handed over; the rest wait on the loop.
        # Without it the threads run queued units back to back on a warm pool and hold the
        # interpreter lock so often that the loop falls behind (T-522).
        self._slots = asyncio.Semaphore(sql.sql_threads)
        # The pool holds exactly as many connections as the thread pool has threads, so a
        # thread never waits for another thread's connection.
        self._engine = create_engine(
            _ENGINE_URL,
            creator=self._connect,
            poolclass=QueuePool,
            pool_size=sql.sql_threads,
            max_overflow=0,
            pool_timeout=sql.sql_pool_timeout_seconds,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
        )
        event.listen(self._engine, "connect", self._on_connect)
        event.listen(self._engine, "close", self._on_close)
        event.listen(self._engine, "checkout", self._on_checkout)
        event.listen(self._engine, "checkin", self._on_checkin)
        self._instruments.watch_pool(self)

    @property
    def service(self) -> str:
        return self._service

    def checked_out(self) -> int:
        with self._pool_state.lock:
            return self._pool_state.checked_out

    # Pool events run in the SQL threads.

    def _on_connect(self, *_: object) -> None:
        with self._pool_state.lock:
            self._pool_state.open_connections += 1

    def _on_close(self, *_: object) -> None:
        with self._pool_state.lock:
            self._pool_state.open_connections -= 1

    def _on_checkout(self, *_: object) -> None:
        with self._pool_state.lock:
            self._pool_state.checked_out += 1
            self._pool_state.last_activity = self._clock.monotonic()

    def _on_checkin(self, *_: object) -> None:
        with self._pool_state.lock:
            self._pool_state.checked_out -= 1
            self._pool_state.last_activity = self._clock.monotonic()

    def _connect(self) -> pyodbc.Connection:
        # Runs in a SQL thread: it only reads the token the loop cached, so no identity call
        # ever runs here.
        token = None
        if self._credential is not None:
            if self._token is None:
                raise SqlUnavailable("token")
            token = self._token.token
        return open_connection(
            self._odbc,
            access_token=token,
            login_timeout=self._sql.sql_connect_timeout_seconds,
            command_timeout=self._sql.sql_command_timeout_seconds,
        )

    # Tokens.

    def _token_fresh(self) -> bool:
        if self._token is None:
            return False
        remaining = self._token.expires_on - self._clock.now().timestamp()
        return remaining > TOKEN_REFRESH_MARGIN_SECONDS

    async def ensure_token(self) -> str | None:
        """The cached access token, refreshed once under a lock when it expires within 300 s;
        `None` when the engine authenticates without a token (local emulators, test logins)."""
        if self._credential is None:
            return None
        if self._token_fresh() and self._token is not None:
            return self._token.token
        async with self._token_lock:
            # The others waited for the one fetch; they find the fresh token here.
            if not self._token_fresh():
                fetched = await self._fetch_token(self._credential)
                if fetched is None:
                    raise SqlUnavailable("token")
                self._token = fetched
            if self._token is None:
                raise SqlUnavailable("token")
            return self._token.token

    async def _fetch_token(self, credential: AsyncTokenSource) -> AccessTokenLike | None:
        try:
            return await credential.get_token(TOKEN_SCOPE)
        except Exception:  # noqa: BLE001 - any credential failure is the one outcome "token"
            # The credential's message can name the identity endpoint's answer; only the reason
            # leaves the layer.
            return None

    # Units of work.

    async def _in_thread[T](self, function: Callable[[], T]) -> T:
        # A cancelled caller stops waiting, but a unit already running in its thread still runs
        # to its commit or rollback, so it is never half-applied. The caller's context goes with
        # it, so code inside the unit (an audit row's correlation ID) sees the current span.
        context = contextvars.copy_context()
        async with self._slots:
            return await asyncio.wrap_future(self._executor.submit(context.run, function))

    def _checkout(self) -> Connection:
        # In a SQL thread: the pool's checkout, which opens a connection through `_connect`
        # when none is free.
        started = self._clock.monotonic()
        try:
            return self._engine.connect()
        except PoolTimeoutError:
            raise _PoolTimeoutError from None
        except (DBAPIError, pyodbc.Error) as error:
            driver = driver_error_of(error)
            if driver is None:
                raise
            raise _ConnectError(describe_driver_error(driver)) from None
        finally:
            elapsed_ms = (self._clock.monotonic() - started) * 1000
            self._instruments.pool_wait.record(elapsed_ms, {"service": self._service})

    def _attempt[T](self, work: Callable[[Connection], T]) -> T:
        connection = self._checkout()
        try:
            with connection, connection.begin():
                return work(connection)
        except (DBAPIError, pyodbc.Error) as error:
            driver = driver_error_of(error)
            if driver is None:
                raise
            raise _WorkError(describe_driver_error(driver)) from None

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        """Run `work` on one connection in one SQL thread inside `connection.begin()`, re-running
        it as a whole on a transient error when `retry` is `"transient"`.

        `name` is the unit's statement-set name, used for the span `docchat.sql.<name>` and the
        metrics. A unit that writes chooses `"transient"` only when it is idempotent.
        """
        if not _NAME_RE.fullmatch(name):
            raise ValueError("a statement-set name is lower-case snake case")
        started = self._clock.monotonic()
        progress = _Progress()
        outcome = "error"
        with self._tracer.start_as_current_span(
            SPAN_PREFIX + name,
            kind=SpanKind.CLIENT,
            attributes={DB_SYSTEM_ATTRIBUTE: DB_SYSTEM},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = await self._attempts(work, retry, progress, started)
                outcome = "ok"
            except SqlError as failure:
                self._instruments.failures.add(
                    1, {"error_number": failure.number, "reason": _failure_reason(failure)}
                )
                raise
            finally:
                span.set_attribute(ATTEMPTS_ATTRIBUTE, progress.attempts)
                span.set_attribute(ERROR_NUMBER_ATTRIBUTE, progress.error_number)
                span.set_attribute(OUTCOME_ATTRIBUTE, outcome)
                if outcome != "ok":
                    span.set_status(Status(StatusCode.ERROR))
                duration_ms = (self._clock.monotonic() - started) * 1000
                self._instruments.duration.record(
                    duration_ms, {"operation": name, "outcome": outcome}
                )
        return result

    async def _attempts[T](
        self, work: Callable[[Connection], T], retry: Retry, progress: _Progress, started: float
    ) -> T:
        while True:
            progress.attempts += 1
            # A token close to expiry is refreshed before every attempt, on the loop.
            await self.ensure_token()
            failure: DriverError
            try:
                return await self._in_thread(partial(self._attempt, work))
            except _PoolTimeoutError:
                failure_reason: UnavailableReason | None = "pool_timeout"
                failure = DriverError(sqlstate="", number=0)
            except _ConnectError as connect_failure:
                failure_reason = "connection"
                failure = connect_failure.error
            except _WorkError as work_failure:
                failure_reason = None
                failure = work_failure.error
            # Raised outside the `except` blocks, so no driver exception (whose text can quote a
            # duplicate key) is chained to what the caller sees.
            progress.error_number = failure.number
            delay = None
            if retry == "transient" and failure.transient and failure_reason != "pool_timeout":
                elapsed = self._clock.monotonic() - started
                factor = JITTER_LOW + JITTER_SPAN * self._jitter()
                delay = retry_delay(progress.attempts, failure, elapsed, factor)
                if delay is None:
                    raise SqlUnavailable("exhausted", failure.number, failure.sqlstate)
            if delay is None:
                if failure_reason is not None:
                    raise SqlUnavailable(failure_reason, failure.number, failure.sqlstate)
                raise sql_error(failure)
            self._instruments.retries.add(1, {"error_number": failure.number})
            await self._sleep(delay)

    async def fetch_one(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: Retry = "transient",
    ) -> Row[Any] | None:
        """One statement's first row, or `None`."""
        return await self.run(
            lambda connection: connection.execute(statement, parameters).first(),
            name=name,
            retry=retry,
        )

    async def fetch_all(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: Retry = "transient",
    ) -> Sequence[Row[Any]]:
        """Every row of one statement."""
        return await self.run(
            lambda connection: connection.execute(statement, parameters).all(),
            name=name,
            retry=retry,
        )

    async def execute(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: Retry,
    ) -> int:
        """One write; returns the affected row count. The retry choice is always explicit."""

        def write(connection: Connection) -> int:
            return connection.execute(statement, parameters).rowcount

        return await self.run(write, name=name, retry=retry)

    # Warm-up.

    async def warm(self) -> int:
        """Open every pooled connection before the process serves (`01-repository.md` section
        6.2 item 11); returns how many are open.

        The token is fetched on the loop first. Then each SQL thread checks one connection out
        of the pool, which opens it through `_connect` and the pool's events exactly as a unit
        of work's checkout does, and holds it until every thread has one, so no connection is
        handed out twice, then checks it back in. The warm-up waits at most
        `DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS` and raises no `SqlError`: a failed checkout or
        the bound ends it, recorded on the span `docchat.sql.warm_pool` and in
        `docchat.sql.failures`; the connections that opened stay in the pool and the others
        open at first use.
        """
        threads = self._sql.sql_threads
        bound = self._sql.sql_connect_timeout_seconds
        barrier = threading.Barrier(threads, timeout=bound)
        opened: list[None] = []
        started = self._clock.monotonic()
        failure: SqlError | None = None
        outcome = "error"
        with self._tracer.start_as_current_span(
            SPAN_PREFIX + WARM_UP_NAME,
            kind=SpanKind.CLIENT,
            attributes={DB_SYSTEM_ATTRIBUTE: DB_SYSTEM},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                try:
                    async with asyncio.timeout(bound):
                        await self.ensure_token()
                        results = await asyncio.gather(
                            *(
                                self._in_thread(partial(self._hold_open, barrier, opened))
                                for _ in range(threads)
                            ),
                            return_exceptions=True,
                        )
                except TimeoutError:
                    failure = SqlUnavailable("connection")
                except SqlError as token_failure:
                    failure = token_failure
                else:
                    failure = _first_warm_failure(results)
                finally:
                    # Threads still waiting for the others check their connections in now.
                    barrier.abort()
                if failure is None:
                    outcome = "ok"
                else:
                    self._instruments.failures.add(
                        1, {"error_number": failure.number, "reason": _failure_reason(failure)}
                    )
            finally:
                span.set_attribute(ATTEMPTS_ATTRIBUTE, 1)
                span.set_attribute(ERROR_NUMBER_ATTRIBUTE, 0 if failure is None else failure.number)
                span.set_attribute(OUTCOME_ATTRIBUTE, outcome)
                if outcome != "ok":
                    span.set_status(Status(StatusCode.ERROR))
                duration_ms = (self._clock.monotonic() - started) * 1000
                self._instruments.duration.record(
                    duration_ms, {"operation": WARM_UP_NAME, "outcome": outcome}
                )
        return len(opened)

    def _hold_open(self, barrier: threading.Barrier, opened: list[None]) -> None:
        # In a SQL thread: one checkout, held until every thread of the warm-up has one.
        try:
            connection = self._checkout()
        except BaseException:
            # The others stop waiting for a connection that won't come.
            barrier.abort()
            raise
        with connection:
            opened.append(None)
            # Broken when another checkout failed or the warm-up stopped waiting; this
            # connection stays open in the pool either way.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()

    # Idle release and shutdown.

    async def release_idle(self) -> bool:
        """Dispose the pool when no connection is checked out, none was used for
        `DOCCHAT_SQL_IDLE_RELEASE_MINUTES` and at least one is open, so a serverless database
        can pause; the next unit of work reconnects."""
        idle_seconds = self._sql.sql_idle_release_minutes * 60
        with self._pool_state.lock:
            state = self._pool_state
            idle = (
                state.checked_out == 0
                and state.open_connections > 0
                and self._clock.monotonic() - state.last_activity >= idle_seconds
            )
        if not idle:
            return False
        await self._in_thread(self._engine.dispose)
        self._instruments.idle_releases.add(1, {"service": self._service})
        return True

    async def run_idle_release(self) -> None:
        """The background task of the API lifespan and of each ingestion worker process."""
        while True:
            await self._sleep(IDLE_CHECK_INTERVAL_SECONDS)
            await self.release_idle()

    async def dispose(self) -> None:
        """Close every pooled connection, then the SQL threads."""
        await self._in_thread(self._engine.dispose)
        self._executor.shutdown(wait=False, cancel_futures=True)


def _first_warm_failure(results: Sequence[object]) -> SqlError | None:
    """The first failed checkout of a warm-up as the layer reports it; any other exception is a
    bug and is raised."""
    for result in results:
        if result is None:
            continue
        if isinstance(result, _ConnectError):
            return SqlUnavailable("connection", result.error.number, result.error.sqlstate)
        if isinstance(result, _PoolTimeoutError):
            return SqlUnavailable("pool_timeout")
        if isinstance(result, SqlError):
            return result
        if isinstance(result, BaseException):
            raise result
    return None


def _failure_reason(failure: SqlError) -> str:
    if isinstance(failure, SqlUnavailable) and failure.reason in {
        "exhausted",
        "pool_timeout",
        "token",
    }:
        return failure.reason
    return "not_retryable"
