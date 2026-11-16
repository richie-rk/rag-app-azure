"""Unit tests of the access and configuration tables (`80-data-model.md` sections 2.2 to 2.8,
2.21 and 2.22; T-078 unit part)."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

from docchat_core.audit.catalogue import AUDIT_ACTIONS, AUDIT_TARGET_TYPES
from docchat_core.sql.tables.access import (
    ACCESS_GRANTS,
    ACCESS_TABLES,
    audit_events,
    project_indexes,
    projects,
    user_index_overrides,
)
from docchat_core.sql.tables.schema import RoleRights

LETTERS = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
ROLES = (
    "docchat_api",
    "docchat_ingestion",
    "docchat_config_operator",
    "docchat_conversation_reader",
    "docchat_user_data_operator",
)

# SQLAlchemy's dialect classes have untyped constructors.
DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


def spec_text() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("80-data-model.md")


def spec_matrix() -> dict[str, dict[str, set[str]]]:
    """The grant matrix of section 2.22 as table → role → verbs (`U(cols)` kept as is)."""
    lines = spec_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Table (owning area) |"))
    matrix: dict[str, dict[str, set[str]]] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        table = cells[0].split("`")[1]
        matrix[table] = {}
        for role, cell in zip(ROLES, cells[1:], strict=True):
            tokens = re.sub(r"[¹²]", "", cell).split()
            matrix[table][role] = {
                "U(cols)" if token == "U(cols)" else LETTERS[token]
                for token in tokens
                if token != "\N{EN DASH}"
            }
    return matrix


def as_letters(cell: RoleRights) -> set[str]:
    verbs: set[str] = set(cell.verbs)
    if cell.update_columns:
        verbs.add("U(cols)")
    return verbs


def check(table_name: str, constraint_name: str) -> str:
    for table in ACCESS_TABLES:
        if table.name == table_name:
            for constraint in table.constraints:
                if constraint.name == constraint_name and isinstance(constraint, CheckConstraint):
                    return str(constraint.sqltext)
    raise KeyError(constraint_name)


def test_seven_access_tables() -> None:
    assert [table.name for table in ACCESS_TABLES] == [
        "users",
        "projects",
        "project_members",
        "project_indexes",
        "user_index_overrides",
        "ops_config",
        "audit_events",
    ]


def test_t078_audit_constraints_list_the_catalogue() -> None:
    action_check = check("audit_events", "CK_audit_events_action")
    listed = re.findall(r"N'([^']+)'", action_check)
    assert tuple(listed) == AUDIT_ACTIONS
    assert len(listed) == 43
    target_check = check("audit_events", "CK_audit_events_target_type")
    assert tuple(re.findall(r"N'([^']+)'", target_check)) == AUDIT_TARGET_TYPES
    assert len(AUDIT_TARGET_TYPES) == 18
    actor_check = check("audit_events", "CK_audit_events_actor_type")
    assert re.findall(r"N'([^']+)'", actor_check) == ["user", "operator", "system"]


def test_ops_config_key_check() -> None:
    assert check("ops_config", "CK_ops_config_key") == (
        "config_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_.]%' "
        "AND CHARINDEX(N'.', config_key) > 1 AND RIGHT(config_key, 1) <> N'.'"
    )


def test_cascades_are_the_three_of_the_access_tables() -> None:
    cascading = {
        constraint.name
        for table in ACCESS_TABLES
        for constraint in table.foreign_key_constraints
        if constraint.ondelete == "CASCADE"
    }
    assert cascading == {
        "FK_project_members_users",
        "FK_user_index_overrides_users",
        "FK_user_index_overrides_mapping",
    }


def test_access_grants_equal_the_matrix() -> None:
    expected = spec_matrix()
    for table in ACCESS_TABLES:
        row = ACCESS_GRANTS[table.name]
        for role in ROLES:
            cell = row.get(role)  # type: ignore[call-overload]
            assert (as_letters(cell) if cell else set()) == expected[table.name][role], (
                table.name,
                role,
            )


def test_audit_events_are_append_only_for_every_role() -> None:
    for cell in ACCESS_GRANTS["audit_events"].values():
        assert not {"UPDATE", "DELETE"} & cell.verbs


def test_ddl_compiles_for_sql_server() -> None:
    dialect = DIALECT
    projects_ddl = str(CreateTable(projects).compile(dialect=dialect))
    assert "row_version ROWVERSION NOT NULL" in projects_ddl
    assert "CONSTRAINT [PK_projects] PRIMARY KEY CLUSTERED (id)" in projects_ddl
    assert "example_questions_json NVARCHAR(max) NOT NULL DEFAULT N'[]'" in projects_ddl
    audit_ddl = str(CreateTable(audit_events).compile(dialect=dialect))
    assert "id BIGINT NOT NULL IDENTITY(1,1)" in audit_ddl
    default_index = next(
        i for i in project_indexes.indexes if i.name == "UX_project_indexes_default"
    )
    assert str(CreateIndex(default_index).compile(dialect=dialect)) == (
        "CREATE UNIQUE INDEX [UX_project_indexes_default] ON app.project_indexes (project_id) "
        "WHERE is_default = 1"
    )
    status_name = next(i for i in projects.indexes if i.name == "IX_projects_status_name")
    assert str(CreateIndex(status_name).compile(dialect=dialect)) == (
        "CREATE INDEX [IX_projects_status_name] ON app.projects (status, name) "
        "INCLUDE (description, example_questions_json)"
    )
    subject = next(i for i in audit_events.indexes if i.name == "IX_audit_events_subject")
    assert str(CreateIndex(subject).compile(dialect=dialect)).endswith(
        "(subject_object_id, occurred_at) WHERE subject_object_id IS NOT NULL"
    )
    overrides_ddl = str(CreateTable(user_index_overrides).compile(dialect=dialect))
    assert (
        "CONSTRAINT [FK_user_index_overrides_mapping] FOREIGN KEY(project_id, search_index_id) "
        "REFERENCES app.project_indexes (project_id, search_index_id) ON DELETE CASCADE"
    ) in overrides_ddl


def test_audit_inserts_never_read_the_identity_back() -> None:
    # The writing roles hold INSERT only, and an OUTPUT clause needs SELECT on what it returns.
    assert audit_events.implicit_returning is False
    compiled = str(audit_events.insert().values(action="x").compile(dialect=DIALECT))
    assert "OUTPUT" not in compiled
