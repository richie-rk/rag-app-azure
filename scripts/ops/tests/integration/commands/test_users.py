"""The user-data command integration tests (T-085, T-329, T-330, T-543, T-546;
`86-operations.md` section 2.8.4; `13-conversations-and-memory.md` sections 10.7 to 10.11;
`80-data-model.md` section 2.23; `82-security.md` SEC-119 and SEC-120).

Against the SQL Server and Cosmos DB emulator of `compose.yaml`: `docchat-ops user export`
returns every SQL row and actor reference of a user present in every table of section 2.23 and
every item of their partitions, hidden sessions included, with manifest hashes that verify;
`user delete` removes every item and the overrides, memberships and `users` row while actor
columns and audit rows stay, changes nothing more on a second run, and leaves another user's data
untouched; `user verify` then exits 0; `user redact-document` removes a withdrawn document's text
from every trace that kept it and nothing else; `user read-conversation` runs at level `read`;
every changing command's audit row carries the printed correlation ID, and the read-only commands
write none.

Each level is its SQL role's login, as `scripts/ops/tests/integration/commands/test_document.py`
fakes the just-in-time membership; the Cosmos DB containers are the emulator's, reached through
the command's own `UserStores` shape. The export's snapshot read needs `ALLOW_SNAPSHOT_ISOLATION`
on the test database, which the module switches on.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from azure.core.credentials import AccessToken
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosClientTimeoutError
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, Table, func, insert, select

from docchat_core.models import SystemClock
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Retry, SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import (
    audit_events,
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import search_indexes
from docchat_ops.cli import Toolkit, run_cli
from docchat_ops.commands.users import CosmosContainer, UserStores
from docchat_ops.framework.auth import AzResult
from docchat_ops.framework.exitcodes import ExitCode
from docchat_testing.builders import (
    audit_event_row,
    memory_document,
    project_index_row,
    project_member_row,
    project_row,
    search_index_row,
    session_document,
    turn_document,
    turn_trace_document,
    usage_day_counter,
    usage_month_counter,
    user_index_override_row,
    user_row,
)
from docchat_testing.fixtures.cosmos import CosmosTestDatabase, emulator_client
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

OPERATOR_OID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
TICKET = "DSR-0329"
REASON = "data-subject request from the user"
MARKER = "0123456789abcdef"
CANARY = "D-CANARY-0330 withdrawn clause text"
SYSTEM_FIELDS = {"_rid", "_self", "_etag", "_attachments", "_ts"}
ALLOW_SNAPSHOT = "ALTER DATABASE CURRENT SET ALLOW_SNAPSHOT_ISOLATION ON"


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
    """The operator's credential; the emulators ignore its token."""

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("operator-token", 4_102_444_800)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _Credential:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _yesterday() -> str:
    """A `--since` inside every window the commands accept, whenever the suite runs."""
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def read_json(path: Path) -> Any:
    return json.loads(await asyncio.to_thread(path.read_bytes))


def _sdk_error[E: BaseException](cls: Callable[..., E], **kwargs: Any) -> E:
    # The SDK's exception constructors are untyped; the call through a typed callable isn't.
    return cls(**kwargs)


def _no_vault(uri: str, credential: object) -> Any:
    raise AssertionError("the user group reads no vault")


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def correlation_id(self) -> str:
        return self.stderr.splitlines()[0].removeprefix("correlationId: ")

    def json(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert isinstance(document, dict)
        return document


@dataclass(frozen=True)
class Containers:
    conversations: ContainerProxy
    memories: ContainerProxy
    usage: ContainerProxy

    def stores(self) -> UserStores:
        async def keep_open() -> None:
            """The fixture closes the client after the test."""

        return UserStores(
            conversations=cast("CosmosContainer", self.conversations),
            memories=cast("CosmosContainer", self.memories),
            usage=cast("CosmosContainer", self.usage),
            close=keep_open,
        )


@dataclass
class Cli:
    checkout: Path
    databases: dict[str, _OperatorDatabase]
    containers: Containers

    async def run(self, level: str, *argv: str) -> Outcome:
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
            credential=_Credential,
            database=lambda target, credential: self.databases[level],
            vault=_no_vault,
            cosmos=lambda endpoint, database, credential: self.containers.stores(),
        )
        arguments = ["--env", "dev", "--json", "--yes", *argv, "--ticket", TICKET]
        code = await run_cli([*arguments, "--reason", REASON], toolkit)
        return Outcome(code, stdout.getvalue(), stderr.getvalue())


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
    (tmp_path / ".azure" / "dev").mkdir(parents=True)
    (tmp_path / ".azure" / "dev" / ".env").write_text(
        'DOCCHAT_SQL_SERVER="sql-docchat-dev-test01.database.windows.net"\n'
        'DOCCHAT_SQL_DATABASE="docchat"\n'
        'DOCCHAT_COSMOS_ENDPOINT="https://cosmos-docchat-dev-test01.documents.azure.com:443/"\n'
        'DOCCHAT_COSMOS_DATABASE="docchat"\n',
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
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(ALLOW_SNAPSHOT)
    yield engine
    engine.dispose()


@pytest.fixture
async def levels(
    sql_database: SqlTestDatabase, admin: Engine
) -> AsyncIterator[dict[str, _OperatorDatabase]]:
    del admin
    engines = {
        "user_data": _engine(
            sql_database, sql_database.connection_string("docchat_user_data_operator")
        ),
        "read": _engine(
            sql_database, sql_database.connection_string("docchat_conversation_reader")
        ),
    }
    login = sql_database.logins["docchat_user_data_operator"]
    engines["none"] = _engine(
        sql_database,
        sql_database.connection_string("docchat_user_data_operator").replace(
            login.password, login.password + "-not-a-member"
        ),
    )
    yield {name: _OperatorDatabase(engine) for name, engine in engines.items()}
    for engine in engines.values():
        await engine.dispose()


@pytest.fixture
async def containers(cosmos_server: CosmosTestDatabase) -> AsyncIterator[Containers]:
    async with emulator_client(cosmos_server.endpoint) as client:
        database = client.get_database_client(cosmos_server.name)
        yield Containers(
            conversations=database.get_container_client("conversations"),
            memories=database.get_container_client("memories"),
            usage=database.get_container_client("usage"),
        )


@pytest.fixture
def cli(checkout: Path, levels: dict[str, _OperatorDatabase], containers: Containers) -> Cli:
    return Cli(checkout, levels, containers)


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def _count(engine: Engine, table: Table, condition: Any) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(table).where(condition)
            ).scalar_one()
        )


async def count(engine: Engine, table: Table, condition: Any) -> int:
    return await asyncio.to_thread(_count, engine, table, condition)


def _audit_rows(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(select(audit_events).order_by(audit_events.c.id)))


async def audit_rows(engine: Engine) -> list[Any]:
    return await asyncio.to_thread(_audit_rows, engine)


async def partition(container: ContainerProxy, user: UUID, prefix: bool = False) -> list[Any]:
    key: Any = [str(user)] if prefix else str(user)
    pager = container.query_items(
        "SELECT * FROM c WHERE c.userId = @userId",
        parameters=[{"name": "@userId", "value": str(user)}],
        partition_key=key,
    )
    return [item async for item in pager]


@dataclass(frozen=True)
class SqlWorld:
    user: UUID
    project: UUID
    hidden_project: UUID
    index: UUID


async def seed_sql(admin: Engine) -> SqlWorld:
    """A user present in every table of `80-data-model.md` section 2.23: their row, a membership,
    an override, actor columns on a project and an index, and audit rows as actor and subject."""
    person = user_row()
    user = person["object_id"]
    project = project_row(created_by=user, updated_by=user)
    hidden = project_row()
    index = search_index_row(created_by=user, updated_by=user)
    rows: list[tuple[Table, dict[str, Any]]] = [
        (users, person),
        (projects, project),
        (projects, hidden),
        (search_indexes, index),
        (project_members, project_member_row(user, project["id"], granted_by=uuid4())),
        (project_indexes, project_index_row(project["id"], index["id"])),
        (user_index_overrides, user_index_override_row(user, project["id"], index["id"])),
        (audit_events, audit_event_row(actor_object_id=user)),
        (audit_events, audit_event_row(subject_object_id=user)),
    ]
    await asyncio.to_thread(put, admin, *rows)
    return SqlWorld(user, project["id"], hidden["id"], index["id"])


async def seed_cosmos(containers: Containers, user: UUID, project: UUID, hidden: UUID) -> int:
    """Two sessions of the user, one in a project they no longer reach, with turns and traces,
    their memory and usage counters; returns the number of `conversations` items."""
    items = 0
    for session_project in (project, hidden):
        session_id = uuid4()
        await containers.conversations.create_item(
            session_document(
                user_id=user, session_id=session_id, project_id=session_project, turn_count=2
            ).to_item()
        )
        items += 1
        for seq in (1, 2):
            turn = turn_document(
                user_id=user, session_id=session_id, seq=seq, project_id=session_project
            )
            await containers.conversations.create_item(turn.to_item())
            await containers.conversations.create_item(
                turn_trace_document(user_id=user, session_id=session_id, turn=turn.id).to_item()
            )
            items += 2
    await containers.memories.create_item(memory_document(user_id=user).to_item())
    await containers.usage.create_item(usage_day_counter(user_id=user).to_item())
    await containers.usage.create_item(usage_month_counter(user_id=user).to_item())
    return items


async def test_t085_the_sql_rows_are_exported_and_deleted_while_actor_columns_stay(
    cli: Cli, admin: Engine, tmp_path: Path
) -> None:
    world = await seed_sql(admin)
    out = tmp_path / "export"

    exported = await cli.run(
        "user_data", "user", "export", "--user", str(world.user), "--out", str(out)
    )
    assert exported.code == ExitCode.SUCCESS, exported.stderr
    counts = exported.json()["counts"]
    assert {key: counts[key] for key in ("users", "projectMembers", "userIndexOverrides")} == {
        "users": 1,
        "projectMembers": 1,
        "userIndexOverrides": 1,
    }
    assert (counts["auditEvents"], counts["actorReferences"]) == (2, 4)
    # The profile holds the person's details (13-conversations-and-memory.md section 10.8 item 3);
    # the manifest names the user.
    profile = await read_json(out / "subject/profile.json")
    assert set(profile) == {"displayName", "email", "firstSeenAt", "lastSeenAt", "lastKnownRole"}
    assert (await read_json(out / "manifest.json"))["user"] == str(world.user)
    memberships = await read_json(out / "subject/memberships.json")
    assert str(world.project) in json.dumps(memberships)
    overrides = await read_json(out / "internal/user_index_overrides.json")
    assert [str(override["search_index_id"]).lower() for override in overrides] == [
        str(world.index)
    ]
    assert all("reason" not in override for override in overrides)

    deleted = await cli.run(
        "user_data",
        "user",
        "delete",
        "--user",
        str(world.user),
        "--confirm",
        str(world.user),
        "--assignment-removed",
    )

    assert deleted.code == ExitCode.SUCCESS, deleted.stderr
    assert await count(admin, users, users.c.object_id == world.user) == 0
    assert await count(admin, project_members, project_members.c.user_object_id == world.user) == 0
    assert (
        await count(
            admin, user_index_overrides, user_index_overrides.c.user_object_id == world.user
        )
        == 0
    )
    assert await count(admin, projects, projects.c.created_by == world.user) == 1
    assert await count(admin, projects, projects.c.updated_by == world.user) == 1
    assert await count(admin, search_indexes, search_indexes.c.updated_by == world.user) == 1
    assert await count(admin, audit_events, audit_events.c.subject_object_id == world.user) >= 1
    assert await count(admin, audit_events, audit_events.c.actor_object_id == world.user) == 1


async def test_t543_export_and_delete_print_the_correlation_id_of_their_audit_rows(
    cli: Cli, admin: Engine, tmp_path: Path
) -> None:
    world = await seed_sql(admin)

    exported = await cli.run(
        "user_data", "user", "export", "--user", str(world.user), "--out", str(tmp_path / "out")
    )
    deleted = await cli.run(
        "user_data",
        "user",
        "delete",
        "--user",
        str(world.user),
        "--confirm",
        str(world.user),
        "--assignment-removed",
    )

    assert (exported.code, deleted.code) == (ExitCode.SUCCESS, ExitCode.SUCCESS), deleted.stderr
    written = {
        row.action: row.correlation_id
        for row in await audit_rows(admin)
        if row.actor_type == "operator"
    }
    assert written == {
        "user_data.exported": exported.correlation_id,
        "user_data.deleted": deleted.correlation_id,
    }


async def test_t329_export_delete_and_verify_one_user(
    cli: Cli, admin: Engine, containers: Containers, tmp_path: Path
) -> None:
    world = await seed_sql(admin)
    items = await seed_cosmos(containers, world.user, world.project, world.hidden_project)
    other = await seed_sql(admin)
    other_items = await seed_cosmos(containers, other.user, other.project, other.hidden_project)
    out = tmp_path / "export"

    exported = await cli.run(
        "user_data", "user", "export", "--user", str(world.user), "--out", str(out)
    )
    assert exported.code == ExitCode.SUCCESS, exported.stderr
    counts = exported.json()["counts"]
    assert counts == {
        "conversations": items,
        "memories": 1,
        "usage": 2,
        "users": 1,
        "projectMembers": 1,
        "userIndexOverrides": 1,
        "auditEvents": 2,
        "actorReferences": 4,
    }
    manifest_bytes = await asyncio.to_thread((out / "manifest.json").read_bytes)
    manifest = json.loads(manifest_bytes)
    for relative, digest in manifest["files"].items():
        data = await asyncio.to_thread((out / relative).read_bytes)
        assert hashlib.sha256(data).hexdigest() == digest
    sessions = await read_json(out / "subject/sessions.json")
    assert {session["projectId"] for session in sessions} == {
        str(world.project),
        str(world.hidden_project),
    }
    references = await read_json(out / "internal/actor_references.json")
    assert {(reference["table"], reference["column"]) for reference in references} == {
        ("projects", "created_by"),
        ("projects", "updated_by"),
        ("search_indexes", "created_by"),
        ("search_indexes", "updated_by"),
    }

    deleted = await cli.run(
        "user_data",
        "user",
        "delete",
        "--user",
        str(world.user),
        "--confirm",
        str(world.user),
        "--assignment-removed",
    )
    assert deleted.code == ExitCode.SUCCESS, deleted.stderr
    assert deleted.json()["counts"] == {
        "conversations": items,
        "memories": 1,
        "usage": 2,
        "users": 1,
        "projectMembers": 1,
        "userIndexOverrides": 1,
    }
    for container, prefix in (
        (containers.conversations, True),
        (containers.memories, False),
        (containers.usage, False),
    ):
        assert await partition(container, world.user, prefix) == []
    assert await count(admin, users, users.c.object_id == world.user) == 0
    assert await count(admin, project_members, project_members.c.user_object_id == world.user) == 0
    # Actor columns and audit rows keep the object ID.
    assert await count(admin, projects, projects.c.created_by == world.user) == 1
    assert await count(admin, search_indexes, search_indexes.c.created_by == world.user) == 1
    assert await count(admin, audit_events, audit_events.c.actor_object_id == world.user) == 1
    # Another user's data is untouched.
    assert len(await partition(containers.conversations, other.user, prefix=True)) == other_items
    assert await count(admin, users, users.c.object_id == other.user) == 1

    again = await cli.run(
        "user_data",
        "user",
        "delete",
        "--user",
        str(world.user),
        "--confirm",
        str(world.user),
        "--assignment-removed",
    )
    assert again.code == ExitCode.SUCCESS, again.stderr
    assert set(again.json()["counts"].values()) == {0}

    verified = await cli.run("user_data", "user", "verify", "--user", str(world.user))
    assert verified.code == ExitCode.SUCCESS, verified.stderr

    rows = await audit_rows(admin)
    by_correlation = {row.correlation_id: row for row in rows if row.actor_type == "operator"}
    assert by_correlation[exported.correlation_id].action == "user_data.exported"
    exported_metadata = json.loads(by_correlation[exported.correlation_id].metadata_json)
    assert exported_metadata["manifestSha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert by_correlation[deleted.correlation_id].action == "user_data.deleted"
    assert json.loads(by_correlation[deleted.correlation_id].metadata_json)["ticket"] == TICKET
    assert verified.correlation_id not in by_correlation


def _source_block(source_id: str, text: str) -> str:
    return f"[[BEGIN SOURCE {source_id} {MARKER}]]\n{text}\n[[END SOURCE {source_id} {MARKER}]]"


def _kept(rank: int, source_id: str, document: UUID) -> dict[str, Any]:
    return {
        "rank": rank,
        "sourceId": source_id,
        "chunkKey": f"{document}_{uuid4()}_{rank}",
        "documentId": str(document),
        "documentVersionId": str(uuid4()),
        "chunkSetId": str(uuid4()),
        "rerankerScore": 2.5,
        "outcome": "sent",
    }


def _trace(user: UUID, kept: list[dict[str, Any]], content: str) -> dict[str, Any]:
    retrieval = {
        "searchIndexId": str(uuid4()),
        "physicalIndexId": str(uuid4()),
        "physicalIndexName": "docchat-idx-1",
        "indexSource": "project_default",
        "queryEmbeddingDeployment": "embed-query",
        "embeddingModel": "text-embedding-3-large",
        "embeddingDimensions": 1024,
        "candidateCount": len(kept),
        "keepCount": len(kept),
        "rerankerThreshold": 1.5,
        "minConfidentResults": 1,
        "rerankPartial": False,
        "broadenAttempted": False,
        "broadened": False,
        "broadenOutcome": "not_needed",
        "searchPassage": None,
        "firstPass": [],
        "broadenedPass": None,
        "kept": kept,
        "screening": None,
    }
    model_input = {
        "rulesVersion": "1",
        "marker": MARKER,
        "deployment": "chat",
        "model": "gpt-5.6-sol",
        "maxCompletionTokens": 2000,
        "reasoningEffort": "low",
        "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": content}],
        "promptTokens": 900,
        "droppedTurns": 0,
        "droppedSourceIds": [],
    }
    return turn_trace_document(user_id=user, retrieval=retrieval, model_input=model_input).to_item()


async def test_t330_redact_document_removes_only_the_documents_text(
    cli: Cli, admin: Engine, containers: Containers
) -> None:
    document, other_document = uuid4(), uuid4()
    citing = f"{_source_block('S1', CANARY)}\n\n{_source_block('S2', 'text of document E')}"
    traces = [
        _trace(uuid4(), [_kept(1, "S1", document), _kept(2, "S2", other_document)], citing),
        _trace(uuid4(), [_kept(1, "S1", document), _kept(2, "S2", other_document)], citing),
    ]
    only_e = _trace(uuid4(), [_kept(1, "S1", other_document)], _source_block("S1", "E only"))
    stored = [await containers.conversations.create_item(trace) for trace in (*traces, only_e)]

    first = await cli.run("user_data", "user", "redact-document", "--document-id", str(document))
    assert first.code == ExitCode.SUCCESS, first.stderr
    report = first.json()
    assert report["traceCount"] == 2
    assert {(pair["userId"], pair["sessionId"]) for pair in report["sessions"]} == {
        (trace["userId"], trace["sessionId"]) for trace in traces
    }
    for before in stored[:2]:
        after = await containers.conversations.read_item(
            before["id"], [before["userId"], before["sessionId"]]
        )
        assert CANARY not in json.dumps(after)
        assert after["redactedAt"] is not None
        unchanged = {
            key for key in before if key not in {*SYSTEM_FIELDS, "modelInput", "redactedAt"}
        }
        assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}
        assert after["modelInput"]["messages"][0] == before["modelInput"]["messages"][0]
    untouched = await containers.conversations.read_item(
        stored[2]["id"], [stored[2]["userId"], stored[2]["sessionId"]]
    )
    assert untouched["_etag"] == stored[2]["_etag"]

    second = await cli.run("user_data", "user", "redact-document", "--document-id", str(document))
    assert second.code == ExitCode.SUCCESS, second.stderr
    assert second.json()["traceCount"] == 0

    rows = [row for row in await audit_rows(admin) if row.action == "document.traces_redacted"]
    assert [json.loads(row.metadata_json)["traceCount"] for row in rows] == [2, 0]
    assert [row.correlation_id for row in rows] == [first.correlation_id, second.correlation_id]

    register = await cli.run("user_data", "user", "deletion-register", "--since", _yesterday())
    assert register.code == ExitCode.SUCCESS, register.stderr
    assert [row["target_id"] for row in register.json()["register"]] == [str(document)] * 2
    assert len(await audit_rows(admin)) == len(rows)


async def test_t330_a_redaction_stopped_part_way_finishes_on_a_rerun(
    cli: Cli, containers: Containers
) -> None:
    document = uuid4()
    citing = _source_block("S1", CANARY)
    for _ in range(2):
        await containers.conversations.create_item(
            _trace(uuid4(), [_kept(1, "S1", document)], citing)
        )
    original = containers.conversations.replace_item
    replaced: list[str] = []

    async def stop_after_the_first(item: Any, body: Any, **kwargs: Any) -> Any:
        if replaced:
            raise _sdk_error(CosmosClientTimeoutError)
        replaced.append(str(item))
        return await original(item, body, **kwargs)

    containers.conversations.replace_item = stop_after_the_first  # type: ignore[method-assign]
    stopped = await cli.run("user_data", "user", "redact-document", "--document-id", str(document))
    assert stopped.code == ExitCode.INCOMPLETE
    containers.conversations.replace_item = original  # type: ignore[method-assign]
    rerun = await cli.run("user_data", "user", "redact-document", "--document-id", str(document))
    assert rerun.code == ExitCode.SUCCESS, rerun.stderr
    assert rerun.json()["traceCount"] == 1


async def test_read_conversation_at_level_read_writes_its_audit_row(
    cli: Cli, admin: Engine, containers: Containers, tmp_path: Path
) -> None:
    user, session_id = uuid4(), uuid4()
    await containers.conversations.create_item(
        session_document(user_id=user, session_id=session_id).to_item()
    )
    turn = turn_document(user_id=user, session_id=session_id)
    await containers.conversations.create_item(turn.to_item())
    await containers.conversations.create_item(
        turn_trace_document(user_id=user, session_id=session_id, turn=turn.id).to_item()
    )
    out = tmp_path / "read"
    outcome = await cli.run(
        "read",
        "user",
        "read-conversation",
        "--user",
        str(user),
        "--session",
        str(session_id),
        "--turn",
        str(turn.id),
        "--out",
        str(out),
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    written = await read_json(out / f"{session_id}.json")
    assert [item["id"] for item in written["turns"]] == [str(turn.id)]
    (row,) = [row for row in await audit_rows(admin) if row.action == "conversation.read"]
    assert row.correlation_id == outcome.correlation_id
    assert json.loads(row.metadata_json) == {
        "turnIds": [str(turn.id)],
        "ticket": TICKET,
        "level": "read",
    }


async def test_t546_without_the_level_nothing_is_read_or_written(
    cli: Cli, admin: Engine, tmp_path: Path
) -> None:
    out = tmp_path / "export"
    outcome = await cli.run("none", "user", "export", "--user", str(uuid4()), "--out", str(out))
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert not await asyncio.to_thread(out.exists)
    assert await audit_rows(admin) == []


async def test_t546_audit_events_lists_a_persons_rows_without_writing_one(
    cli: Cli, admin: Engine
) -> None:
    world = await seed_sql(admin)
    outcome = await cli.run(
        "user_data",
        "user",
        "audit-events",
        "--actor",
        str(world.user),
        "--since",
        _yesterday(),
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert [event["action"] for event in outcome.json()["events"]] == ["project.restored"]
    assert len(await audit_rows(admin)) == 2
