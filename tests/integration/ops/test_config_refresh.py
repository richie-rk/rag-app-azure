"""The ops-config refresh integration test (T-121; `81-config.md` section 3.1 items 3 and 4,
section 3.3 item 3; ADR 0028), with the start-up half of the ops-config seed integration test
(T-086), against the SQL Server of `compose.yaml`.

The module builds its own database `docchat_test_<8 hex>` and applies the revisions to it through
their offline T-SQL (`alembic upgrade head --sql`), because `DOCCHAT_SQL_DATABASE` admits only
`docchat`; every test then finds exactly the rows the migrations wrote. The store runs as a
running API process does, on the data-access layer, with a fake clock in place of waiting.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from azure.core.credentials import AccessToken
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, create_engine, delete, insert, select
from sqlalchemy.pool import NullPool

from docchat_core.models import SystemClock
from docchat_core.opsconfig import (
    API_AREAS,
    INGESTION_AREAS,
    OpsConfigInvalidError,
    OpsConfigStore,
)
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import (
    Retry,
    SqlEngine,
    emulator_connection_string,
    open_connection,
)
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import audit_events, ops_config
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.framework.auth import AzResult

CHAT_SMALL_EFFORTS = ("none", "low", "medium", "high")
"""The chat-small model's row of the capability table (`12-chat-pipeline.md` section 10.7)."""

OPERATOR_OID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
DATABASE_RE = re.compile(r"^docchat_test_[0-9a-f]{8}$")


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


def _sa_password() -> str:
    password = os.environ.get("DOCCHAT_LOCAL_SQL_SA_PASSWORD")
    if not password:
        pytest.fail("DOCCHAT_LOCAL_SQL_SA_PASSWORD is unset: run docchat-ops local init")
    return password


def _alembic_ini() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "db" / "alembic.ini"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("db/alembic.ini")


def _migration_batches() -> list[str]:
    """The T-SQL batches of every revision, in order, as `alembic upgrade head --sql` renders
    them."""
    buffer = io.StringIO()
    command.upgrade(Config(str(_alembic_ini()), output_buffer=buffer), "head", sql=True)
    batches = []
    for batch in re.split(r"\n\s*GO\s*\n", buffer.getvalue()):
        lines = [line for line in batch.splitlines() if not line.startswith("-- ")]
        text = "\n".join(lines).strip()
        if text:
            batches.append(text)
    return batches


def _sa_engine(password: str, database: str) -> Engine:
    odbc = emulator_connection_string("migrations", password=password, database=database)
    connect = partial(
        open_connection, odbc, access_token=None, login_timeout=30, command_timeout=300
    )
    return create_engine(
        "mssql+pyodbc://", creator=connect, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """A fresh database at the head revision, dropped when the module ends."""
    password = _sa_password()
    name = f"docchat_test_{secrets.token_hex(4)}"
    assert DATABASE_RE.fullmatch(name)
    master = _sa_engine(password, "master")
    with master.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE [{name}]")
    try:
        migrated = _sa_engine(password, name)
        with migrated.connect() as connection:
            # One connection, so each revision's BEGIN TRANSACTION and COMMIT batches pair up.
            for batch in _migration_batches():
                connection.exec_driver_sql(batch)
        migrated.dispose()
        yield name
    finally:
        with master.connect() as connection:
            connection.exec_driver_sql(
                f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            connection.exec_driver_sql(f"DROP DATABASE [{name}]")
        master.dispose()


@pytest.fixture
async def engine(database: str) -> AsyncIterator[SqlEngine]:
    """The API's data-access layer on the test database; the ops-config rows are put back as the
    migrations wrote them after each test."""
    password = _sa_password()
    sql_engine = SqlEngine(
        "api",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(local_emulators=True, local_sql_sa_password=SecretStr(password)),
        credential=None,
        odbc_connection_string=emulator_connection_string(
            "api", password=password, database=database
        ),
    )
    seeded = await sql_engine.fetch_all(
        select(ops_config.c.config_key, ops_config.c.value_json, ops_config.c.default_json),
        name="test_read_ops_config_seed",
    )
    rows = [dict(row._mapping) for row in seeded]

    def restore(connection: Connection) -> None:
        connection.execute(delete(ops_config))
        connection.execute(insert(ops_config), rows)

    try:
        yield sql_engine
    finally:
        await sql_engine.run(restore, name="test_restore_ops_config", retry="none")
        await sql_engine.dispose()


def api_store(engine: SqlEngine, clock: FakeClock | None = None) -> OpsConfigStore:
    return OpsConfigStore(
        engine, API_AREAS, chat_small_efforts=CHAT_SMALL_EFFORTS, clock=clock or FakeClock()
    )


def _unsigned_token(oid: UUID) -> str:
    """An access token carrying only `oid`: `docchat-ops` reads its actor from the SQL token's
    claim, and the compose SQL Server signs in with SQL authentication, so no real token exists."""
    claims = base64.urlsafe_b64encode(json.dumps({"oid": str(oid)}).encode()).decode()
    return f"e30.{claims.rstrip('=')}.unsigned"


class _OperatorDatabase:
    """The test database as `docchat-ops` reaches it: the test's data-access layer, with the
    operator's token standing in for the Entra token of an Azure database."""

    def __init__(self, engine: SqlEngine) -> None:
        self._engine = engine

    async def ensure_token(self) -> str | None:
        return _unsigned_token(OPERATOR_OID)

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        return await self._engine.run(work, name=name, retry=retry)

    async def dispose(self) -> None:
        """The `engine` fixture disposes the layer after the test."""


class _PersonAz:
    async def run(self, args: Sequence[str]) -> AzResult:
        assert list(args[:2]) == ["account", "show"]
        return AzResult(returncode=0, stdout="user\n", stderr="")


class _NoCredential:
    """The credential the command opens and closes; the SQL layer above takes no token from it."""

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        raise AssertionError("the test database takes no Entra token")

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _NoCredential:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _no_vault(uri: str, credential: object) -> Any:
    raise AssertionError("config set reads no vault")


async def config_set(engine: SqlEngine, key: str, value: Any) -> str:
    """`docchat-ops --env dev config set` (`81-config.md` section 3.3 item 3) against the test
    database: validation with the area's model, the row update on its `row_version` and the audit
    row in one transaction. Returns the run's correlation ID; the command's refusals are the
    `docchat-ops config` integration tests' subject (T-122)."""
    with tempfile.TemporaryDirectory() as checkout:
        root = Path(checkout)
        (root / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
        (root / ".azure" / "dev").mkdir(parents=True)
        (root / ".azure" / "dev" / ".env").write_text(
            'DOCCHAT_SQL_SERVER="sql-docchat-dev-test01.database.windows.net"\n'
            'DOCCHAT_SQL_DATABASE="docchat"\n',
            encoding="utf-8",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        toolkit = Toolkit(
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=stderr,
            environ={},
            clock=SystemClock(),
            sleep=asyncio.sleep,
            working_directory=root,
            az=_PersonAz(),
            credential=_NoCredential,
            database=lambda target, credential: _OperatorDatabase(engine),
            vault=_no_vault,
        )
        code = await run_cli(
            [
                "--env",
                "dev",
                "config",
                "set",
                "--key",
                key,
                "--value",
                json.dumps(value),
                "--ticket",
                "T-121",
                "--reason",
                "T-121 refresh integration test",
                "--yes",
            ],
            toolkit,
        )
    assert code == 0, stderr.getvalue()
    return stderr.getvalue().splitlines()[0].removeprefix("correlationId: ")


async def test_t086_the_start_up_validation_of_both_apps_accepts_the_seeded_rows(
    engine: SqlEngine,
) -> None:
    api = await api_store(engine).start()
    assert api.chat.context_token_budget == 40_000
    ingestion = await OpsConfigStore(engine, INGESTION_AREAS, clock=FakeClock()).start()
    assert ingestion.ingestion.max_parallel_items == 1


async def test_t121_start_up_fails_when_a_registered_area_misses_a_seeded_key(
    engine: SqlEngine,
) -> None:
    await engine.execute(
        delete(ops_config).where(ops_config.c.config_key == "chat.max_turn_seconds"),
        name="test_delete_ops_config_row",
        retry="none",
    )
    with pytest.raises(OpsConfigInvalidError) as raised:
        await api_store(engine).start()
    assert raised.value.failures == {"chat": ("chat.max_turn_seconds",)}
    # A process that doesn't register the area starts.
    await OpsConfigStore(engine, INGESTION_AREAS, clock=FakeClock()).start()


async def test_t121_a_value_changed_by_config_set_is_seen_within_60_s(engine: SqlEngine) -> None:
    clock = FakeClock()
    store = api_store(engine, clock)
    before = await store.start()
    assert before.ratelimit.chat_requests_per_minute == 3
    correlation_id = await config_set(engine, "ratelimit.chat_requests_per_minute", 4)
    audit = await engine.fetch_one(
        select(audit_events.c.action, audit_events.c.actor_object_id).where(
            audit_events.c.correlation_id == correlation_id
        ),
        name="test_read_audit_event",
    )
    assert audit is not None
    assert (audit.action, audit.actor_object_id) == ("ops_config.set", OPERATOR_OID)
    clock.advance(59)
    assert (await store.snapshot()).ratelimit.chat_requests_per_minute == 3
    clock.advance(1)
    assert (await store.snapshot()).ratelimit.chat_requests_per_minute == 4


async def test_rows_of_an_unknown_area_are_ignored(engine: SqlEngine) -> None:
    await engine.execute(
        insert(ops_config).values(
            config_key="future.setting", value_json='{"value":1}', default_json='{"value":1}'
        ),
        name="test_insert_ops_config_row",
        retry="none",
    )
    snapshot = await api_store(engine).start()
    assert snapshot.api.write_requests_per_minute == 120
