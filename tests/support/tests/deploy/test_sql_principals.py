"""Unit tests of the grants step (`80-data-model.md` section 2.22; ADR 0125; T-082, T-083)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sql_principals
from sql_principals import (
    ContainedUser,
    Principal,
    StepError,
    expected_users,
    plan,
    read_azd_environment,
    read_groups,
    sid_literal,
)
from sqlalchemy.sql.elements import TextClause

from docchat_core.sql import statements

API_CLIENT = "00112233-4455-6677-8899-aabbccddeeff"
INGEST_CLIENT = "01234567-89ab-cdef-0123-456789abcdef"
API_PRINCIPAL = "ffffffff-0000-1111-2222-333333333333"
INGEST_PRINCIPAL = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
CONFIG_GROUP = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D"
READER_GROUP = "11111111-2222-4333-8444-555555555555"
DATA_GROUP = "66666666-7777-4888-9999-aaaaaaaaaaaa"

OUTPUTS = {
    "DOCCHAT_API_IDENTITY_CLIENT_ID": API_CLIENT,
    "DOCCHAT_INGESTION_IDENTITY_CLIENT_ID": INGEST_CLIENT,
    "DOCCHAT_API_IDENTITY_PRINCIPAL_ID": API_PRINCIPAL,
    "DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID": INGEST_PRINCIPAL,
}
GROUPS = {
    "configOperators": CONFIG_GROUP,
    "conversationReaders": READER_GROUP,
    "userDataOperators": DATA_GROUP,
}


@pytest.mark.parametrize(
    ("identifier", "sid"),
    [
        # CONVERT(VARBINARY(16), <uniqueidentifier>) reverses the first three groups.
        (API_CLIENT, "33221100554477668899aabbccddeeff"),
        (INGEST_CLIENT, "67452301ab89efcd0123456789abcdef"),
        (API_PRINCIPAL, "ffffffff000011112222333333333333"),
        (INGEST_PRINCIPAL, "9e0d2c3f7a8bc641a5d4e3f2c1b0a998"),
        (CONFIG_GROUP, "d4c3b2a1f6e57b4a8c9d0e1f2a3b4c5d"),
    ],
)
def test_t082_sid_literal(identifier: str, sid: str) -> None:
    assert sid_literal(identifier) == sid


@pytest.mark.parametrize(
    "identifier",
    [
        "not-a-uuid",
        "00112233445566778899aabbccddeeff",
        "{00112233-4455-6677-8899-aabbccddeeff}",
        "",
    ],
)
def test_t082_non_uuid_is_refused(identifier: str) -> None:
    with pytest.raises(StepError, match="UUID"):
        sid_literal(identifier)


def test_t082_identities_from_outputs_groups_from_the_environment_file() -> None:
    users = expected_users("dev", outputs=OUTPUTS, groups=GROUPS)
    assert users == (
        ContainedUser("id-docchat-api-dev", "E", sid_literal(API_CLIENT), "docchat_api"),
        ContainedUser(
            "id-docchat-ingest-dev", "E", sid_literal(INGEST_CLIENT), "docchat_ingestion"
        ),
        ContainedUser(
            "sg-docchat-dev-config-operators",
            "X",
            sid_literal(CONFIG_GROUP),
            "docchat_config_operator",
        ),
        ContainedUser(
            "sg-docchat-dev-conversation-readers",
            "X",
            sid_literal(READER_GROUP),
            "docchat_conversation_reader",
        ),
        ContainedUser(
            "sg-docchat-dev-user-data-operators",
            "X",
            sid_literal(DATA_GROUP),
            "docchat_user_data_operator",
        ),
    )
    by_object_id = expected_users("prod", outputs=OUTPUTS, groups=GROUPS, sid_source="object-id")
    assert [user.sid_hex for user in by_object_id[:2]] == [
        sid_literal(API_PRINCIPAL),
        sid_literal(INGEST_PRINCIPAL),
    ]
    assert by_object_id[2:] == tuple(
        ContainedUser(user.name.replace("-dev-", "-prod-"), "X", user.sid_hex, user.role)
        for user in users[2:]
    )


def test_missing_inputs_are_refused() -> None:
    with pytest.raises(StepError, match="DOCCHAT_API_IDENTITY_CLIENT_ID"):
        expected_users("dev", outputs={}, groups=GROUPS)
    with pytest.raises(StepError, match=r"groups\.userDataOperators"):
        expected_users(
            "dev",
            outputs=OUTPUTS,
            groups={"configOperators": CONFIG_GROUP, "conversationReaders": READER_GROUP},
        )


def texts(planned: list[TextClause]) -> list[str]:
    return [statement.text for statement in planned]


def applied_state(
    users: tuple[ContainedUser, ...],
) -> tuple[list[Principal], list[tuple[str, str]]]:
    principals = [Principal(u.name, u.user_type, bytes.fromhex(u.sid_hex)) for u in users]
    principals.append(Principal("dbo", "S", b"\x01"))
    memberships = [(u.role, u.name) for u in users] + [("db_owner", "dbo")]
    return principals, memberships


def test_t083_second_run_issues_nothing() -> None:
    users = expected_users("dev", outputs=OUTPUTS, groups=GROUPS)
    first = plan(users, [Principal("dbo", "S", b"\x01")], [("db_owner", "dbo")])
    assert texts(first) == [
        *(f"CREATE USER [{u.name}] WITH SID = 0x{u.sid_hex}, TYPE = {u.user_type}" for u in users),
        *(f"ALTER ROLE {u.role} ADD MEMBER [{u.name}]" for u in users),
    ]
    principals, memberships = applied_state(users)
    assert plan(users, principals, memberships) == []


def test_a_recreated_identity_is_recreated_with_its_new_sid() -> None:
    users = expected_users("dev", outputs=OUTPUTS, groups=GROUPS)
    principals, memberships = applied_state(users)
    principals[1] = Principal("id-docchat-ingest-dev", "E", bytes.fromhex("00" * 16))
    planned = texts(plan(users, principals, memberships))
    assert planned == [
        "DROP USER [id-docchat-ingest-dev]",
        f"CREATE USER [id-docchat-ingest-dev] WITH SID = 0x{users[1].sid_hex}, TYPE = E",
        "ALTER ROLE docchat_ingestion ADD MEMBER [id-docchat-ingest-dev]",
    ]


def test_unexpected_members_of_the_five_roles_are_removed() -> None:
    users = expected_users("dev", outputs=OUTPUTS, groups=GROUPS)
    principals, memberships = applied_state(users)
    memberships += [
        ("docchat_api", "someone"),
        ("docchat_config_operator", "id-docchat-api-dev"),
        ("db_datareader", "id-docchat-api-dev"),
    ]
    assert texts(plan(users, principals, memberships)) == [
        "ALTER ROLE docchat_api DROP MEMBER [someone]",
        "ALTER ROLE docchat_config_operator DROP MEMBER [id-docchat-api-dev]",
    ]


def test_a_user_of_another_type_is_recreated() -> None:
    users = expected_users("dev", outputs=OUTPUTS, groups=GROUPS)
    principals, memberships = applied_state(users)
    principals[2] = Principal(users[2].name, "S", bytes.fromhex(users[2].sid_hex))
    assert texts(plan(users, principals, memberships))[0] == f"DROP USER [{users[2].name}]"


def test_read_azd_environment(tmp_path: Path) -> None:
    folder = tmp_path / ".azure" / "dev"
    folder.mkdir(parents=True)
    (folder / ".env").write_text(
        '# comment\nDOCCHAT_SQL_SERVER="sql-x.database.windows.net"\nEMPTY=""\nnot a line\n',
        encoding="utf-8",
    )
    values = read_azd_environment("dev", tmp_path, environ={"EMPTY": "set"})
    assert values == {"DOCCHAT_SQL_SERVER": "sql-x.database.windows.net", "EMPTY": "set"}


def test_read_groups(tmp_path: Path) -> None:
    (tmp_path / "infra" / "env").mkdir(parents=True)
    (tmp_path / "infra" / "env" / "dev.json").write_text(json.dumps({"groups": GROUPS}))
    assert read_groups("dev", tmp_path) == GROUPS
    with pytest.raises(StepError, match=r"prod\.json"):
        read_groups("prod", tmp_path)
    (tmp_path / "infra" / "env" / "load.json").write_text("{}")
    with pytest.raises(StepError, match="no groups"):
        read_groups("load", tmp_path)


class FakeConnection:
    def __init__(self, principals: list[Principal], memberships: list[tuple[str, str]]) -> None:
        self.principals = principals
        self.memberships = memberships
        self.executed: list[str] = []

    def execute(self, statement: TextClause) -> list[Any]:
        if statement is statements.DATABASE_PRINCIPALS:
            return [
                SimpleNamespace(name=p.name, type=p.principal_type, sid=p.sid)
                for p in self.principals
            ]
        if statement is statements.ROLE_MEMBERS:
            return [SimpleNamespace(role_name=r, member_name=m) for r, m in self.memberships]
        self.executed.append(statement.text)
        return []


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        yield self.connection

    def dispose(self) -> None:
        self.disposed = True


def prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str | None]]:
    (tmp_path / "infra" / "env").mkdir(parents=True)
    (tmp_path / "infra" / "env" / "dev.json").write_text(json.dumps({"groups": GROUPS}))
    monkeypatch.setattr(sql_principals, "REPOSITORY", tmp_path)
    monkeypatch.setattr(
        sql_principals,
        "read_azd_environment",
        lambda environment: dict(OUTPUTS),
    )
    monkeypatch.setattr(
        sql_principals, "read_groups", lambda environment: read_groups(environment, tmp_path)
    )
    monkeypatch.setattr(sql_principals, "access_token", lambda: "token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return []


def test_main_creates_the_users(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(monkeypatch, tmp_path)
    connection = FakeConnection([], [])
    engine = FakeEngine(connection)
    calls: list[tuple[str, str | None]] = []

    def create_sync_engine(service: str, *, access_token: str | None) -> FakeEngine:
        calls.append((service, access_token))
        return engine

    monkeypatch.setattr(sql_principals, "create_sync_engine", create_sync_engine)
    assert sql_principals.main(["--env", "dev"]) == 0
    assert calls == [("grants", "token")]
    assert len(connection.executed) == 10
    assert engine.disposed
    assert json.loads(capsys.readouterr().out) == {"statements": 10, "users": 5}


def test_a_bad_id_stops_before_any_sql(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sql_principals,
        "read_azd_environment",
        lambda environment: {**OUTPUTS, "DOCCHAT_API_IDENTITY_CLIENT_ID": "x'; DROP USER dbo;--"},
    )

    def no_engine(service: str, *, access_token: str | None) -> FakeEngine:
        raise AssertionError("no SQL may run")

    monkeypatch.setattr(sql_principals, "create_sync_engine", no_engine)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert sql_principals.main(["--env", "dev"]) == 2
    line = json.loads(capsys.readouterr().err)
    assert line == {
        "code": "sql_principal_failed",
        "message": "A migration or a principal grant failed; nothing was deployed.",
        "detail": "inputs: an identity or group ID isn't a UUID",
    }
    assert json.loads(summary.read_text()) == line


def test_a_sql_failure_reports_its_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare(monkeypatch, tmp_path)

    def failing(service: str, *, access_token: str | None) -> FakeEngine:
        raise RuntimeError("Login failed for user 'secret@example.com'")

    monkeypatch.setattr(sql_principals, "create_sync_engine", failing)
    assert sql_principals.main(["--env", "dev"]) == 2
    error = capsys.readouterr().err
    assert json.loads(error)["detail"] == "grants: RuntimeError"
    assert "secret" not in error


def test_access_token_comes_from_the_azure_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    class Credential:
        def get_token(self, scope: str) -> Any:
            assert scope == "https://database.windows.net/.default"
            return SimpleNamespace(token="cli")

    monkeypatch.setattr(sql_principals, "AzureCliCredential", Credential)
    assert sql_principals.access_token() == "cli"
