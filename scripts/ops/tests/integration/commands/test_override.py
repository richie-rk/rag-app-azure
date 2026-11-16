"""`docchat-ops override` against the SQL Server container: the `override` half of the operator
CLI integration tests (T-546) and of the operator correlation integration test (T-543)
(`86-operations.md` sections 2.5 and 2.8.3, runbook R04; `80-data-model.md` sections 2.6, 2.21
and 2.22).

The CLI runs through `run_cli` on the test's loop with the real parser and handlers. Its SQL
layer is the data-access layer as the `docchat_config_operator` role's login, so every statement
runs with exactly that role's rights; rows are arranged and read back as `sa`. The container signs
in with SQL authentication, so an unsigned token carrying only `oid` stands in for the operator's
Entra token, from which the CLI reads the audit rows' actor.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from azure.core.credentials import AccessToken
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, Integer, Table, cast, insert, literal, select

from docchat_core.audit.models import (
    UserIndexOverrideClearedMetadata,
    UserIndexOverrideSetMetadata,
)
from docchat_core.models import SystemClock
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Retry, Service, SqlEngine
from docchat_core.sql.repositories.access import read_index_choice
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables import DatabaseRole
from docchat_core.sql.tables.access import (
    audit_events,
    project_indexes,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import search_indexes
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.commands import override
from docchat_ops.framework.auth import AzResult
from docchat_testing.builders import (
    project_index_row,
    project_row,
    search_index_row,
    user_index_override_row,
    user_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

OPERATOR_OID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
REASON = "T-546 override integration test"
TICKET = ("--ticket", "T-546", "--reason", REASON)


# --- The CLI against the test database ------------------------------------------------------------


def _unsigned_token(oid: UUID) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"oid": str(oid)}).encode()).decode()
    return f"e30.{claims.rstrip('=')}.unsigned"


class _OperatorDatabase:
    """The test database as `docchat-ops` reaches it, as the config operator role."""

    def __init__(self, engine: SqlEngine) -> None:
        self._engine = engine

    async def ensure_token(self) -> str | None:
        return _unsigned_token(OPERATOR_OID)

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        return await self._engine.run(work, name=name, retry=retry)

    async def dispose(self) -> None:
        """The `operator` fixture disposes the layer after the test."""


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
    raise AssertionError("override reads no vault")


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def correlation_id(self) -> str:
        return self.stderr.splitlines()[0].removeprefix("correlationId: ")

    def json(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.stdout)
        return document


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
        code = await run_cli(["--env", "dev", "--json", "override", *argv], toolkit)
        return Outcome(code, stdout.getvalue(), stderr.getvalue())


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


def _engine(sql_database: SqlTestDatabase, service: Service, role: DatabaseRole) -> SqlEngine:
    return SqlEngine(
        service,
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string(role),
    )


@pytest.fixture
async def operator(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = _engine(sql_database, "ops", "docchat_config_operator")
    yield engine
    await engine.dispose()


@pytest.fixture
async def api(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = _engine(sql_database, "api", "docchat_api")
    yield engine
    await engine.dispose()


@pytest.fixture
def cli(checkout: Path, operator: SqlEngine) -> Cli:
    return Cli(checkout, _OperatorDatabase(operator))


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


# --- Rows -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    user: UUID
    project: UUID
    default_index: UUID
    other_index: UUID
    unmapped_index: UUID


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def world(admin: Engine) -> World:
    """A signed-in user and a project mapping two indexes, the first its default, beside an
    index the project doesn't map."""
    user = user_row()
    project = project_row()
    default, other, unmapped = search_index_row(), search_index_row(), search_index_row()
    put(
        admin,
        (users, user),
        (projects, project),
        (search_indexes, default),
        (search_indexes, other),
        (search_indexes, unmapped),
        (project_indexes, project_index_row(project["id"], default["id"], is_default=True)),
        (project_indexes, project_index_row(project["id"], other["id"])),
    )
    return World(user["object_id"], project["id"], default["id"], other["id"], unmapped["id"])


def target(place: World) -> tuple[str, ...]:
    return ("--user", str(place.user), "--project", str(place.project))


def override_rows(admin: Engine) -> list[Any]:
    with admin.connect() as connection:
        return list(connection.execute(select(user_index_overrides)))


def audit_rows(admin: Engine) -> list[Any]:
    with admin.connect() as connection:
        return list(connection.execute(select(audit_events).order_by(audit_events.c.id)))


# --- set ------------------------------------------------------------------------------------------


async def test_t546_set_writes_the_row_and_its_audit_row_in_one_transaction(
    cli: Cli, admin: Engine, api: SqlEngine
) -> None:
    place = world(admin)
    outcome = await cli.run(
        "set", *target(place), "--index", str(place.other_index), *TICKET, "--yes"
    )
    assert outcome.code == 0, outcome.stderr

    (row,) = override_rows(admin)
    assert (row.user_object_id, row.project_id, row.search_index_id) == (
        place.user,
        place.project,
        place.other_index,
    )
    assert row.reason == REASON
    assert row.set_by == OPERATOR_OID
    assert row.expires_at - row.set_at == timedelta(days=override.DAYS_DEFAULT)
    (audit,) = audit_rows(admin)
    assert (audit.action, audit.actor_type, audit.target_type) == (
        "user_index_override.set",
        "operator",
        "user_index_override",
    )
    assert (audit.actor_object_id, audit.subject_object_id) == (OPERATOR_OID, place.user)
    assert (audit.target_id, audit.project_id) == (str(place.user), place.project)
    # T-543: the printed correlation ID is the row's.
    assert audit.correlation_id == outcome.correlation_id
    metadata = UserIndexOverrideSetMetadata.model_validate_json(audit.metadata_json)
    assert metadata.search_index_id == place.other_index
    assert metadata.replaced is False
    assert metadata.ticket == "T-546"
    assert metadata.expires_at.replace(tzinfo=None) == row.expires_at
    assert REASON not in audit.metadata_json
    assert (
        outcome.json()["expiresAt"] == metadata.model_dump(mode="json", by_alias=True)["expiresAt"]
    )

    # The API's index resolution takes the override on the user's next request.
    choice = await read_index_choice(api, oid=place.user, project_id=place.project)
    assert choice.override_index_id == place.other_index


async def test_t546_set_replaces_the_users_override_in_the_project(cli: Cli, admin: Engine) -> None:
    place = world(admin)
    first = await cli.run(
        "set", *target(place), "--index", str(place.other_index), *TICKET, "--yes"
    )
    assert first.code == 0, first.stderr
    second = await cli.run(
        "set", *target(place), "--index", str(place.default_index), "--days", "30", *TICKET, "--yes"
    )
    assert second.code == 0, second.stderr

    (row,) = override_rows(admin)
    assert row.search_index_id == place.default_index
    assert row.expires_at - row.set_at == timedelta(days=30)
    replaced = [
        UserIndexOverrideSetMetadata.model_validate_json(audit.metadata_json).replaced
        for audit in audit_rows(admin)
    ]
    assert replaced == [False, True]


async def test_t546_set_refusals_write_nothing(cli: Cli, admin: Engine) -> None:
    place = world(admin)
    unmapped = await cli.run(
        "set", *target(place), "--index", str(place.unmapped_index), *TICKET, "--yes"
    )
    assert unmapped.code == 2
    unknown_user = await cli.run(
        "set",
        "--user",
        str(uuid4()),
        "--project",
        str(place.project),
        "--index",
        str(place.other_index),
        *TICKET,
        "--yes",
    )
    assert unknown_user.code == 4
    too_long = await cli.run(
        "set", *target(place), "--index", str(place.other_index), "--days", "31", *TICKET, "--yes"
    )
    assert too_long.code == 2
    assert override_rows(admin) == []
    assert audit_rows(admin) == []


async def test_t546_a_failed_audit_insert_rolls_the_override_back(
    cli: Cli, admin: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = world(admin)

    def failing_audit(connection: Connection, event: object) -> None:
        # A statement that fails on the server, in the transaction that holds the change.
        connection.execute(select(cast(literal("not a number"), Integer)))

    monkeypatch.setattr(override, "write_audit_event", failing_audit)
    outcome = await cli.run(
        "set", *target(place), "--index", str(place.other_index), *TICKET, "--yes"
    )
    assert outcome.code == 1
    assert override_rows(admin) == []
    assert audit_rows(admin) == []


# --- list -----------------------------------------------------------------------------------------


async def test_t546_list_shows_the_expired_flag_never_the_reason_and_writes_no_row(
    cli: Cli, admin: Engine
) -> None:
    place = world(admin)
    other_user = user_row()
    set_at = sql_now() - timedelta(days=10)
    expired = user_index_override_row(
        other_user["object_id"],
        place.project,
        place.other_index,
        set_at=set_at,
        expires_at=set_at + timedelta(days=3),
    )
    current = user_index_override_row(place.user, place.project, place.other_index)
    put(
        admin,
        (users, other_user),
        (user_index_overrides, expired),
        (user_index_overrides, current),
    )

    every = await cli.run("list", *TICKET)
    assert every.code == 0, every.stderr
    flags = {item["userObjectId"]: item["expired"] for item in every.json()["overrides"]}
    assert flags == {str(place.user): False, str(other_user["object_id"]): True}
    assert current["reason"] not in every.stdout
    assert expired["reason"] not in every.stdout

    one = await cli.run("list", "--user", str(place.user), *TICKET)
    assert [item["userObjectId"] for item in one.json()["overrides"]] == [str(place.user)]
    assert audit_rows(admin) == []


# --- clear ----------------------------------------------------------------------------------------


async def test_t546_clear_deletes_the_row_with_its_audit_row(
    cli: Cli, admin: Engine, api: SqlEngine
) -> None:
    place = world(admin)
    put(
        admin,
        (
            user_index_overrides,
            user_index_override_row(place.user, place.project, place.other_index),
        ),
    )
    outcome = await cli.run("clear", *target(place), *TICKET, "--yes")
    assert outcome.code == 0, outcome.stderr
    assert override_rows(admin) == []
    (audit,) = audit_rows(admin)
    assert audit.action == "user_index_override.cleared"
    assert (audit.subject_object_id, audit.project_id) == (place.user, place.project)
    assert audit.correlation_id == outcome.correlation_id
    metadata = UserIndexOverrideClearedMetadata.model_validate_json(audit.metadata_json)
    assert (metadata.search_index_id, metadata.ticket) == (place.other_index, "T-546")
    choice = await read_index_choice(api, oid=place.user, project_id=place.project)
    assert (choice.override_index_id, choice.default_index_id) == (None, place.default_index)

    again = await cli.run("clear", *target(place), *TICKET, "--yes")
    assert again.code == 4
    assert len(audit_rows(admin)) == 1


async def test_t543_set_and_clear_print_the_correlation_id_of_their_audit_rows(
    cli: Cli, admin: Engine
) -> None:
    place = world(admin)

    set_ = await cli.run("set", *target(place), "--index", str(place.other_index), *TICKET, "--yes")
    cleared = await cli.run("clear", *target(place), *TICKET, "--yes")

    assert (set_.code, cleared.code) == (0, 0), cleared.stderr
    first, second = audit_rows(admin)
    assert (first.action, first.correlation_id) == (
        "user_index_override.set",
        set_.correlation_id,
    )
    assert (second.action, second.correlation_id) == (
        "user_index_override.cleared",
        cleared.correlation_id,
    )
    assert set_.correlation_id != cleared.correlation_id
