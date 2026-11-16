"""Unit tests of the access queries Q1 to Q5 (`11-api.md` section 1.4.4): their statement text,
their bound parameters and the rows they return, on a data-access layer without SQL."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.sql.base import Executable

from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import access

DIALECT = create_engine("mssql+pyodbc://").dialect
OID = uuid4()
PROJECT = uuid4()
INDEX = uuid4()


def sql(statement: Executable) -> str:
    return re.sub(r"\s+", " ", statement.compile(dialect=DIALECT).string).strip()  # type: ignore[attr-defined]


def binds(statement: Executable) -> list[str]:
    positions: list[str] = statement.compile(dialect=DIALECT).positiontup  # type: ignore[attr-defined]
    return positions


def test_q1_reads_the_project_and_the_callers_membership() -> None:
    assert sql(access.PROJECT_FOR_CHAT) == (
        "SELECT p.id, p.name, p.description, p.instructions, p.example_questions_json, p.status, "
        "CASE WHEN (m.user_object_id IS NULL) THEN 0 ELSE 1 END AS is_member "
        "FROM app.projects AS p LEFT OUTER JOIN app.project_members AS m "
        "ON m.user_object_id = ? AND m.project_id = p.id WHERE p.id = ?"
    )
    assert binds(access.PROJECT_FOR_CHAT) == ["oid", "project_id"]


def test_q2_joins_the_unexpired_override_and_the_default_to_one_anchor_row() -> None:
    assert sql(access.INDEX_CHOICE) == (
        "SELECT o.search_index_id AS override_index_id, o.expires_at AS override_expires_at, "
        "d.search_index_id AS default_index_id FROM (SELECT 1 AS anchor) AS a "
        "LEFT OUTER JOIN app.user_index_overrides AS o ON o.user_object_id = ? "
        "AND o.project_id = ? AND o.expires_at > SYSUTCDATETIME() "
        "LEFT OUTER JOIN app.project_indexes AS d ON d.project_id = ? AND d.is_default = 1"
    )
    assert binds(access.INDEX_CHOICE) == ["oid", "project_id", "project_id"]


def test_q3_reads_the_active_physical_index() -> None:
    assert sql(access.INDEX_RECORD) == (
        "SELECT s.id, s.status, p.id AS physical_index_id, p.name, p.schema_version, "
        "p.embedding_model, p.embedding_dimensions, p.query_embedding_deployment "
        "FROM app.search_indexes AS s LEFT OUTER JOIN app.physical_indexes AS p "
        "ON p.id = s.active_physical_index_id AND p.state = N'active' WHERE s.id = ?"
    )


def test_q4_has_an_administrator_form_and_a_member_form() -> None:
    assert sql(access.ACTIVE_PROJECT_IDS) == (
        "SELECT p.id FROM app.projects AS p WHERE p.status = N'active'"
    )
    assert sql(access.MEMBER_PROJECT_IDS) == (
        "SELECT m.project_id FROM app.project_members AS m JOIN app.projects AS p "
        "ON p.id = m.project_id WHERE m.user_object_id = ? AND p.status = N'active'"
    )


_Q5_COLUMNS = (
    "SELECT TOP (?) p.id, p.name, p.description, p.example_questions_json, "
    "CASE WHEN (ph.id IS NOT NULL) THEN 1 ELSE 0 END AS has_default_index "
)
_Q5_JOINS = (
    "LEFT OUTER JOIN app.project_indexes AS d ON d.project_id = p.id AND d.is_default = 1 "
    "LEFT OUTER JOIN app.search_indexes AS s ON s.id = d.search_index_id "
    "LEFT OUTER JOIN app.physical_indexes AS ph "
    "ON ph.id = s.active_physical_index_id AND ph.state = N'active' "
)


@pytest.mark.parametrize(
    ("admin", "cursor", "source", "where", "parameters"),
    [
        (
            True,
            False,
            "FROM app.projects AS p ",
            "WHERE p.status = N'active' ",
            ["limit_plus_one"],
        ),
        (
            True,
            True,
            "FROM app.projects AS p ",
            "WHERE p.status = N'active' AND p.name > ? ",
            ["limit_plus_one", "after_name"],
        ),
        (
            False,
            False,
            "FROM app.project_members AS m JOIN app.projects AS p ON p.id = m.project_id ",
            "WHERE m.user_object_id = ? AND p.status = N'active' ",
            ["limit_plus_one", "oid"],
        ),
        (
            False,
            True,
            "FROM app.project_members AS m JOIN app.projects AS p ON p.id = m.project_id ",
            "WHERE m.user_object_id = ? AND p.status = N'active' AND p.name > ? ",
            ["limit_plus_one", "oid", "after_name"],
        ),
    ],
)
def test_q5_has_one_constant_text_per_case(
    admin: bool, cursor: bool, source: str, where: str, parameters: list[str]
) -> None:
    statement = access.MY_PROJECTS[(admin, cursor)]
    assert sql(statement) == _Q5_COLUMNS + source + _Q5_JOINS + where + "ORDER BY p.name"
    assert binds(statement) == parameters


def test_no_statement_tests_a_parameter_for_null() -> None:
    statements = [
        access.PROJECT_FOR_CHAT,
        access.INDEX_CHOICE,
        access.INDEX_RECORD,
        access.ACTIVE_PROJECT_IDS,
        access.MEMBER_PROJECT_IDS,
        *access.MY_PROJECTS.values(),
    ]
    for statement in statements:
        assert "? IS NULL" not in sql(statement)


def test_every_query_is_registered_for_the_plan_audit() -> None:
    names = {
        "read_project_for_chat",
        "read_index_choice",
        "read_index_record",
        "read_active_project_ids",
        "read_member_project_ids",
        "read_all_active_projects",
        "read_member_projects",
    }
    registered = {
        key.rsplit(".", 1)[1]: entry
        for key, entry in REQUEST_PATH_REGISTRY.items()
        if key.startswith("docchat_core.sql.repositories.access.")
    }
    assert set(registered) == names
    assert registered["read_index_record"].example_params.keys() == {"search_index_id"}


async def test_q1_returns_the_row_or_none(fake_sql: Any) -> None:
    fake_sql.queue(
        "authorize_project",
        SimpleNamespace(
            id=PROJECT,
            name="A",
            description="d",
            instructions="i",
            example_questions_json="[]",
            status="active",
            is_member=1,
        ),
        None,
    )

    row = await access.read_project_for_chat(fake_sql, oid=OID, project_id=PROJECT)
    assert row == access.ProjectForChatRow(PROJECT, "A", "d", "i", "[]", "active", is_member=True)
    assert await access.read_project_for_chat(fake_sql, oid=OID, project_id=PROJECT) is None
    (call, _) = fake_sql.calls
    assert call.method == "fetch_one"
    assert call.statement is access.PROJECT_FOR_CHAT
    assert call.parameters == {"oid": OID, "project_id": PROJECT}
    assert call.retry == "transient"


async def test_q2_returns_both_branches_with_a_utc_expiry(fake_sql: Any) -> None:
    default = uuid4()
    fake_sql.queue(
        "resolve_index",
        SimpleNamespace(
            override_index_id=INDEX,
            override_expires_at=datetime(2026, 11, 7, 9, 0),  # noqa: DTZ001 - naive UTC from SQL
            default_index_id=default,
        ),
        SimpleNamespace(override_index_id=None, override_expires_at=None, default_index_id=None),
        None,
    )

    choice = await access.read_index_choice(fake_sql, oid=OID, project_id=PROJECT)
    assert choice == access.IndexChoiceRow(INDEX, datetime(2026, 11, 7, 9, tzinfo=UTC), default)
    empty = access.IndexChoiceRow(None, None, None)
    assert await access.read_index_choice(fake_sql, oid=OID, project_id=PROJECT) == empty
    assert await access.read_index_choice(fake_sql, oid=OID, project_id=PROJECT) == empty
    assert fake_sql.calls[0].parameters == {"oid": OID, "project_id": PROJECT}


async def test_q3_returns_the_record_with_or_without_its_physical_index(fake_sql: Any) -> None:
    physical = uuid4()
    fake_sql.queue(
        "read_index_record",
        SimpleNamespace(
            id=INDEX,
            status="ready",
            physical_index_id=physical,
            name="docchat-abc-g1",
            schema_version=1,
            embedding_model="text-embedding-3-large",
            embedding_dimensions=1024,
            query_embedding_deployment="embed-query-te3-large-v1",
        ),
        SimpleNamespace(
            id=INDEX,
            status="provisioning",
            physical_index_id=None,
            name=None,
            schema_version=None,
            embedding_model=None,
            embedding_dimensions=None,
            query_embedding_deployment=None,
        ),
        None,
    )

    record = await access.read_index_record(fake_sql, search_index_id=INDEX)
    assert record == access.IndexRecordRow(
        INDEX,
        "ready",
        access.PhysicalIndexRow(
            physical,
            "docchat-abc-g1",
            1,
            "text-embedding-3-large",
            1024,
            "embed-query-te3-large-v1",
        ),
    )
    pending = await access.read_index_record(fake_sql, search_index_id=INDEX)
    assert pending == access.IndexRecordRow(INDEX, "provisioning", None)
    assert await access.read_index_record(fake_sql, search_index_id=INDEX) is None
    assert fake_sql.calls[0].parameters == {"search_index_id": INDEX}


async def test_q4_returns_the_ids_of_each_form(fake_sql: Any) -> None:
    first, second = uuid4(), uuid4()
    fake_sql.queue(
        "reachable_projects",
        [SimpleNamespace(id=first), SimpleNamespace(id=second)],
        [SimpleNamespace(project_id=first)],
    )

    assert await access.read_active_project_ids(fake_sql) == frozenset({first, second})
    assert await access.read_member_project_ids(fake_sql, oid=OID) == frozenset({first})
    admin_call, member_call = fake_sql.calls
    assert (admin_call.statement, admin_call.parameters) == (access.ACTIVE_PROJECT_IDS, None)
    assert (member_call.statement, member_call.parameters) == (
        access.MEMBER_PROJECT_IDS,
        {"oid": OID},
    )


def _project_row(name: str, *, has_default: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        description="",
        example_questions_json='["q"]',
        has_default_index=has_default,
    )


async def test_q5_picks_the_statement_of_its_case(fake_sql: Any) -> None:
    fake_sql.queue(
        "list_my_projects",
        [_project_row("A", has_default=1)],
        [_project_row("B", has_default=0)],
        [],
        [],
    )

    (admin,) = await access.read_all_active_projects(fake_sql, after_name=None, limit_plus_one=51)
    (member,) = await access.read_member_projects(
        fake_sql, oid=OID, after_name=None, limit_plus_one=11
    )
    await access.read_all_active_projects(fake_sql, after_name="A", limit_plus_one=51)
    await access.read_member_projects(fake_sql, oid=OID, after_name="B", limit_plus_one=11)

    assert (admin.name, admin.has_default_index) == ("A", True)
    assert (member.name, member.has_default_index, member.example_questions_json) == (
        "B",
        False,
        '["q"]',
    )
    statements = [call.statement for call in fake_sql.calls]
    assert statements == [
        access.MY_PROJECTS[(True, False)],
        access.MY_PROJECTS[(False, False)],
        access.MY_PROJECTS[(True, True)],
        access.MY_PROJECTS[(False, True)],
    ]
    assert [call.parameters for call in fake_sql.calls] == [
        {"limit_plus_one": 51},
        {"oid": OID, "limit_plus_one": 11},
        {"limit_plus_one": 51, "after_name": "A"},
        {"oid": OID, "limit_plus_one": 11, "after_name": "B"},
    ]
