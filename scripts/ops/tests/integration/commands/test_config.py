"""T-122, the `docchat-ops config` integration tests (`81-config.md` section 3; `86-operations.md`
section 2.5; `80-data-model.md` sections 2.7 and 2.21).

Against the SQL Server container, `config set` refuses an unknown key, an out-of-range value and a
reason outside 10 to 500 characters with exit 2 and no write; a stale `row_version` exits 6 with no
write; a success writes the row and one `ops_config.set` audit row in one transaction, and a
forced audit failure rolls the change back.

The CLI runs through `run_cli` on the test's loop with the real parser and handlers. Its SQL
layer is the data-access layer as the `docchat_config_operator` role's login; rows are read back
as `sa`. The container signs in with SQL authentication, so an unsigned token carrying only `oid`
stands in for the operator's Entra token.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from azure.core.credentials import AccessToken
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, select, update

from docchat_core.audit import AuditEvent, write_audit_event
from docchat_core.models import SystemClock
from docchat_core.opsconfig import encode_value
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Retry, SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import audit_events, ops_config
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.commands import config
from docchat_ops.framework.auth import AzResult
from docchat_ops.framework.exitcodes import ExitCode
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

OPERATOR_OID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
KEY = "api.write_requests_per_minute"
SEEDED = 120
REASON = "raise the write limit for the pilot"
TICKET = "CHG-2026-122"


def _unsigned_token(oid: UUID) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"oid": str(oid)}).encode()).decode()
    return f"e30.{claims.rstrip('=')}.unsigned"


@dataclass
class _OperatorDatabase:
    """The test database as `docchat-ops` reaches it, as the config operator role;
    `between` runs after the named unit of work, before the next one."""

    engine: SqlEngine
    between: dict[str, Callable[[], None]] = field(default_factory=dict)

    async def ensure_token(self) -> str | None:
        return _unsigned_token(OPERATOR_OID)

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        result = await self.engine.run(work, name=name, retry=retry)
        action = self.between.pop(name, None)
        if action is not None:
            await asyncio.to_thread(action)
        return result

    async def dispose(self) -> None:
        """The `operator` fixture disposes of the layer after the test."""


class _PersonAz:
    async def run(self, args: Sequence[str]) -> AzResult:
        assert list(args[:2]) == ["account", "show"]
        return AzResult(returncode=0, stdout="user\n", stderr="")


class _NoCredential:
    """The credential the command opens and closes; the SQL layer takes no token from it."""

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        raise AssertionError("the test database takes no Entra token")

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _NoCredential:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _no_vault(uri: str, credential: object) -> Any:
    raise AssertionError("config reads no vault")


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def correlation_id(self) -> str:
        return self.stderr.splitlines()[0].removeprefix("correlationId: ")


@dataclass(frozen=True)
class Cli:
    checkout: Path
    database: _OperatorDatabase

    async def run(self, *argv: str) -> Outcome:
        stdout, stderr = io.StringIO(), io.StringIO()
        toolkit = Toolkit(
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=stderr,
            environ={},
            clock=SystemClock(),
            sleep=asyncio.sleep,
            working_directory=self.checkout,
            az=_PersonAz(),
            credential=_NoCredential,
            database=lambda target, credential: self.database,
            vault=_no_vault,
        )
        code = await run_cli(["--env", "dev", "--json", "config", *argv], toolkit)
        return Outcome(code, stdout.getvalue(), stderr.getvalue())

    async def set(self, key: str, value: str, *, reason: str = REASON) -> Outcome:
        return await self.run(
            "set", "--key", key, "--value", value, "--ticket", TICKET, "--reason", reason, "--yes"
        )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
    (tmp_path / ".azure" / "dev").mkdir(parents=True)
    (tmp_path / ".azure" / "dev" / ".env").write_text(
        'DOCCHAT_SQL_SERVER="sql-docchat-dev-test01.database.windows.net"\n'
        'DOCCHAT_SQL_DATABASE="docchat"\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
async def operator(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = SqlEngine(
        "ops",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_config_operator"),
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def database(operator: SqlEngine) -> _OperatorDatabase:
    return _OperatorDatabase(operator)


@pytest.fixture
def cli(checkout: Path, database: _OperatorDatabase) -> Cli:
    return Cli(checkout, database)


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def _stored(admin: Engine) -> Any:
    with admin.connect() as connection:
        return connection.execute(select(ops_config).where(ops_config.c.config_key == KEY)).one()


def _audit_rows(admin: Engine) -> list[Any]:
    with admin.connect() as connection:
        return list(connection.execute(select(audit_events).order_by(audit_events.c.id)))


async def stored(admin: Engine) -> Any:
    """The key's row, read in a worker thread, off the test's guarded loop."""
    return await asyncio.to_thread(_stored, admin)


async def audit_rows(admin: Engine) -> list[Any]:
    return await asyncio.to_thread(_audit_rows, admin)


async def nothing_written(admin: Engine, before: Any) -> None:
    after = await stored(admin)
    assert (after.value_json, after.row_version) == (before.value_json, before.row_version)
    assert await audit_rows(admin) == []


# T-122


async def test_t122_an_unknown_key_exits_2_with_no_write(cli: Cli, admin: Engine) -> None:
    before = await stored(admin)

    outcome = await cli.set("api.no_such_key", "60")

    assert outcome.code == ExitCode.USAGE
    await nothing_written(admin, before)


async def test_t122_an_out_of_range_value_exits_2_with_no_write(cli: Cli, admin: Engine) -> None:
    before = await stored(admin)

    outcome = await cli.set(KEY, "5000")

    assert outcome.code == ExitCode.USAGE
    assert "less than or equal to 1200" in outcome.stderr
    await nothing_written(admin, before)


@pytest.mark.parametrize("length", [9, 501])
async def test_t122_a_reason_outside_10_to_500_characters_exits_2_with_no_write(
    cli: Cli, admin: Engine, length: int
) -> None:
    before = await stored(admin)

    outcome = await cli.set(KEY, "60", reason="r" * length)

    assert outcome.code == ExitCode.USAGE
    await nothing_written(admin, before)


async def test_t122_a_stale_row_version_exits_6_with_no_write(
    cli: Cli, admin: Engine, database: _OperatorDatabase
) -> None:
    def someone_else_changes_the_row() -> None:
        with admin.begin() as connection:
            connection.execute(
                update(ops_config)
                .where(ops_config.c.config_key == KEY)
                .values(value_json=encode_value(90))
            )

    database.between[config.READ_UNIT] = someone_else_changes_the_row
    before_race = await stored(admin)

    outcome = await cli.set(KEY, "60")

    assert outcome.code == ExitCode.CONFLICT
    assert "changed by someone else" in outcome.stderr
    after = await stored(admin)
    assert json.loads(after.value_json) == json.loads(encode_value(90))
    assert after.row_version != before_race.row_version
    assert await audit_rows(admin) == []


async def test_t122_a_success_writes_the_row_and_one_audit_row_in_one_transaction(
    cli: Cli, admin: Engine
) -> None:
    outcome = await cli.set(KEY, "60")

    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    row = await stored(admin)
    assert json.loads(row.value_json) == json.loads(encode_value(60))
    assert row.change_reason == REASON
    assert row.updated_by == OPERATOR_OID
    (audit,) = await audit_rows(admin)
    assert (audit.action, audit.actor_type, audit.target_type, audit.target_id) == (
        "ops_config.set",
        "operator",
        "ops_config",
        KEY,
    )
    assert audit.actor_object_id == OPERATOR_OID
    assert audit.correlation_id == outcome.correlation_id
    assert json.loads(audit.metadata_json) == {
        "area": "api",
        "previous": SEEDED,
        "new": 60,
        "ticket": TICKET,
    }
    assert REASON not in audit.metadata_json


async def test_t122_a_forced_audit_failure_rolls_the_change_back(
    cli: Cli, admin: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused_by_the_server(connection: Connection, event: AuditEvent) -> None:
        # An action outside the catalogue fails `CK_audit_events_action` at the server, after
        # the update of the same transaction ran.
        write_audit_event(connection, event.model_copy(update={"action": "ops_config.refused"}))

    monkeypatch.setattr("docchat_ops.commands.config.write_audit_event", refused_by_the_server)
    before = await stored(admin)

    outcome = await cli.set(KEY, "60")

    assert outcome.code not in (ExitCode.SUCCESS, ExitCode.USAGE, ExitCode.CONFLICT)
    await nothing_written(admin, before)


async def test_t543_set_and_reset_print_the_correlation_id_of_their_audit_rows(
    cli: Cli, admin: Engine
) -> None:
    changed = await cli.set(KEY, "60")
    reset = await cli.run("reset", "--key", KEY, "--ticket", TICKET, "--reason", REASON, "--yes")

    assert (changed.code, reset.code) == (ExitCode.SUCCESS, ExitCode.SUCCESS), reset.stderr
    first, second = await audit_rows(admin)
    assert (first.action, first.correlation_id) == ("ops_config.set", changed.correlation_id)
    assert (second.action, second.correlation_id) == ("ops_config.reset", reset.correlation_id)
    assert changed.correlation_id != reset.correlation_id
