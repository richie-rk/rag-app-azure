"""`override list`, `override set` and `override clear` (T-541; `86-operations.md` section 2.8.3
and runbook R04; `80-data-model.md` sections 2.6 and 2.21).

Every command runs through the real parser against the scripted SQL layer and the faked Azure CLI
of `conftest.py`. The behaviour against SQL Server under `docchat_config_operator` is the operator
CLI integration test's (`scripts/ops/tests/integration/commands/test_override.py`).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from sqlalchemy import Update
from sqlalchemy.sql.dml import Delete, Insert

from docchat_core.sql.engine import SqlDeadlock, SqlError, SqlUniqueViolation
from docchat_core.sql.tables.access import user_index_overrides
from docchat_ops.commands import override
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

REASON = "test the rebuilt index against live questions"
TICKET = ("--ticket", "CHG-3107", "--reason", REASON)
USER = UUID("6f1c2a4e-8b3d-4e5f-9a7b-1c2d3e4f5a60")
OTHER_USER = UUID("0d5e6f7a-8b9c-4d1e-8f3a-4b5c6d7e8f64")
PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
OLD_INDEX = UUID("9c4d5e6f-7a8b-4c0d-9e2f-3a4b5c6d7e63")
SETTER = UUID("1e6f7a8b-9c0d-4e2f-9a4b-5c6d7e8f9a65")
# DATETIME2 holds UTC without a zone; SYSUTCDATETIME() carries more than milliseconds.
DB_NOW = datetime(2026, 10, 30, 12, 0, 0, 123456)  # noqa: DTZ001
SET_AT = datetime(2026, 10, 30, 12, 0, 0, 123000)  # noqa: DTZ001
EXPIRES = datetime(2026, 11, 2, 9, 30, 0, 250000)  # noqa: DTZ001
EXPIRED = datetime(2026, 10, 1, 9, 30)  # noqa: DTZ001


def _target(user: UUID = USER, project: UUID = PROJECT) -> tuple[str, ...]:
    return ("--user", str(user), "--project", str(project))


def _set(ops: OpsHarness, *extra: str, environment: str = "dev", yes: bool = True) -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "override",
        "set",
        *_target(),
        "--index",
        str(INDEX),
        *TICKET,
        *(("--yes",) if yes else ()),
        *extra,
    )


def _clear(ops: OpsHarness, *, environment: str = "dev", yes: bool = True) -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "override",
        "clear",
        *_target(),
        *TICKET,
        *(("--yes",) if yes else ()),
    )


def _current(
    index: UUID = OLD_INDEX, expires: datetime = EXPIRES, expired: int = 0
) -> dict[str, Any]:
    return {"search_index_id": index, "expires_at": expires, "expired": expired}


def _script_set_read(
    ops: OpsHarness,
    *,
    user: bool = True,
    mapped: bool = True,
    current: dict[str, Any] | None = None,
) -> None:
    database = ops.database
    database.answer(*([{"object_id": USER}] if user else []))
    database.answer(*([{"search_index_id": INDEX}] if mapped else []))
    database.answer(*([current] if current else []))


def _audit(ops: OpsHarness) -> dict[str, Any]:
    (row,) = ops.database.audit_rows
    return row


def _metadata(ops: OpsHarness) -> dict[str, Any]:
    metadata: dict[str, Any] = json.loads(_audit(ops)["metadata_json"])
    return metadata


def _statements(ops: OpsHarness, kind: type[Insert | Update | Delete]) -> list[Any]:
    return [
        statement
        for statement, _ in ops.database.executed
        if isinstance(statement, kind) and statement.table is user_index_overrides
    ]


def _units(ops: OpsHarness) -> list[str]:
    return [name for name, _ in ops.database.units]


# --- Arguments ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("7", 7), ("30", 30)])
def test_days_is_1_to_30(value: str, expected: int) -> None:
    assert override.days(value) == expected


@pytest.mark.parametrize("value", ["0", "31", "-1", "seven", "1.5", ""])
def test_days_outside_1_to_30_is_refused(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        override.days(value)


@pytest.mark.parametrize(
    "argv",
    [
        ("set", *_target(), "--index", str(INDEX), "--days", "31", *TICKET),
        ("set", *_target(), "--index", str(INDEX), "--days", "0", *TICKET),
        ("set", *_target(), *TICKET),
        ("set", "--user", "not-a-uuid", "--project", str(PROJECT), "--index", str(INDEX), *TICKET),
        ("clear", "--user", str(USER), *TICKET),
        ("list", "--project", "x", *TICKET),
        ("list",),
    ],
)
def test_invalid_arguments_exit_2_before_any_sign_in(
    ops: OpsHarness, argv: tuple[str, ...]
) -> None:
    outcome = ops.run("--env", "dev", "override", *argv)
    assert outcome.code == ExitCode.USAGE
    assert ops.az.calls == []
    assert ops.credentials_made == 0


# --- list -----------------------------------------------------------------------------------------


def _list_rows() -> list[dict[str, Any]]:
    return [
        {
            "user_object_id": USER,
            "project_id": PROJECT,
            "search_index_id": INDEX,
            "set_by": SETTER,
            "set_at": SET_AT,
            "expires_at": EXPIRES,
            "expired": 0,
        },
        {
            "user_object_id": OTHER_USER,
            "project_id": PROJECT,
            "search_index_id": OLD_INDEX,
            "set_by": SETTER,
            "set_at": EXPIRED - timedelta(days=7),
            "expires_at": EXPIRED,
            "expired": 1,
        },
    ]


def test_list_prints_every_override_with_its_expired_flag_and_no_reason(ops: OpsHarness) -> None:
    ops.database.answer(*_list_rows())
    outcome = ops.run("--env", "dev", "--json", "override", "list", *TICKET)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["command"] == "override list"
    assert report["overrides"] == [
        {
            "userObjectId": str(USER),
            "projectId": str(PROJECT),
            "indexId": str(INDEX),
            "setBy": str(SETTER),
            "setAt": "2026-10-30T12:00:00.123Z",
            "expiresAt": "2026-11-02T09:30:00.250Z",
            "expired": False,
        },
        {
            "userObjectId": str(OTHER_USER),
            "projectId": str(PROJECT),
            "indexId": str(OLD_INDEX),
            "setBy": str(SETTER),
            "setAt": "2026-09-24T09:30:00.000Z",
            "expiresAt": "2026-10-01T09:30:00.000Z",
            "expired": True,
        },
    ]
    (statement,) = ops.database.sql("override_list")
    assert "reason" not in statement
    assert "CASE WHEN (app.user_index_overrides.expires_at > SYSUTCDATETIME()) THEN 0 ELSE 1" in (
        statement
    )
    assert "WHERE" not in statement
    assert statement.endswith(
        "ORDER BY app.user_index_overrides.user_object_id, app.user_index_overrides.project_id"
    )
    # A read: no audit row, no sign-in check beyond SQL's own, no confirmation.
    assert ops.database.audit_rows == []
    assert ("override_list", "transient") in ops.database.units
    assert ops.database.disposed


@pytest.mark.parametrize(
    ("filters", "columns"),
    [
        (("--user", str(USER)), ["user_object_id"]),
        (("--project", str(PROJECT)), ["project_id"]),
        (_target(), ["user_object_id", "project_id"]),
    ],
)
def test_list_filters_by_user_and_project(
    ops: OpsHarness, filters: tuple[str, ...], columns: list[str]
) -> None:
    ops.database.answer()
    outcome = ops.run("--env", "dev", "--json", "override", "list", *filters, *TICKET)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["overrides"] == []
    (statement,) = ops.database.sql("override_list")
    where = statement.split("WHERE ")[1]
    assert [
        column for column in ("user_object_id", "project_id") if f".{column} = " in where
    ] == columns


def test_list_prints_a_table_without_json(ops: OpsHarness) -> None:
    ops.database.answer(*_list_rows())
    outcome = ops.run("--env", "dev", "override", "list", *TICKET)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    lines = outcome.stdout.splitlines()
    assert lines[0] == "result: listed"
    assert lines[1].split() == list(override.LIST_COLUMNS)
    assert lines[2].split()[-1] == "false"
    assert lines[3].split()[-1] == "true"


def test_list_accepts_a_service_principal(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    ops.database.answer()
    outcome = ops.run("--env", "prod", "override", "list", *TICKET)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr


# --- set ------------------------------------------------------------------------------------------


def test_set_inserts_a_new_override_with_its_audit_row(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.database.answer({"now": DB_NOW})
    ops.database.answer(rowcount=0)  # the update finds no row
    outcome = _set(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not ops.database.script
    assert _units(ops) == ["ops_connection_check", "override_read", "override_set"]
    assert ("override_set", "none") in ops.database.units
    read_user, read_mapping, read_current = ops.database.sql("override_read")
    assert read_user.startswith("SELECT app.users.object_id")
    assert "app.project_indexes.project_id = " in read_mapping
    assert "app.project_indexes.search_index_id = " in read_mapping
    assert "reason" not in read_current
    now, update, insert, audit = ops.database.sql("override_set")
    assert now == "SELECT SYSUTCDATETIME() AS now"
    assert update.startswith("UPDATE app.user_index_overrides WITH (UPDLOCK, HOLDLOCK) SET ")
    assert "WHERE app.user_index_overrides.user_object_id = " in update
    assert insert.startswith("INSERT INTO app.user_index_overrides ")
    assert audit.startswith("INSERT INTO app.audit_events")
    (inserted,) = _statements(ops, Insert)
    values = inserted.compile().params
    assert values == {
        "user_object_id": USER,
        "project_id": PROJECT,
        "search_index_id": INDEX,
        "reason": REASON,
        "set_by": UUID(ops.operator_oid),
        "set_at": SET_AT,
        "expires_at": SET_AT + timedelta(days=7),
    }
    audit_row = _audit(ops)
    assert audit_row["action"] == "user_index_override.set"
    assert audit_row["actor_type"] == "operator"
    assert str(audit_row["actor_object_id"]) == ops.operator_oid
    assert audit_row["subject_object_id"] == USER
    assert audit_row["target_type"] == "user_index_override"
    assert audit_row["target_id"] == str(USER)
    assert audit_row["project_id"] == PROJECT
    assert audit_row["correlation_id"] == outcome.correlation_id
    assert _metadata(ops) == {
        "searchIndexId": str(INDEX),
        "expiresAt": "2026-11-06T12:00:00.123Z",
        "replaced": False,
        "ticket": "CHG-3107",
    }
    report = outcome.json()
    assert report["result"] == "set"
    assert report["expiresAt"] == "2026-11-06T12:00:00.123Z"
    assert report["replaced"] is False
    assert report["indexId"] == str(INDEX)
    assert override.NEXT_REQUEST in outcome.stderr


def test_set_replaces_an_existing_override_under_the_key_range_lock(ops: OpsHarness) -> None:
    _script_set_read(ops, current=_current(expired=1, expires=EXPIRED))
    ops.database.answer({"now": DB_NOW})
    ops.database.answer(rowcount=1)  # the update replaced the row
    outcome = _set(ops, "--days", "30")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert _statements(ops, Insert) == []
    (updated,) = _statements(ops, Update)
    values = updated.compile().params
    assert values["search_index_id"] == INDEX
    assert values["reason"] == REASON
    assert values["set_at"] == SET_AT
    # Exactly 30 days: `CK_user_index_overrides_expiry` allows `DATEADD(DAY, 30, set_at)`.
    assert values["expires_at"] - values["set_at"] == timedelta(days=30)
    assert _metadata(ops)["replaced"] is True
    assert _metadata(ops)["expiresAt"] == "2026-11-29T12:00:00.123Z"
    assert outcome.json()["replaced"] is True
    assert (
        f"current override: index {OLD_INDEX}, expires at 2026-10-01T09:30:00.000Z (expired)"
        in outcome.stderr
    )
    assert "it replaces the current override" in outcome.stderr


def test_set_prints_a_summary_of_identifiers_only(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.answer("yes\n")
    ops.database.answer({"now": DB_NOW})
    ops.database.answer(rowcount=0)
    outcome = _set(ops, yes=False)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    summary = outcome.stderr.split("Type yes to continue")[0]
    assert f"user: {USER}" in summary
    assert f"project: {PROJECT}" in summary
    assert f"index: {INDEX}" in summary
    assert "current override: none" in summary
    assert "expires 7 days after the change" in summary
    # The reason never leaves the row.
    assert REASON not in outcome.stdout + outcome.stderr
    assert REASON not in _audit(ops)["metadata_json"]


def test_set_without_a_users_row_exits_4_with_nothing_written(ops: OpsHarness) -> None:
    _script_set_read(ops, user=False)
    outcome = _set(ops)
    assert outcome.code == ExitCode.NOT_FOUND
    assert "no users row" in outcome.failure
    assert "signs in" in outcome.failure
    assert "override_set" not in _units(ops)
    assert ops.database.audit_rows == []


def test_set_of_an_index_not_mapped_to_the_project_exits_2(ops: OpsHarness) -> None:
    _script_set_read(ops, mapped=False)
    outcome = _set(ops)
    assert outcome.code == ExitCode.USAGE
    assert "isn't mapped to project" in outcome.failure
    assert "Projects screen" in outcome.failure
    assert "override_set" not in _units(ops)
    assert ops.database.audit_rows == []


def test_set_with_a_reason_padded_to_10_characters_exits_2_before_sign_in(
    ops: OpsHarness,
) -> None:
    outcome = ops.run(
        "--env",
        "dev",
        "override",
        "set",
        *_target(),
        "--index",
        str(INDEX),
        "--ticket",
        "CHG-3107",
        "--reason",
        "too short   ",
    )
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0
    assert ops.az.calls == []


def test_set_when_the_user_or_mapping_went_away_exits_6(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.database.answer({"now": DB_NOW})
    ops.database.answer_error(SqlError(547, "23000"))
    outcome = _set(ops)
    assert outcome.code == ExitCode.CONFLICT
    assert "changed since it was read" in outcome.failure
    assert ops.database.audit_rows == []


@pytest.mark.parametrize(
    "error",
    [SqlUniqueViolation(2627, "23000", "PK_user_index_overrides"), SqlDeadlock(1205, "40001")],
)
def test_set_racing_another_set_exits_6(ops: OpsHarness, error: SqlError) -> None:
    _script_set_read(ops)
    ops.database.unit_errors["override_set"] = error
    assert _set(ops).code == ExitCode.CONFLICT


def test_set_with_another_sql_error_exits_1(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.database.unit_errors["override_set"] = SqlError(8152, "22001")
    outcome = _set(ops)
    assert outcome.code == ExitCode.UNEXPECTED
    assert "SQL error 8152" in outcome.failure


def test_set_in_prod_needs_the_typed_environment_name(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.answer("yes\n", terminal=True)
    outcome = _set(ops, environment="prod")
    assert outcome.code == ExitCode.USAGE
    assert "Type the environment name to continue" in outcome.stderr
    assert "override_set" not in _units(ops)


def test_a_declined_set_writes_nothing(ops: OpsHarness) -> None:
    _script_set_read(ops)
    ops.answer("no\n")
    outcome = _set(ops, yes=False)
    assert outcome.code == ExitCode.USAGE
    assert "override_set" not in _units(ops)
    assert ops.database.audit_rows == []


# --- clear ----------------------------------------------------------------------------------------


def test_clear_deletes_the_override_it_read_with_its_audit_row(ops: OpsHarness) -> None:
    ops.database.answer(_current(index=INDEX))
    ops.database.answer(rowcount=1)
    outcome = _clear(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not ops.database.script
    deleted, audit = ops.database.sql("override_clear")
    assert deleted.startswith("DELETE FROM app.user_index_overrides WHERE ")
    assert audit.startswith("INSERT INTO app.audit_events")
    (statement,) = _statements(ops, Delete)
    assert statement.compile().params == {
        "user_object_id_1": USER,
        "project_id_1": PROJECT,
        "search_index_id_1": INDEX,
        "expires_at_1": EXPIRES,
    }
    audit_row = _audit(ops)
    assert audit_row["action"] == "user_index_override.cleared"
    assert audit_row["subject_object_id"] == USER
    assert audit_row["target_type"] == "user_index_override"
    assert audit_row["target_id"] == str(USER)
    assert audit_row["project_id"] == PROJECT
    assert _metadata(ops) == {"searchIndexId": str(INDEX), "ticket": "CHG-3107"}
    assert outcome.json()["result"] == "cleared"
    assert outcome.json()["indexId"] == str(INDEX)
    assert ("override_clear", "none") in ops.database.units


def test_clear_of_an_expired_override_removes_it_too(ops: OpsHarness) -> None:
    ops.database.answer(_current(expires=EXPIRED, expired=1))
    ops.database.answer(rowcount=1)
    outcome = _clear(ops)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "(expired)" in outcome.stderr


def test_clear_without_an_override_exits_4(ops: OpsHarness) -> None:
    ops.database.answer()
    outcome = _clear(ops)
    assert outcome.code == ExitCode.NOT_FOUND
    assert "no override for user" in outcome.failure
    assert "override_clear" not in _units(ops)


def test_clear_of_an_override_changed_meanwhile_exits_6(ops: OpsHarness) -> None:
    ops.database.answer(_current())
    ops.database.answer(rowcount=0)
    outcome = _clear(ops)
    assert outcome.code == ExitCode.CONFLICT
    assert "changed by someone else" in outcome.failure
    assert ops.database.audit_rows == []


def test_a_declined_clear_writes_nothing(ops: OpsHarness) -> None:
    ops.database.answer(_current())
    ops.answer("n\n")
    outcome = _clear(ops, yes=False)
    assert outcome.code == ExitCode.USAGE
    assert "override_clear" not in _units(ops)


def test_unit_names_are_statement_set_names() -> None:
    for name in (override.LIST_UNIT, override.READ_UNIT, override.SET_UNIT, override.CLEAR_UNIT):
        assert name.replace("_", "").isalpha()
        assert name == name.lower()
