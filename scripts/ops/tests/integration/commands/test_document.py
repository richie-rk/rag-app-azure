"""T-617, the source fetch command test (`86-operations.md` section 2.8.10; `14-ingestion.md`
section 19.1 and acceptance criterion 59; `82-security.md` SEC-54).

Against Azurite and the SQL Server container: `docchat-ops document fetch-source` exits 3 without
level `user_data`; with it, it prints the untrusted-file warning before the download, writes the
version's source to `--out` with mode 0600 and no execute bit, refuses an existing `--out` with
exit 2, exits 4 for an unknown version or a missing blob, exits 5 and leaves no file when the
bytes differ from `document_versions.sha256`, exits 7 when Blob Storage is unreachable, and
writes one `document.source_fetched` row holding the ticket.

Level `user_data` is membership of `sg-docchat-<env>-user-data-operators`, which the database
maps to the role `docchat_user_data_operator`; the membership check is faked at the SQL login:
an operator with the level signs in as that role's login, and one without it is refused by SQL
Server (error 18456). The blob store is Azurite's `documents` container through the command's
own `StorageSourceBlobs`; the unreachable store is the same client aimed at a closed port.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import stat
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from azure.core.credentials import AccessToken
from azure.storage.blob.aio import BlobServiceClient, ExponentialRetry
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, Table, insert, select

from docchat_core.clients.emulators import AZURITE_CONNECTION_STRING
from docchat_core.models import SystemClock
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Retry, SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import audit_events, projects
from docchat_core.sql.tables.ingestion import document_versions, documents, search_indexes
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.commands import document
from docchat_ops.framework.auth import AzResult
from docchat_ops.framework.exitcodes import ExitCode
from docchat_testing.builders import project_row
from docchat_testing.builders.ingestion import document_row, document_version_row, search_index_row
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients

pytestmark = pytest.mark.integration

OPERATOR_OID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
TICKET = "LEGAL-0617"
REASON = "audit request for the uploaded contract"
CLOSED_PORT_BLOB_ENDPOINT = "http://127.0.0.1:9/devstoreaccount1"


def _unsigned_token(oid: UUID) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"oid": str(oid)}).encode()).decode()
    return f"e30.{claims.rstrip('=')}.unsigned"


class _OperatorDatabase:
    """The test database as `docchat-ops` reaches it, as the login of one role."""

    def __init__(self, engine: SqlEngine) -> None:
        self._engine = engine

    async def ensure_token(self) -> str | None:
        return _unsigned_token(OPERATOR_OID)

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        return await self._engine.run(work, name=name, retry=retry)

    async def dispose(self) -> None:
        """The fixtures dispose of the layer after the test."""


class _PersonAz:
    async def run(self, args: Sequence[str]) -> AzResult:
        assert list(args[:2]) == ["account", "show"]
        return AzResult(returncode=0, stdout="user\n", stderr="")


class _Credential:
    """The operator's credential: a storage token the emulator ignores."""

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("operator-token", 4_102_444_800)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _Credential:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _no_vault(uri: str, credential: object) -> Any:
    raise AssertionError("fetch-source reads no vault")


@dataclass
class _WarnedBlobs:
    """The command's own blob reader, recording what standard error held at the download."""

    inner: document.SourceBlobs
    stderr: io.StringIO
    seen_at_download: list[str]

    async def download(self, path: str) -> AsyncIterator[bytes]:
        self.seen_at_download.append(self.stderr.getvalue())
        async for chunk in self.inner.download(path):
            yield chunk

    async def close(self) -> None:
        await self.inner.close()


def azurite_blobs() -> document.SourceBlobs:
    return document.StorageSourceBlobs(
        BlobServiceClient.from_connection_string(AZURITE_CONNECTION_STRING)
    )


def unreachable_blobs() -> document.SourceBlobs:
    # `UseDevelopmentStorage=true` names no key (81-config.md section 6): the SDK expands it to
    # Azurite's published account, whose shared-key credential signs requests to a closed port.
    azurite = BlobServiceClient.from_connection_string(AZURITE_CONNECTION_STRING)
    return document.StorageSourceBlobs(
        BlobServiceClient(
            CLOSED_PORT_BLOB_ENDPOINT,
            credential=azurite.credential,
            connection_timeout=2,
            read_timeout=2,
            retry_policy=ExponentialRetry(retry_total=0),
        )
    )


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str
    seen_at_download: list[str]

    @property
    def correlation_id(self) -> str:
        return self.stderr.splitlines()[0].removeprefix("correlationId: ")


@dataclass
class Cli:
    checkout: Path
    database: _OperatorDatabase
    blobs: Callable[[], document.SourceBlobs] = azurite_blobs

    async def fetch(self, version: UUID, out: Path) -> Outcome:
        stdout, stderr = io.StringIO(), io.StringIO()
        seen: list[str] = []
        toolkit = Toolkit(
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=stderr,
            environ={},
            clock=SystemClock(),
            sleep=asyncio.sleep,
            working_directory=self.checkout,
            az=_PersonAz(),
            credential=_Credential,
            database=lambda target, credential: self.database,
            vault=_no_vault,
            blobs=lambda endpoint, credential: _WarnedBlobs(self.blobs(), stderr, seen),
        )
        argv = ["--env", "dev", "--json", "document", "fetch-source"]
        argv += ["--version", str(version), "--out", str(out), "--ticket", TICKET]
        argv += ["--reason", REASON]
        code = await run_cli(argv, toolkit)
        return Outcome(code, stdout.getvalue(), stderr.getvalue(), seen)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
    (tmp_path / ".azure" / "dev").mkdir(parents=True)
    (tmp_path / ".azure" / "dev" / ".env").write_text(
        'DOCCHAT_SQL_SERVER="sql-docchat-dev-test01.database.windows.net"\n'
        'DOCCHAT_SQL_DATABASE="docchat"\n'
        'DOCCHAT_STORAGE_BLOB_ENDPOINT="https://stdocchatdevabc123.blob.core.windows.net/"\n',
        encoding="utf-8",
    )
    return tmp_path


def _engine(sql_database: SqlTestDatabase, odbc: str) -> SqlEngine:
    return SqlEngine(
        "ops",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=odbc,
    )


@pytest.fixture
async def user_data_operator(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = _engine(sql_database, sql_database.connection_string("docchat_user_data_operator"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def without_the_level(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    """The same login with another password: SQL Server refuses the sign-in (18456), as it
    refuses an operator outside the user-data operators group."""
    login = sql_database.logins["docchat_user_data_operator"]
    odbc = sql_database.connection_string("docchat_user_data_operator").replace(
        login.password, login.password + "-not-a-member"
    )
    engine = _engine(sql_database, odbc)
    yield engine
    await engine.dispose()


@pytest.fixture
def cli(checkout: Path, user_data_operator: SqlEngine) -> Cli:
    return Cli(checkout, _OperatorDatabase(user_data_operator))


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def out(tmp_path: Path) -> Path:
    directory = tmp_path / "fetched"
    directory.mkdir(mode=0o700)
    return directory / "source.bin"


@dataclass(frozen=True)
class Version:
    id: UUID
    project_id: UUID
    path: str
    source: bytes


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


async def stored_version(
    admin: Engine, storage: StorageClients, *, blob: bytes | None = None, uploaded: bool = True
) -> Version:
    """An ingested PDF version whose source is in the `documents` container (`blob`, or the
    version's own bytes; none when not `uploaded`)."""
    source = b"%PDF-1.7 T-617 source " + uuid4().hex.encode() + b"\n%%EOF\n"
    project = project_row()
    index = search_index_row(status="ready")
    row = document_row(index["id"])
    version = document_version_row(
        row["id"],
        index["id"],
        project["id"],
        state="ingested",
        sha256=hashlib.sha256(source).digest(),
        size_bytes=len(source),
        committed_at=sql_now(),
    )
    await asyncio.to_thread(
        put,
        admin,
        (projects, project),
        (search_indexes, index),
        (documents, row),
        (document_versions, version),
    )
    if uploaded:
        await storage.container("documents").upload_blob(
            version["blob_path"], blob if blob is not None else source, overwrite=True
        )
    return Version(version["id"], project["id"], version["blob_path"], source)


def _audit_rows(admin: Engine) -> list[Any]:
    with admin.connect() as connection:
        return list(connection.execute(select(audit_events).order_by(audit_events.c.id)))


async def audit_rows(admin: Engine) -> list[Any]:
    """The audit rows, read in a worker thread, off the test's guarded loop."""
    return await asyncio.to_thread(_audit_rows, admin)


async def exists(path: Path) -> bool:
    return await asyncio.to_thread(path.exists)


async def read(path: Path) -> bytes:
    return await asyncio.to_thread(path.read_bytes)


# T-617


async def test_t617_without_level_user_data_it_exits_3_and_writes_nothing(
    checkout: Path, without_the_level: SqlEngine, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage)
    cli = Cli(checkout, _OperatorDatabase(without_the_level))

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.ACCESS_DENIED
    assert outcome.seen_at_download == []
    assert not await exists(out)
    assert await audit_rows(admin) == []


async def test_t617_with_the_level_it_warns_writes_the_source_0600_and_one_audit_row(
    cli: Cli, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage)

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert await read(out) == version.source
    mode = stat.S_IMODE((await asyncio.to_thread(os.lstat, out)).st_mode)
    assert mode == 0o600
    assert mode & 0o111 == 0
    (before_download,) = outcome.seen_at_download
    assert document.UNTRUSTED_WARNING in before_download
    (audit,) = await audit_rows(admin)
    assert (audit.action, audit.actor_type, audit.target_type) == (
        "document.source_fetched",
        "operator",
        "document_version",
    )
    assert audit.target_id == str(version.id)
    assert audit.actor_object_id == OPERATOR_OID
    assert audit.project_id == version.project_id
    assert audit.correlation_id == outcome.correlation_id
    assert json.loads(audit.metadata_json)["ticket"] == TICKET
    assert REASON not in audit.metadata_json


async def test_t543_fetch_source_prints_the_correlation_id_of_its_audit_row(
    cli: Cli, admin: Engine, storage: StorageClients, tmp_path: Path
) -> None:
    first = await stored_version(admin, storage)
    second = await stored_version(admin, storage)

    outcomes = [
        await cli.fetch(first.id, tmp_path / "first.pdf"),
        await cli.fetch(second.id, tmp_path / "second.pdf"),
    ]

    assert [outcome.code for outcome in outcomes] == [ExitCode.SUCCESS, ExitCode.SUCCESS]
    rows = await audit_rows(admin)
    assert [(row.target_id, row.correlation_id) for row in rows] == [
        (str(first.id), outcomes[0].correlation_id),
        (str(second.id), outcomes[1].correlation_id),
    ]
    assert outcomes[0].correlation_id != outcomes[1].correlation_id


async def test_t617_an_existing_out_exits_2(
    cli: Cli, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage)
    await asyncio.to_thread(out.write_bytes, b"kept")

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.USAGE
    assert await read(out) == b"kept"
    assert await audit_rows(admin) == []


async def test_t617_an_unknown_version_exits_4(cli: Cli, admin: Engine, out: Path) -> None:
    outcome = await cli.fetch(uuid4(), out)

    assert outcome.code == ExitCode.NOT_FOUND
    assert not await exists(out)
    assert await audit_rows(admin) == []


async def test_t617_a_missing_blob_exits_4_and_leaves_no_file(
    cli: Cli, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage, uploaded=False)

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.NOT_FOUND
    assert not await exists(out)
    assert await audit_rows(admin) == []


async def test_t617_bytes_that_differ_from_the_hash_exit_5_and_leave_no_file(
    cli: Cli, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage, blob=b"%PDF-1.7 other bytes\n%%EOF\n")

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.INCOMPLETE
    assert not await exists(out)
    assert await audit_rows(admin) == []


async def test_t617_unreachable_blob_storage_exits_7_and_leaves_no_file(
    cli: Cli, admin: Engine, storage: StorageClients, out: Path
) -> None:
    version = await stored_version(admin, storage)
    cli.blobs = unreachable_blobs

    outcome = await cli.fetch(version.id, out)

    assert outcome.code == ExitCode.UNREACHABLE
    assert not await exists(out)
    assert await audit_rows(admin) == []
