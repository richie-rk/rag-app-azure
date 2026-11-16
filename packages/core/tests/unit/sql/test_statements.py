"""Unit tests of the raw statements (`80-data-model.md` section 2.22; ADR 0125)."""

from __future__ import annotations

import pytest
from sqlalchemy.sql.elements import TextClause

from docchat_core.sql import statements

SID = "0123456789abcdef0123456789abcdef"


def test_create_user() -> None:
    statement = statements.create_user("id-docchat-api-dev", SID, "E")
    assert isinstance(statement, TextClause)
    assert statement.text == f"CREATE USER [id-docchat-api-dev] WITH SID = 0x{SID}, TYPE = E"
    group = statements.create_user("sg-docchat-prod-config-operators", SID, "X")
    assert group.text.endswith("TYPE = X")


@pytest.mark.parametrize(
    "name",
    [
        "id-docchat-api-test",
        "id-docchat-web-dev",
        "sg-docchat-dev-sql-admins",
        "x]; DROP TABLE app.users; --",
        "",
    ],
)
def test_only_the_five_users(name: str) -> None:
    with pytest.raises(ValueError, match="contained users"):
        statements.create_user(name, SID, "E")
    with pytest.raises(ValueError, match="contained users"):
        statements.drop_user(name)
    with pytest.raises(ValueError, match="contained users"):
        statements.add_role_member("docchat_api", name)


@pytest.mark.parametrize("sid", ["0x0123", SID.upper(), SID + "00", "z" * 32])
def test_sid_is_32_lower_case_hex(sid: str) -> None:
    with pytest.raises(ValueError, match="SID"):
        statements.create_user("id-docchat-api-dev", sid, "E")


def test_user_type() -> None:
    with pytest.raises(ValueError, match="type"):
        statements.create_user("id-docchat-api-dev", SID, "S")  # type: ignore[arg-type]


def test_role_membership() -> None:
    added = statements.add_role_member("docchat_ingestion", "id-docchat-ingest-load")
    assert added.text == "ALTER ROLE docchat_ingestion ADD MEMBER [id-docchat-ingest-load]"
    dropped = statements.drop_role_member("docchat_api", "stray]user")
    assert dropped.text == "ALTER ROLE docchat_api DROP MEMBER [stray]]user]"
    assert statements.drop_user("sg-docchat-dev-user-data-operators").text == (
        "DROP USER [sg-docchat-dev-user-data-operators]"
    )


@pytest.mark.parametrize("role", ["db_owner", "public", "docchat_api; DROP"])
def test_only_the_five_roles(role: str) -> None:
    with pytest.raises(ValueError, match="roles"):
        statements.add_role_member(role, "id-docchat-api-dev")
    with pytest.raises(ValueError, match="roles"):
        statements.drop_role_member(role, "someone")


@pytest.mark.parametrize("name", ["", "a" * 129, "line\nbreak"])
def test_dropped_member_names_are_printable(name: str) -> None:
    with pytest.raises(ValueError, match="principal"):
        statements.drop_role_member("docchat_api", name)


def test_catalogue_queries_bind_nothing() -> None:
    for statement in (
        statements.LOGIN_PROBE,
        statements.DATABASE_PRINCIPALS,
        statements.ROLE_MEMBERS,
        statements.DATABASE_PERMISSIONS,
    ):
        assert isinstance(statement, TextClause)
        assert statement._bindparams == {}
    assert "sys.database_permissions" in statements.DATABASE_PERMISSIONS.text
    assert "minor_id" in statements.DATABASE_PERMISSIONS.text
    assert "sys.database_role_members" in statements.ROLE_MEMBERS.text


def test_xact_abort_is_a_constant_session_option() -> None:
    assert isinstance(statements.SET_XACT_ABORT_ON, TextClause)
    assert statements.SET_XACT_ABORT_ON.text == "SET XACT_ABORT ON"
    assert statements.SET_XACT_ABORT_ON._bindparams == {}


def test_isolation_levels_are_constant_session_options() -> None:
    assert statements.SET_SNAPSHOT_ISOLATION.text == "SET TRANSACTION ISOLATION LEVEL SNAPSHOT"
    assert statements.SET_READ_COMMITTED_ISOLATION.text == (
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    )
    for statement in (statements.SET_SNAPSHOT_ISOLATION, statements.SET_READ_COMMITTED_ISOLATION):
        assert isinstance(statement, TextClause)
        assert statement._bindparams == {}
