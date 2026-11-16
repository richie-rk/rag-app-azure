from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Table, insert
from sqlalchemy.dialects import mssql
from sqlalchemy.dialects.mssql import ROWVERSION

from docchat_core.audit.catalogue import (
    ACTIONS_WITHOUT_METADATA,
    AUDIT_ACTIONS,
    AUDIT_ACTOR_TYPES,
    AUDIT_TARGET_TYPES,
)
from docchat_core.sql.tables.access import (
    audit_events,
    ops_config,
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_testing.builders.access import (
    OVERRIDE_LIFETIME,
    audit_event_row,
    ops_config_row,
    project_index_row,
    project_member_row,
    project_row,
    user_index_override_row,
    user_row,
)
from docchat_testing.builders.canaries import is_canary
from docchat_testing.tokens import SESSION_AUTH


def assert_insertable(table: Table, row: dict[str, Any]) -> None:
    """Every column the server can't fill is given, and the insert compiles for SQL Server."""
    required = {
        column.name
        for column in table.columns
        if not column.nullable
        and column.server_default is None
        and column.identity is None
        and not isinstance(column.type, ROWVERSION)
    }
    assert required <= set(row), required - set(row)
    insert(table).values(**row).compile(dialect=mssql.dialect())  # type: ignore[no-untyped-call]


def test_user_row() -> None:
    row = user_row()
    assert_insertable(users, row)
    assert row["tenant_id"] == SESSION_AUTH.tenant_id
    assert row["last_known_role"] in ("Admin", "User")
    assert is_canary(row["display_name"])
    assert is_canary(row["email"])
    assert user_row()["object_id"] != row["object_id"]
    assert user_row(last_known_role="Admin")["last_known_role"] == "Admin"


def test_project_row() -> None:
    row = project_row()
    assert_insertable(projects, row)
    assert 1 <= len(row["name"]) <= 80
    assert all(is_canary(row[name]) for name in ("name", "description", "instructions"))


def test_membership_and_mapping_rows() -> None:
    user, project, index = uuid4(), uuid4(), uuid4()
    member = project_member_row(user, project)
    assert_insertable(project_members, member)
    assert member["grant_source"] == "admin"
    assert member["granted_by"] is not None
    default = project_member_row(user, project, grant_source="default", granted_by=None)
    assert default["granted_by"] is None
    mapping = project_index_row(project, index)
    assert_insertable(project_indexes, mapping)
    assert mapping["is_default"] is False


def test_user_index_override_row_is_inside_the_30_day_limit() -> None:
    row = user_index_override_row(uuid4(), uuid4(), uuid4())
    assert_insertable(user_index_overrides, row)
    assert row["expires_at"] - row["set_at"] == OVERRIDE_LIFETIME
    assert len(row["reason"]) >= 10


def test_ops_config_row() -> None:
    value = json.dumps({"value": 3})
    row = ops_config_row("api.example_key", value)
    assert_insertable(ops_config, row)
    assert row["value_json"] == row["default_json"] == value


def test_audit_event_row_follows_the_catalogue() -> None:
    row = audit_event_row()
    assert_insertable(audit_events, row)
    assert row["actor_type"] in AUDIT_ACTOR_TYPES
    assert row["action"] in AUDIT_ACTIONS
    assert row["action"] in ACTIONS_WITHOUT_METADATA
    assert row["target_type"] in AUDIT_TARGET_TYPES
    assert row["target_id"] == str(row["project_id"])


def test_overrides_are_checked_against_the_table() -> None:
    with pytest.raises(KeyError):
        project_row(nam="x")
