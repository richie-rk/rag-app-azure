"""Unit tests of the administrator project, member and mapping statements and units of work
(`11-api.md` sections 3.7 and 3.9; ADR 0022), on a scripted connection without SQL.

Each unit of work runs against `ScriptedConnection`, which answers each statement with the
results queued for it and records every statement, so a test proves the order of the steps
(lookups, then the version, then the state rules), what was written and which audit rows the
transaction carries.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Insert, create_engine
from sqlalchemy.sql.base import Executable

from docchat_core.errors import DocChatProblem
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY, SqlUniqueViolation
from docchat_core.sql.repositories import projects
from docchat_core.sql.statements import SET_XACT_ABORT_ON
from docchat_core.sql.tables.access import audit_events

DIALECT = create_engine("mssql+pyodbc://").dialect
PROJECT = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
USER = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
INDEX = UUID("3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f")
OTHER_INDEX = UUID("4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f8a")
CALLER = UUID("0ca20100-0000-4000-8000-000000000003")
RV = bytes(range(1, 9))
NEW_RV = bytes(range(11, 19))
STAMP = datetime(2026, 10, 30, 9, 15, 2, 123000)  # noqa: DTZ001 - naive UTC from SQL


def sql(statement: Executable) -> str:
    return re.sub(r"\s+", " ", statement.compile(dialect=DIALECT).string).strip()  # type: ignore[attr-defined]


def binds(statement: Executable) -> list[str]:
    positions: list[str] = statement.compile(dialect=DIALECT).positiontup  # type: ignore[attr-defined]
    return positions


# --- A scripted connection and engine --------------------------------------------------------


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)
    rowcount: int = 1

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        assert len(self.rows) == 1, "exactly one row expected"
        return self.rows[0]

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one(self) -> Any:
        return self.one()[0]


class ScriptedConnection:
    """Answers each statement with the next result queued for it (an empty result with row
    count 1 when none is queued) and records what ran."""

    def __init__(self) -> None:
        self._answers: defaultdict[int, deque[Result]] = defaultdict(deque)
        self.executed: list[tuple[Any, Mapping[str, Any] | None]] = []

    def answer(self, statement: object, *results: Result) -> ScriptedConnection:
        self._answers[id(statement)].extend(results)
        return self

    def execute(self, statement: Any, parameters: Mapping[str, Any] | None = None) -> Result:
        self.executed.append((statement, parameters))
        queued = self._answers.get(id(statement))
        return queued.popleft() if queued else Result()

    def statements(self) -> list[Any]:
        return [statement for statement, _ in self.executed]

    def parameters_of(self, statement: object) -> list[Mapping[str, Any] | None]:
        return [parameters for ran, parameters in self.executed if ran is statement]

    def audit_rows(self) -> list[Mapping[str, Any]]:
        return [
            parameters or {}
            for statement, parameters in self.executed
            if isinstance(statement, Insert) and statement.table is audit_events
        ]


@dataclass
class Call:
    method: str
    name: str
    statement: object
    parameters: Mapping[str, Any] | None
    retry: str


@dataclass
class ScriptedEngine:
    """Stands in for `SqlEngine`: a unit of work runs on `connection`; `fetch_*` answer from
    `fetched`; `failure` is raised by `run`."""

    connection: ScriptedConnection = field(default_factory=ScriptedConnection)
    fetched: deque[Any] = field(default_factory=deque)
    failure: BaseException | None = None
    calls: list[Call] = field(default_factory=list)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.calls.append(Call("run", name, work, None, retry))
        if self.failure is not None:
            raise self.failure
        return work(self.connection)

    async def fetch_one(
        self, statement: object, parameters: Mapping[str, Any] | None = None, *, name: str
    ) -> Any:
        self.calls.append(Call("fetch_one", name, statement, parameters, "transient"))
        return self.fetched.popleft()

    async def fetch_all(
        self, statement: object, parameters: Mapping[str, Any] | None = None, *, name: str
    ) -> Any:
        self.calls.append(Call("fetch_all", name, statement, parameters, "transient"))
        return self.fetched.popleft()


class Precondition(DocChatProblem):
    code = "precondition_failed"


class Required(DocChatProblem):
    code = "precondition_required"


class Unrelated(DocChatProblem):
    code = "internal_error"


@dataclass
class Version:
    """A `VersionCheck` that records what it compared and refuses as told."""

    refuse: type[DocChatProblem] | None = None
    seen: list[bytes] = field(default_factory=list)

    def require_row_version(self, current: bytes, /) -> None:
        self.seen.append(current)
        if self.refuse is not None:
            raise self.refuse()


def locked(**overrides: Any) -> Result:
    row = {
        "status": "active",
        "is_default": 0,
        "name": "Field notes",
        "description": "Notes",
        "instructions": "Be brief.",
        "example_questions_json": '["What changed?"]',
        "row_version": RV,
    } | overrides
    return Result([SimpleNamespace(**row)])


def project_row(**overrides: Any) -> SimpleNamespace:
    row = {
        "id": PROJECT,
        "name": "Field notes",
        "description": "Notes",
        "instructions": "Be brief.",
        "example_questions_json": '["What changed?"]',
        "is_default": 1,
        "status": "active",
        "member_count": 2,
        "index_count": 1,
        "default_index_id": INDEX,
        "default_index_name": "Handbook",
        "created_at": STAMP,
        "created_by": CALLER,
        "created_by_name": "Carol",
        "updated_at": STAMP,
        "updated_by": USER,
        "updated_by_name": None,
        "archived_at": None,
        "archived_by": None,
        "archived_by_name": None,
        "row_version": NEW_RV,
    } | overrides
    return SimpleNamespace(**row)


def member_row(**overrides: Any) -> SimpleNamespace:
    row = {
        "object_id": USER,
        "display_name": "Ada",
        "email": "ada@example.test",
        "grant_source": "admin",
        "granted_at": STAMP,
        "granted_by": CALLER,
        "granted_by_name": "Carol",
        "last_seen_at": STAMP,
    } | overrides
    return SimpleNamespace(**row)


def mapping_row(**overrides: Any) -> SimpleNamespace:
    row = {
        "id": INDEX,
        "display_name": "Handbook",
        "status": "ready",
        "is_default": 1,
        "created_at": STAMP,
        "created_by": CALLER,
        "created_by_name": None,
        "updated_at": STAMP,
    } | overrides
    return SimpleNamespace(**row)


def moved() -> Result:
    return Result([SimpleNamespace(row_version=NEW_RV)])


def actions(connection: ScriptedConnection) -> list[str]:
    return [row["action"] for row in connection.audit_rows()]


# --- Statements ------------------------------------------------------------------------------


def test_the_version_check_locks_the_project_row() -> None:
    assert sql(projects.LOCK_PROJECT) == (
        "SELECT app.projects.status, app.projects.is_default, app.projects.name, "
        "app.projects.description, app.projects.instructions, "
        "app.projects.example_questions_json, app.projects.row_version "
        "FROM app.projects WITH (UPDLOCK, ROWLOCK) WHERE app.projects.id = ?"
    )
    assert sql(projects.LOCK_USER) == (
        "SELECT 1 AS found FROM app.users WITH (UPDLOCK, ROWLOCK) WHERE app.users.object_id = ?"
    )
    assert "WITH (UPDLOCK, ROWLOCK)" in sql(projects.LOCK_INDEX)
    assert "IN (__[POSTCOMPILE_user_ids])" in sql(projects.LOCK_USERS)


def test_the_version_move_is_guarded_and_returns_the_new_version() -> None:
    assert sql(projects.MOVE_VERSION) == (
        "UPDATE app.projects SET updated_at=SYSUTCDATETIME(), updated_by=? "
        "OUTPUT inserted.row_version WHERE app.projects.id = ? AND app.projects.row_version = ?"
    )
    for statement in (
        projects.UPDATE_FIELDS,
        projects.ARCHIVE_PROJECT,
        projects.RESTORE_PROJECT,
    ):
        text = sql(statement)
        assert "OUTPUT inserted.row_version" in text
        assert text.endswith("WHERE app.projects.id = ? AND app.projects.row_version = ?")


def test_archiving_clears_the_default_and_restoring_keeps_it_clear() -> None:
    assert sql(projects.ARCHIVE_PROJECT).startswith(
        "UPDATE app.projects SET is_default=0, status=N'archived', archived_at=SYSUTCDATETIME(), "
        "archived_by=?, updated_at=SYSUTCDATETIME(), updated_by=?"
    )
    restore = sql(projects.RESTORE_PROJECT)
    assert "status=N'active', archived_at=NULL, archived_by=NULL" in restore
    assert "is_default" not in restore


def test_a_membership_is_inserted_only_when_none_exists() -> None:
    assert sql(projects.ADD_MEMBER_IF_ABSENT) == (
        "INSERT INTO app.project_members (user_object_id, project_id, grant_source, granted_by) "
        "SELECT ? AS anon_1, ? AS anon_2, N'admin', ? AS anon_3 WHERE NOT (EXISTS (SELECT 1 "
        "FROM app.project_members WHERE app.project_members.user_object_id = ? "
        "AND app.project_members.project_id = ?))"
    )
    assert sql(projects.REMOVE_MEMBERSHIP) == (
        "DELETE FROM app.project_members OUTPUT deleted.grant_source "
        "WHERE app.project_members.user_object_id = ? AND app.project_members.project_id = ?"
    )


def test_the_default_mapping_statements_keep_the_filtered_index_literal() -> None:
    assert sql(projects.CLEAR_DEFAULT) == (
        "UPDATE app.project_indexes SET is_default=0, updated_at=SYSUTCDATETIME(), updated_by=? "
        "WHERE app.project_indexes.project_id = ? AND app.project_indexes.is_default = 1"
    )
    assert "d.is_default = 1" in sql(projects.ADMIN_PROJECT)
    assert sql(projects.MAPPING_OVERRIDES) == (
        "SELECT count(*) AS count_1 FROM app.user_index_overrides "
        "WHERE app.user_index_overrides.project_id = ? "
        "AND app.user_index_overrides.search_index_id = ?"
    )


def test_the_project_list_has_one_text_per_case() -> None:
    assert len(projects.PROJECT_PAGES) == 8
    full = projects.PROJECT_PAGES[(True, True, True)]
    text = sql(full)
    assert text.startswith("SELECT p.id, p.name, p.description, p.is_default, p.status, ")
    assert (
        "(SELECT count(*) AS count_1 FROM app.project_members AS mc WHERE mc.project_id = p.id) "
        "AS member_count" in text
    )
    # The page's keys come from the name indexes, then each row by its primary key.
    assert (
        "FROM (SELECT TOP (?) k.id AS id, k.name AS name FROM app.projects AS k "
        "WHERE k.status = ? AND k.name LIKE ? ESCAPE '\\' AND k.name > ? ORDER BY k.name ) "
        "AS page JOIN app.projects AS p ON p.id = page.id "
    ) in text
    assert text.endswith("ORDER BY page.name OPTION (LOOP JOIN)")
    assert binds(full) == ["limit_plus_one", "status", "prefix", "after_name"]
    plain = sql(projects.PROJECT_PAGES[(False, False, False)])
    assert "FROM app.projects AS k ORDER BY k.name ) AS page" in plain
    assert "WHERE" not in plain.split("ON s.id")[1]


def test_member_and_mapping_pages_are_keyset_pages() -> None:
    members = sql(projects.MEMBER_PAGES[True])
    assert members.endswith(
        "WHERE m.project_id = ? AND (u.display_name > ? OR u.display_name = ? AND "
        "u.object_id > ?) ORDER BY u.display_name, u.object_id"
    )
    mappings = sql(projects.MAPPING_PAGES[True])
    assert mappings.endswith(
        "WHERE pi.project_id = ? AND (pi.is_default < ? OR pi.is_default = ? AND "
        "(s.display_name > ? OR s.display_name = ? AND s.id > ?)) "
        "ORDER BY pi.is_default DESC, s.display_name, s.id"
    )
    assert "TOP" not in sql(projects.ONE_MEMBER)
    assert "TOP" not in sql(projects.ONE_MAPPING)


def test_every_request_path_function_is_registered_for_the_plan_audit() -> None:
    names = {
        "read_admin_projects",
        "read_admin_project",
        "project_exists",
        "read_project_members",
        "read_project_mappings",
        "create_project",
        "update_project",
        "add_member",
        "remove_member",
        "map_index",
        "unmap_index",
    }
    prefix = "docchat_core.sql.repositories.projects."
    for name in names:
        entry = REQUEST_PATH_REGISTRY[prefix + name]
        assert entry.function is getattr(projects, name)


# --- Reads -----------------------------------------------------------------------------------


async def test_the_project_list_binds_each_cases_values() -> None:
    engine = ScriptedEngine()
    summary = SimpleNamespace(
        id=PROJECT,
        name="Field notes",
        description="",
        is_default=0,
        status="archived",
        member_count=3,
        index_count=0,
        default_index_id=None,
        default_index_name=None,
        updated_at=STAMP,
        row_version=NEW_RV,
    )
    engine.fetched.extend([[summary], []])

    (row,) = await projects.read_admin_projects(
        engine,  # type: ignore[arg-type]
        status="archived",
        q="50%",
        after_name="Alpha",
        limit_plus_one=11,
    )
    await projects.read_admin_projects(
        engine,  # type: ignore[arg-type]
        status=None,
        q=None,
        after_name=None,
        limit_plus_one=51,
    )

    assert row == projects.AdminProjectSummaryRow(
        id=PROJECT,
        name="Field notes",
        description="",
        is_default=False,
        status="archived",
        member_count=3,
        index_count=0,
        default_index=None,
        updated_at=datetime(2026, 10, 30, 9, 15, 2, 123000, tzinfo=UTC),
        row_version=NEW_RV,
    )
    first, second = engine.calls
    assert first.statement is projects.PROJECT_PAGES[(True, True, True)]
    assert first.parameters == {
        "limit_plus_one": 11,
        "status": "archived",
        "prefix": "50\\%%",
        "after_name": "Alpha",
    }
    assert second.statement is projects.PROJECT_PAGES[(False, False, False)]
    assert second.parameters == {"limit_plus_one": 51}


async def test_one_project_reads_its_counts_default_and_actors() -> None:
    engine = ScriptedEngine()
    engine.fetched.extend([project_row(), None])

    row = await projects.read_admin_project(engine, project_id=PROJECT)  # type: ignore[arg-type]
    missing = await projects.read_admin_project(engine, project_id=PROJECT)  # type: ignore[arg-type]

    assert missing is None
    assert row is not None
    assert row.example_questions == ("What changed?",)
    assert row.default_index == projects.IndexRefRow(index_id=INDEX, display_name="Handbook")
    assert row.created_by == projects.UserRefRow(user_id=CALLER, display_name="Carol")
    # An actor without an `app.users` row still names the person, with an empty display name.
    assert row.updated_by == projects.UserRefRow(user_id=USER, display_name="")
    assert row.archived_by is None
    assert row.created_at.tzinfo is UTC
    assert engine.calls[0].name == "admin_read_project"


async def test_project_exists_reads_the_key() -> None:
    engine = ScriptedEngine()
    engine.fetched.extend([SimpleNamespace(id=PROJECT), None])

    assert await projects.project_exists(engine, project_id=PROJECT) is True  # type: ignore[arg-type]
    assert await projects.project_exists(engine, project_id=PROJECT) is False  # type: ignore[arg-type]
    assert engine.calls[0].statement is projects.PROJECT_ID


async def test_a_member_page_needs_its_project() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.PROJECT_ID, Result([]))

    rows = await projects.read_project_members(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        after=None,
        limit_plus_one=51,
    )

    assert rows is None
    assert engine.connection.statements() == [projects.PROJECT_ID]


async def test_a_member_page_follows_its_position() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.PROJECT_ID, Result([SimpleNamespace(id=PROJECT)]))
    connection.answer(
        projects.MEMBER_PAGES[True],
        Result([member_row(), member_row(grant_source="default", granted_by=None)]),
    )

    rows = await projects.read_project_members(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        after=projects.MemberPosition(display_name="", user_id=USER),
        limit_plus_one=3,
    )

    assert rows is not None
    admin, default = rows
    assert admin.granted_by == projects.UserRefRow(user_id=CALLER, display_name="Carol")
    assert default.granted_by is None
    assert connection.parameters_of(projects.MEMBER_PAGES[True]) == [
        {
            "project_id": PROJECT,
            "limit_plus_one": 3,
            "after_display_name": "",
            "after_user_id": USER,
        }
    ]
    assert engine.calls[0].retry == "transient"


async def test_a_mapping_page_follows_its_position() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.PROJECT_ID, Result([SimpleNamespace(id=PROJECT)]))
    connection.answer(projects.MAPPING_PAGES[True], Result([mapping_row(is_default=0)]))

    rows = await projects.read_project_mappings(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        after=projects.MappingPosition(is_default=True, display_name="Handbook", index_id=INDEX),
        limit_plus_one=51,
    )

    assert rows == [
        projects.ProjectIndexMappingRow(
            index_id=INDEX,
            display_name="Handbook",
            index_status="ready",
            is_default=False,
            mapped_at=datetime(2026, 10, 30, 9, 15, 2, 123000, tzinfo=UTC),
            mapped_by=projects.UserRefRow(user_id=CALLER, display_name=""),
            updated_at=datetime(2026, 10, 30, 9, 15, 2, 123000, tzinfo=UTC),
        )
    ]
    (parameters,) = connection.parameters_of(projects.MAPPING_PAGES[True])
    assert parameters is not None
    assert parameters["after_is_default"] is True


async def test_a_missing_project_has_no_mapping_page() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.PROJECT_ID, Result([]))

    rows = await projects.read_project_mappings(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        after=None,
        limit_plus_one=51,
    )

    assert rows is None


# --- adminCreateProject ----------------------------------------------------------------------


def new_project(*members: UUID, is_default: bool = False) -> projects.NewProject:
    return projects.NewProject(
        id=PROJECT,
        name="Field notes",
        description="Notes",
        instructions="Be brief.",
        example_questions=("What changed?", "Who owns it?"),
        is_default=is_default,
        initial_member_ids=members,
    )


async def test_an_unknown_initial_member_writes_nothing() -> None:
    engine = ScriptedEngine()
    known = uuid4()
    engine.connection.answer(projects.LOCK_USERS, Result([SimpleNamespace(object_id=known)]))
    unknown = uuid4()

    result = await projects.create_project(
        engine,  # type: ignore[arg-type]
        project=new_project(known, unknown, uuid4()),
        caller=CALLER,
    )

    assert result == projects.UnknownMembers(positions=(1, 2))
    assert engine.connection.statements() == [SET_XACT_ABORT_ON, projects.LOCK_USERS]
    (parameters,) = engine.connection.parameters_of(projects.LOCK_USERS)
    assert parameters is not None
    assert len(parameters["user_ids"]) == 3


async def test_a_project_is_created_with_its_members_and_audit_rows() -> None:
    engine = ScriptedEngine()
    members = (uuid4(), uuid4(), uuid4())
    connection = engine.connection
    connection.answer(
        projects.LOCK_USERS, Result([SimpleNamespace(object_id=member) for member in members])
    )
    connection.answer(projects.ADMIN_PROJECT, Result([project_row(member_count=3)]))

    result = await projects.create_project(
        engine,  # type: ignore[arg-type]
        project=new_project(*members, is_default=True),
        caller=CALLER,
    )

    assert isinstance(result, projects.ProjectWrite)
    assert result.project.member_count == 3
    (insert,) = connection.parameters_of(projects.INSERT_PROJECT)
    assert insert == {
        "project_id": PROJECT,
        "name": "Field notes",
        "description": "Notes",
        "instructions": "Be brief.",
        "example_questions_json": '["What changed?","Who owns it?"]',
        "is_default": True,
        "caller": CALLER,
    }
    assert connection.parameters_of(projects.INSERT_MEMBER) == [
        {"user_id": member, "project_id": PROJECT, "caller": CALLER} for member in members
    ]
    assert actions(connection) == ["project.created"] + ["project_member.added"] * 3
    created, *added = connection.audit_rows()
    assert created["metadata_json"] == '{"isDefault":true}'
    assert created["target_id"] == str(PROJECT)
    assert created["project_id"] == PROJECT
    assert [row["subject_object_id"] for row in added] == list(members)
    assert {row["metadata_json"] for row in added} == {'{"grantSource":"admin"}'}
    assert [event.action for event in result.events] == actions(connection)
    assert engine.calls[0].retry == "transient"


async def test_a_project_without_members_locks_no_user() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.ADMIN_PROJECT, Result([project_row()]))

    result = await projects.create_project(
        engine,  # type: ignore[arg-type]
        project=new_project(),
        caller=CALLER,
    )

    assert isinstance(result, projects.ProjectWrite)
    assert projects.LOCK_USERS not in engine.connection.statements()
    assert actions(engine.connection) == ["project.created"]


async def test_a_taken_name_is_refused() -> None:
    engine = ScriptedEngine(failure=SqlUniqueViolation(2627, "23000", "UQ_projects_name"))

    result = await projects.create_project(
        engine,  # type: ignore[arg-type]
        project=new_project(),
        caller=CALLER,
    )

    assert result == projects.Refused("project_name_taken")


async def test_another_unique_violation_is_not_a_taken_name() -> None:
    engine = ScriptedEngine(failure=SqlUniqueViolation(2627, "23000", "PK_projects"))

    with pytest.raises(SqlUniqueViolation):
        await projects.create_project(
            engine,  # type: ignore[arg-type]
            project=new_project(),
            caller=CALLER,
        )


# --- adminUpdateProject ----------------------------------------------------------------------


async def update(
    engine: ScriptedEngine, changes: projects.ProjectChanges, version: Version | None = None
) -> projects.ProjectWrite | projects.Refused:
    return await projects.update_project(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        changes=changes,
        version=version or Version(),
        caller=CALLER,
    )


async def test_an_update_of_a_missing_project_is_not_found_before_the_version() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, Result([]))
    version = Version(refuse=Required)

    result = await update(engine, projects.ProjectChanges(name="X"), version)

    assert result == projects.Refused("project_not_found")
    assert version.seen == []


@pytest.mark.parametrize(
    ("problem", "refusal"),
    [(Required, "precondition_required"), (Precondition, "precondition_failed")],
)
async def test_the_version_is_compared_with_the_locked_row(
    problem: type[DocChatProblem], refusal: str
) -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked(status="archived"))
    version = Version(refuse=problem)

    result = await update(engine, projects.ProjectChanges(name="X"), version)

    # A stale version on an archived project is a 412, not a 409.
    assert result == projects.Refused(refusal)  # type: ignore[arg-type]
    assert version.seen == [RV]
    assert engine.connection.audit_rows() == []


async def test_another_problem_from_the_version_check_is_raised() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked())

    with pytest.raises(Unrelated):
        await update(engine, projects.ProjectChanges(name="X"), Version(refuse=Unrelated))


async def test_a_status_change_holds_no_other_field() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked())

    result = await update(engine, projects.ProjectChanges(status="archived", name="X"))

    assert result == projects.Refused("status_change_with_fields")


@pytest.mark.parametrize(
    "changes",
    [
        projects.ProjectChanges(name="Renamed"),
        projects.ProjectChanges(status="archived"),
        projects.ProjectChanges(status="archived", name="Field notes"),
    ],
)
async def test_an_archived_project_takes_only_the_restore_body(
    changes: projects.ProjectChanges,
) -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked(status="archived"))

    result = await update(engine, changes)

    assert result == projects.Refused("project_archived")


async def test_an_equal_body_changes_nothing() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.ADMIN_PROJECT, Result([project_row(row_version=RV)]))

    result = await update(
        engine,
        projects.ProjectChanges(
            name="Field notes",
            example_questions=("What changed?",),
            is_default=False,
            status="active",
        ),
    )

    assert isinstance(result, projects.ProjectWrite)
    assert result.events == ()
    assert result.project.row_version == RV
    assert connection.statements() == [
        SET_XACT_ABORT_ON,
        projects.LOCK_PROJECT,
        projects.ADMIN_PROJECT,
    ]


async def test_a_field_edit_keeps_the_other_fields_and_names_what_changed() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.UPDATE_FIELDS, moved())
    connection.answer(projects.ADMIN_PROJECT, Result([project_row()]))

    result = await update(
        engine,
        projects.ProjectChanges(
            is_default=True, description="New notes", example_questions=("A?", "B?")
        ),
    )

    assert isinstance(result, projects.ProjectWrite)
    (parameters,) = connection.parameters_of(projects.UPDATE_FIELDS)
    assert parameters == {
        "project_id": PROJECT,
        "rv": RV,
        "caller": CALLER,
        "name": "Field notes",
        "description": "New notes",
        "instructions": "Be brief.",
        "example_questions_json": '["A?","B?"]',
        "is_default": True,
    }
    (row,) = connection.audit_rows()
    assert row["action"] == "project.updated"
    assert json.loads(row["metadata_json"]) == {
        "changedFields": ["description", "exampleQuestions", "isDefault"]
    }


async def test_archiving_records_whether_the_project_was_default() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked(is_default=1))
    connection.answer(projects.ARCHIVE_PROJECT, moved())
    connection.answer(projects.ADMIN_PROJECT, Result([project_row(status="archived")]))

    result = await update(engine, projects.ProjectChanges(status="archived"))

    assert isinstance(result, projects.ProjectWrite)
    (row,) = connection.audit_rows()
    assert (row["action"], row["metadata_json"]) == ("project.archived", '{"wasDefault":true}')
    assert connection.parameters_of(projects.ARCHIVE_PROJECT) == [
        {"project_id": PROJECT, "rv": RV, "caller": CALLER}
    ]


async def test_restoring_writes_project_restored_without_metadata() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked(status="archived"))
    connection.answer(projects.RESTORE_PROJECT, moved())
    connection.answer(projects.ADMIN_PROJECT, Result([project_row()]))

    result = await update(engine, projects.ProjectChanges(status="active"))

    assert isinstance(result, projects.ProjectWrite)
    (row,) = connection.audit_rows()
    assert (row["action"], row["metadata_json"]) == ("project.restored", None)


@pytest.mark.parametrize(
    ("still_there", "refusal"),
    [(True, "precondition_failed"), (False, "project_not_found")],
)
async def test_a_guarded_update_that_changed_no_row_rereads_and_rolls_back(
    still_there: bool, refusal: str
) -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.UPDATE_FIELDS, Result([]))
    connection.answer(
        projects.PROJECT_ID, Result([SimpleNamespace(id=PROJECT)] if still_there else [])
    )

    result = await update(engine, projects.ProjectChanges(name="Renamed"))

    assert result == projects.Refused(refusal)  # type: ignore[arg-type]
    assert connection.audit_rows() == []


async def test_a_rename_to_a_taken_name_is_refused() -> None:
    engine = ScriptedEngine(failure=SqlUniqueViolation(2601, "23000", "UQ_projects_name"))

    result = await update(engine, projects.ProjectChanges(name="Taken"))

    assert result == projects.Refused("project_name_taken")


# --- Members ---------------------------------------------------------------------------------


async def add(engine: ScriptedEngine, version: Version | None = None) -> Any:
    return await projects.add_member(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        user_id=USER,
        version=version or Version(),
        caller=CALLER,
    )


async def test_an_unknown_user_is_not_found_before_the_version() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked())
    engine.connection.answer(projects.LOCK_USER, Result([]))
    version = Version(refuse=Required)

    assert await add(engine, version) == projects.Refused("user_not_found")
    assert version.seen == []


async def test_a_member_is_not_added_to_an_archived_project() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked(status="archived"))
    engine.connection.answer(projects.LOCK_USER, Result([SimpleNamespace(found=1)]))

    assert await add(engine) == projects.Refused("project_archived")
    assert projects.ADD_MEMBER_IF_ABSENT not in engine.connection.statements()


async def test_a_new_member_moves_the_project_version() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.LOCK_USER, Result([SimpleNamespace(found=1)]))
    connection.answer(projects.ADD_MEMBER_IF_ABSENT, Result(rowcount=1))
    connection.answer(projects.MOVE_VERSION, moved())
    connection.answer(projects.ONE_MEMBER, Result([member_row()]))

    result = await add(engine)

    assert isinstance(result, projects.MemberChange)
    assert result.created is True
    assert result.project_row_version == NEW_RV
    assert result.member.user_id == USER
    assert actions(connection) == ["project_member.added"]
    assert connection.parameters_of(projects.MOVE_VERSION) == [
        {"project_id": PROJECT, "rv": RV, "caller": CALLER}
    ]
    order = connection.statements()
    assert order.index(projects.LOCK_PROJECT) < order.index(projects.LOCK_USER)


async def test_an_existing_member_keeps_the_version() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.LOCK_USER, Result([SimpleNamespace(found=1)]))
    connection.answer(projects.ADD_MEMBER_IF_ABSENT, Result(rowcount=0))
    connection.answer(projects.ONE_MEMBER, Result([member_row(grant_source="default")]))

    result = await add(engine)

    assert isinstance(result, projects.MemberChange)
    assert (result.created, result.project_row_version, result.events) == (False, RV, ())
    assert projects.MOVE_VERSION not in connection.statements()


async def remove(engine: ScriptedEngine) -> Any:
    return await projects.remove_member(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        user_id=USER,
        version=Version(),
        caller=CALLER,
    )


async def test_removing_a_membership_records_its_grant_source() -> None:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(projects.REMOVE_MEMBERSHIP, Result([SimpleNamespace(grant_source="default")]))
    connection.answer(projects.MOVE_VERSION, moved())

    result = await remove(engine)

    assert result == projects.PartRemoval(
        removed=True, project_row_version=NEW_RV, events=result.events
    )
    (row,) = connection.audit_rows()
    assert row["action"] == "project_member.removed"
    assert row["metadata_json"] == '{"grantSource":"default"}'
    assert row["subject_object_id"] == USER


async def test_removing_an_absent_membership_changes_nothing() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, locked())
    engine.connection.answer(projects.REMOVE_MEMBERSHIP, Result([]))

    result = await remove(engine)

    assert result == projects.PartRemoval(removed=False, project_row_version=RV, events=())
    assert projects.MOVE_VERSION not in engine.connection.statements()


async def test_removing_from_a_missing_project_is_not_found() -> None:
    engine = ScriptedEngine()
    engine.connection.answer(projects.LOCK_PROJECT, Result([]))

    assert await remove(engine) == projects.Refused("project_not_found")


# --- Mappings --------------------------------------------------------------------------------


async def map_index(
    engine: ScriptedEngine, *, is_default: bool, version: Version | None = None
) -> Any:
    return await projects.map_index(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        index_id=INDEX,
        is_default=is_default,
        version=version or Version(),
        caller=CALLER,
    )


def mapping_world(
    *, status: str | None = "ready", mappings: dict[UUID, bool] | None = None
) -> ScriptedEngine:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(
        projects.LOCK_INDEX, Result([] if status is None else [SimpleNamespace(status=status)])
    )
    connection.answer(
        projects.PROJECT_MAPPINGS,
        Result(
            [
                SimpleNamespace(search_index_id=key, is_default=int(default))
                for key, default in (mappings or {}).items()
            ]
        ),
    )
    connection.answer(projects.MOVE_VERSION, moved())
    connection.answer(projects.ONE_MAPPING, Result([mapping_row()]))
    return engine


@pytest.mark.parametrize("status", [None, "deleted"])
async def test_a_missing_or_deleted_index_is_not_found_before_the_version(
    status: str | None,
) -> None:
    engine = mapping_world(status=status)
    version = Version(refuse=Precondition)

    assert await map_index(engine, is_default=True, version=version) == projects.Refused(
        "index_not_found"
    )
    assert version.seen == []


@pytest.mark.parametrize("status", ["failed", "deleting"])
async def test_a_failed_or_deleting_index_is_not_mappable(status: str) -> None:
    engine = mapping_world(status=status)

    assert await map_index(engine, is_default=True) == projects.Refused("index_not_mappable")


async def test_a_stale_version_wins_over_an_unmappable_index() -> None:
    engine = mapping_world(status="failed")

    result = await map_index(engine, is_default=True, version=Version(refuse=Precondition))

    assert result == projects.Refused("precondition_failed")


async def test_a_first_mapping_must_be_the_default() -> None:
    engine = mapping_world(mappings={})

    assert await map_index(engine, is_default=False) == projects.Refused("default_index_required")


async def test_the_default_cant_be_unset_directly() -> None:
    engine = mapping_world(mappings={INDEX: True, OTHER_INDEX: False})

    assert await map_index(engine, is_default=False) == projects.Refused("default_index_required")


async def test_a_first_default_mapping_is_inserted_and_audited() -> None:
    engine = mapping_world(mappings={})
    connection = engine.connection

    result = await map_index(engine, is_default=True)

    assert isinstance(result, projects.MappingChange)
    assert (result.created, result.project_row_version) == (True, NEW_RV)
    assert projects.CLEAR_DEFAULT not in connection.statements()
    assert connection.parameters_of(projects.INSERT_MAPPING) == [
        {"project_id": PROJECT, "index_id": INDEX, "is_default": True, "caller": CALLER}
    ]
    (row,) = connection.audit_rows()
    assert (row["action"], row["metadata_json"]) == (
        "project_index.mapped",
        '{"isDefault":true}',
    )
    assert row["target_type"] == "project_index"
    assert row["target_id"] == str(INDEX)


async def test_a_new_default_clears_the_old_one_first() -> None:
    engine = mapping_world(mappings={OTHER_INDEX: True})
    connection = engine.connection

    result = await map_index(engine, is_default=True)

    assert isinstance(result, projects.MappingChange)
    order = connection.statements()
    assert order.index(projects.CLEAR_DEFAULT) < order.index(projects.INSERT_MAPPING)
    assert actions(connection) == ["project_index.mapped", "project_index.default_set"]
    assert json.loads(connection.audit_rows()[1]["metadata_json"]) == {
        "previousDefaultIndexId": str(OTHER_INDEX)
    }


async def test_an_existing_mapping_made_default_is_updated() -> None:
    engine = mapping_world(mappings={OTHER_INDEX: True, INDEX: False})
    connection = engine.connection

    result = await map_index(engine, is_default=True)

    assert isinstance(result, projects.MappingChange)
    assert result.created is False
    order = connection.statements()
    assert order.index(projects.CLEAR_DEFAULT) < order.index(projects.SET_DEFAULT)
    assert projects.INSERT_MAPPING not in order
    assert actions(connection) == ["project_index.default_set"]


async def test_another_mapping_joins_as_non_default() -> None:
    engine = mapping_world(mappings={OTHER_INDEX: True})
    connection = engine.connection

    result = await map_index(engine, is_default=False)

    assert isinstance(result, projects.MappingChange)
    assert projects.CLEAR_DEFAULT not in connection.statements()
    assert actions(connection) == ["project_index.mapped"]
    assert connection.audit_rows()[0]["metadata_json"] == '{"isDefault":false}'


@pytest.mark.parametrize(
    ("mappings", "is_default"),
    [({INDEX: True}, True), ({OTHER_INDEX: True, INDEX: False}, False)],
)
async def test_an_unchanged_mapping_keeps_the_version(
    mappings: dict[UUID, bool], is_default: bool
) -> None:
    engine = mapping_world(mappings=mappings)

    result = await map_index(engine, is_default=is_default)

    assert isinstance(result, projects.MappingChange)
    assert (result.created, result.project_row_version, result.events) == (False, RV, ())
    assert projects.MOVE_VERSION not in engine.connection.statements()


async def unmap(engine: ScriptedEngine) -> Any:
    return await projects.unmap_index(
        engine,  # type: ignore[arg-type]
        project_id=PROJECT,
        index_id=INDEX,
        version=Version(),
        caller=CALLER,
    )


def unmapping_world(mappings: dict[UUID, bool], overrides: int = 0) -> ScriptedEngine:
    engine = ScriptedEngine()
    connection = engine.connection
    connection.answer(projects.LOCK_PROJECT, locked())
    connection.answer(
        projects.PROJECT_MAPPINGS,
        Result(
            [
                SimpleNamespace(search_index_id=key, is_default=int(default))
                for key, default in mappings.items()
            ]
        ),
    )
    connection.answer(projects.MAPPING_OVERRIDES, Result([(overrides,)]))
    connection.answer(projects.MOVE_VERSION, moved())
    return engine


async def test_an_absent_mapping_is_not_removed() -> None:
    engine = unmapping_world({OTHER_INDEX: True})

    assert await unmap(engine) == projects.PartRemoval(
        removed=False, project_row_version=RV, events=()
    )
    assert projects.DELETE_MAPPING not in engine.connection.statements()


async def test_the_default_stays_while_other_mappings_exist() -> None:
    engine = unmapping_world({INDEX: True, OTHER_INDEX: False})

    assert await unmap(engine) == projects.Refused("default_index_in_use")


async def test_the_only_mapping_can_always_go() -> None:
    engine = unmapping_world({INDEX: True}, overrides=2)
    connection = engine.connection

    result = await unmap(engine)

    assert isinstance(result, projects.PartRemoval)
    assert (result.removed, result.project_row_version) == (True, NEW_RV)
    order = connection.statements()
    assert order.index(projects.MAPPING_OVERRIDES) < order.index(projects.DELETE_MAPPING)
    (row,) = connection.audit_rows()
    assert row["action"] == "project_index.unmapped"
    assert json.loads(row["metadata_json"]) == {"wasDefault": True, "overridesRemoved": 2}


async def test_a_non_default_mapping_is_removed() -> None:
    engine = unmapping_world({INDEX: False, OTHER_INDEX: True})

    result = await unmap(engine)

    assert isinstance(result, projects.PartRemoval)
    assert result.removed is True
    assert json.loads(engine.connection.audit_rows()[0]["metadata_json"]) == {
        "wasDefault": False,
        "overridesRemoved": 0,
    }


async def test_unmapping_from_an_archived_project_is_refused() -> None:
    engine = unmapping_world({INDEX: True})
    engine.connection = ScriptedConnection().answer(
        projects.LOCK_PROJECT, locked(status="archived")
    )

    assert await unmap(engine) == projects.Refused("project_archived")
