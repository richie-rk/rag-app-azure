"""Unit tests of the provisioning unit of work and the user search query Q6 (`11-api.md` sections
1.4.4 and 3.2.1), on a data-access layer without SQL."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Insert, create_engine
from sqlalchemy.sql.base import Executable

from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import users
from docchat_core.sql.statements import SET_XACT_ABORT_ON
from docchat_core.sql.tables.access import audit_events

DIALECT = create_engine("mssql+pyodbc://").dialect
OID = UUID("0a11ce00-0000-4000-8000-000000000001")
TID = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
PROJECT = uuid4()


def sql(statement: Executable) -> str:
    return re.sub(r"\s+", " ", statement.compile(dialect=DIALECT).string).strip()  # type: ignore[attr-defined]


def binds(statement: Executable) -> list[str]:
    positions: list[str] = statement.compile(dialect=DIALECT).positiontup  # type: ignore[attr-defined]
    return positions


# --- Provisioning ------------------------------------------------------------------------------


def test_the_update_locks_the_key_and_keeps_stored_display_values() -> None:
    assert sql(users.UPDATE_USER) == (
        "UPDATE app.users WITH (UPDLOCK, HOLDLOCK) SET tenant_id=?, "
        "display_name=CASE WHEN (N'' = ?) THEN app.users.display_name ELSE ? END, "
        "email=CASE WHEN (N'' = ?) THEN app.users.email ELSE ? END, "
        "last_known_role=?, last_seen_at=CASE WHEN (app.users.first_seen_at > SYSUTCDATETIME()) "
        "THEN app.users.first_seen_at ELSE SYSUTCDATETIME() END WHERE app.users.object_id = ?"
    )


def test_the_inserts_create_the_row_and_the_default_memberships() -> None:
    assert sql(users.INSERT_USER) == (
        "INSERT INTO app.users (object_id, tenant_id, display_name, email, last_known_role) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    assert sql(users.INSERT_DEFAULT_MEMBERSHIPS) == (
        "INSERT INTO app.project_members (user_object_id, project_id, grant_source, granted_by) "
        "SELECT ? AS anon_1, p.id, N'default', NULL AS anon_2 FROM app.projects AS p "
        "WHERE p.is_default = 1"
    )
    assert binds(users.INSERT_DEFAULT_MEMBERSHIPS) == ["oid"]


def _values(**overrides: object) -> dict[str, Any]:
    return {
        "oid": OID,
        "tid": TID,
        "display_name": "Ada",
        "email": "ada@example.test",
        "role": "User",
    } | overrides


def test_a_known_user_is_updated_only(fake_sql: Any) -> None:
    connection = fake_sql.connection(-1, 1)

    result = users._provision(connection, **_values())

    assert result == users.ProvisionResult(provisioned=False, default_project_count=0)
    statements = [statement for statement, _ in connection.executed]
    assert statements == [SET_XACT_ABORT_ON, users.UPDATE_USER]
    assert connection.executed[1][1] == _values()


def test_a_new_user_gets_the_row_the_memberships_and_the_audit_row(fake_sql: Any) -> None:
    connection = fake_sql.connection(-1, 0, 1, 2, 1)
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("request") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        result = users._provision(connection, **_values(role="Admin", display_name=""))

    assert result == users.ProvisionResult(provisioned=True, default_project_count=2)
    statements = [statement for statement, _ in connection.executed]
    assert statements[:4] == [
        SET_XACT_ABORT_ON,
        users.UPDATE_USER,
        users.INSERT_USER,
        users.INSERT_DEFAULT_MEMBERSHIPS,
    ]
    assert connection.executed[2][1] == _values(role="Admin", display_name="")
    assert connection.executed[3][1] == {"oid": OID}
    audit_statement, audit_row = connection.executed[4]
    assert isinstance(audit_statement, Insert)
    assert audit_statement.table is audit_events
    assert audit_row is not None
    assert audit_row == {
        "actor_type": "user",
        "actor_object_id": OID,
        "subject_object_id": OID,
        "action": "user.provisioned",
        "target_type": "user",
        "target_id": str(OID),
        "project_id": None,
        "metadata_json": json.dumps({"defaultProjectCount": 2}, separators=(",", ":")),
        "correlation_id": trace_id,
    }
    assert trace.get_current_span().get_span_context().is_valid is False


async def test_provisioning_is_one_retried_unit_of_work(fake_sql: Any) -> None:
    fake_sql.queue("ensure_user", fake_sql.connection(-1, 1))

    result = await users.provision_user(fake_sql, **_values())

    assert result.provisioned is False
    (call,) = fake_sql.calls
    assert (call.method, call.name, call.retry) == ("run", "ensure_user", "transient")


def test_provisioning_is_registered_for_the_plan_audit() -> None:
    entry = REQUEST_PATH_REGISTRY["docchat_core.sql.repositories.users.provision_user"]
    assert set(entry.example_params) == {"oid", "tid", "display_name", "email", "role"}


# --- Q6 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "pattern"),
    [
        ("ada", "ada%"),
        ("100%", "100\\%%"),
        ("a_b", "a\\_b%"),
        ("[x]", "\\[x]%"),
        ("c:\\temp", "c:\\\\temp%"),
    ],
)
def test_a_prefix_escapes_the_like_wildcards(q: str, pattern: str) -> None:
    assert users.like_prefix(q) == pattern


@pytest.mark.parametrize(
    ("q", "form"),
    [
        (None, "all"),
        ("ada", "prefix"),
        ("0A11CE00-0000-4000-8000-000000000001", "object_id"),
        ("0a11ce00000040008000000000000001", "prefix"),
        ("{0a11ce00-0000-4000-8000-000000000001}", "prefix"),
    ],
)
def test_the_form_follows_the_text(q: str | None, form: str) -> None:
    assert users.search_form(q) == form


_COLUMNS = (
    "SELECT TOP (?) u.object_id, u.display_name, u.email, u.last_known_role, u.first_seen_at, "
    "u.last_seen_at"
)
_MEMBER = ", CASE WHEN (m.user_object_id IS NULL) THEN 0 ELSE 1 END AS is_member"
_HIT = (
    " FROM (SELECT app.users.object_id AS object_id FROM app.users "
    "WHERE app.users.display_name LIKE ? ESCAPE '\\' UNION "
    "SELECT app.users.object_id AS object_id FROM app.users "
    "WHERE app.users.email LIKE ? ESCAPE '\\') AS hit JOIN app.users AS u "
    "ON u.object_id = hit.object_id"
)
_JOIN = (
    " LEFT OUTER JOIN app.project_members AS m ON m.user_object_id = u.object_id "
    "AND m.project_id = ?"
)
_CURSOR = "u.display_name > ? OR u.display_name = ? AND u.object_id > ?"
_ORDER = " ORDER BY u.display_name, u.object_id"


def test_q6_prefix_form_unions_two_seeks() -> None:
    assert sql(users.USER_PAGES[("prefix", False, False)]) == _COLUMNS + _HIT + _ORDER
    full = users.USER_PAGES[("prefix", True, True)]
    assert sql(full) == _COLUMNS + _MEMBER + _HIT + _JOIN + " WHERE " + _CURSOR + _ORDER
    assert binds(full) == [
        "limit_plus_one",
        "prefix",
        "prefix",
        "project_id",
        "after_display_name",
        "after_display_name",
        "after_object_id",
    ]


def test_q6_object_id_form_seeks_the_key() -> None:
    assert sql(users.USER_PAGES[("object_id", False, False)]) == (
        _COLUMNS + " FROM app.users AS u WHERE u.object_id = ?" + _ORDER
    )
    assert sql(users.USER_PAGES[("object_id", True, True)]) == (
        _COLUMNS
        + _MEMBER
        + " FROM app.users AS u"
        + _JOIN
        + " WHERE u.object_id = ? AND ("
        + _CURSOR
        + ")"
        + _ORDER
    )


def test_q6_without_q_reads_in_display_name_order() -> None:
    assert (
        sql(users.USER_PAGES[("all", False, False)]) == _COLUMNS + " FROM app.users AS u" + _ORDER
    )
    assert sql(users.USER_PAGES[("all", True, False)]) == (
        _COLUMNS + _MEMBER + " FROM app.users AS u" + _JOIN + _ORDER
    )
    assert len(users.USER_PAGES) == 12


def _user(name: str, *, is_member: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        object_id=uuid4(),
        display_name=name,
        email=f"{name}@example.test",
        last_known_role="User",
        first_seen_at=datetime(2026, 10, 1, 8, 0),  # noqa: DTZ001 - naive UTC from SQL
        last_seen_at=datetime(2026, 10, 30, 8, 0),  # noqa: DTZ001 - naive UTC from SQL
        is_member=is_member,
    )


async def test_q6_binds_each_forms_values(fake_sql: Any) -> None:
    after = users.UserPosition("Ada", OID)
    fake_sql.queue("search_users", [_user("ada", is_member=1)], [_user("bo")], [], [])

    (member,) = await users.read_users(
        fake_sql, q="a_d", project_id=PROJECT, after=None, limit_plus_one=51
    )
    (plain,) = await users.read_users(
        fake_sql, q=None, project_id=None, after=after, limit_plus_one=11
    )
    await users.read_users(
        fake_sql, q=str(OID).upper(), project_id=None, after=None, limit_plus_one=2
    )
    await users.read_users(fake_sql, q="x", project_id=PROJECT, after=after, limit_plus_one=3)

    assert member.is_member is True
    assert member.first_seen_at == datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert plain.is_member is None
    assert plain.last_seen_at.tzinfo is UTC
    first, second, third, fourth = fake_sql.calls
    assert first.statement is users.USER_PAGES[("prefix", True, False)]
    assert first.parameters == {"limit_plus_one": 51, "prefix": "a\\_d%", "project_id": PROJECT}
    assert second.statement is users.USER_PAGES[("all", False, True)]
    assert second.parameters == {
        "limit_plus_one": 11,
        "after_display_name": "Ada",
        "after_object_id": OID,
    }
    assert third.statement is users.USER_PAGES[("object_id", False, False)]
    assert third.parameters == {"limit_plus_one": 2, "oid": OID}
    assert fourth.statement is users.USER_PAGES[("prefix", True, True)]
    assert {call.name for call in fake_sql.calls} == {"search_users"}


async def test_q6_a_member_flag_of_zero_is_false(fake_sql: Any) -> None:
    fake_sql.queue("search_users", [_user("ada", is_member=0)])
    (row,) = await users.read_users(
        fake_sql, q=None, project_id=PROJECT, after=None, limit_plus_one=2
    )
    assert row.is_member is False


def test_the_search_is_registered_for_the_plan_audit() -> None:
    entry = REQUEST_PATH_REGISTRY["docchat_core.sql.repositories.users.read_users"]
    assert entry.example_params["q"] == "a"
