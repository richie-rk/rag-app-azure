"""`docchat-ops chat capacity-check` against the SQL Server container: the `chat` half of the
operator CLI integration tests (T-546; `86-operations.md` sections 2.5 item 3 and 2.8.7;
`12-chat-pipeline.md` section 3.7).

The command reads the rows the migration seeds as the `docchat_config_operator` role's login, the
rights a person runs it with, evaluates a proposal in memory and inserts no audit row. The CLI
runs through `run_cli` on the test's loop with the real parser and handler.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from azure.core.credentials import AccessToken
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, func, select

from docchat_core.models import SystemClock
from docchat_core.opsconfig import decode_value
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Retry, SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import audit_events, ops_config
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.framework.auth import AzResult
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

OPERATOR_OID = UUID("5d0c7a3e-2b19-4f6d-8e4a-9c1b2d3e4f50")
CAPACITIES = {"dev": ("30", "30"), "prod": ("500", "530"), "load": ("", "")}


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


class _NoAz:
    async def run(self, args: Sequence[str]) -> AzResult:
        raise AssertionError(f"capacity-check runs no az command: {args}")


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
    raise AssertionError("capacity-check reads no vault")


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.stdout)
        return document


@dataclass(frozen=True)
class Cli:
    checkout: Path
    database: _OperatorDatabase

    async def run(self, environment: str, *argv: str) -> Outcome:
        stdout, stderr = io.StringIO(), io.StringIO()
        toolkit = Toolkit(
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=stderr,
            environ={},
            clock=SystemClock(),
            sleep=asyncio.sleep,
            working_directory=self.checkout,
            az=_NoAz(),
            credential=_NoCredential,
            database=lambda target, credential: self.database,
            vault=_no_vault,
        )
        args = ["--env", environment, "--json", "chat", "capacity-check", *argv]
        code = await run_cli(args, toolkit)
        return Outcome(code, stdout.getvalue(), stderr.getvalue())


def _repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "azure.yaml").is_file():
            return parent
    raise FileNotFoundError("azure.yaml")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout holding the repository's own environment files and an azd file per
    environment with the capacities each environment's deployments have."""
    (tmp_path / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
    for environment, (chat, small) in CAPACITIES.items():
        source = _repository_root() / "infra" / "env" / f"{environment}.json"
        destination = tmp_path / "infra" / "env" / f"{environment}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        directory = tmp_path / ".azure" / environment
        directory.mkdir(parents=True)
        (directory / ".env").write_text(
            f'DOCCHAT_SQL_SERVER="sql-docchat-{environment}-test01.database.windows.net"\n'
            'DOCCHAT_SQL_DATABASE="docchat"\n'
            f'DOCCHAT_OPENAI_CHAT_CAPACITY="{chat}"\n'
            f'DOCCHAT_OPENAI_SMALL_CAPACITY="{small}"\n',
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
def cli(checkout: Path, operator: SqlEngine) -> Cli:
    return Cli(checkout, _OperatorDatabase(operator))


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def _audit_count(admin: Engine) -> int:
    with admin.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(audit_events)).scalar_one())


def _stored(admin: Engine, key: str) -> object:
    with admin.connect() as connection:
        text = connection.execute(
            select(ops_config.c.value_json).where(ops_config.c.config_key == key)
        ).scalar_one()
    return decode_value(str(text))


async def test_t546_capacity_check_reads_the_seeded_rows_and_writes_nothing(
    cli: Cli, admin: Engine
) -> None:
    outcome = await cli.run("prod")

    assert outcome.code == 0, outcome.stderr
    report = outcome.json()
    assert (report["chatShare"], report["smallShare"]) == (0.24, 0.2479)
    assert report["smallChargeTokens"] == 43_790
    assert _audit_count(admin) == 0


async def test_t546_a_proposal_is_evaluated_without_a_write(cli: Cli, admin: Engine) -> None:
    outcome = await cli.run("prod", "--set", "ratelimit.chat_requests_per_minute=4")

    assert outcome.code == 2
    assert outcome.json()["exceeded"] == ["chat", "small"]
    assert _stored(admin, "ratelimit.chat_requests_per_minute") == 3
    assert _audit_count(admin) == 0


@pytest.mark.parametrize("environment", ["dev", "load"])
async def test_dev_and_load_only_report(cli: Cli, environment: str) -> None:
    outcome = await cli.run(environment)

    assert outcome.code == 0, outcome.stderr
    report = outcome.json()
    assert report["enforced"] is False
    if environment == "load":
        assert report["absent"] == ["DOCCHAT_OPENAI_CHAT_CAPACITY", "DOCCHAT_OPENAI_SMALL_CAPACITY"]
    else:
        assert report["result"] == "exceeded"
