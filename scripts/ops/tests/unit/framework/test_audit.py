"""Correlation and audit rows of an operator run (`86-operations.md` section 2.5;
`80-data-model.md` section 2.21)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from sqlalchemy.sql.selectable import Select

from docchat_core.audit import current_correlation_id
from docchat_core.audit.models import TestUsersRotatedMetadata
from docchat_core.sql.engine import SqlEngine
from docchat_ops.framework import audit
from docchat_ops.framework.audit import (
    connect,
    correlation_scope,
    new_correlation_id,
    operator_event,
    ops_database,
    write_event,
)
from docchat_ops.framework.endpoints import SqlTarget
from docchat_ops.framework.exitcodes import AccessDenied

if TYPE_CHECKING:
    from ops.tests.unit.conftest import FakeCredential, FakeDatabase

ACTOR = "11111111-1111-4111-8111-111111111111"
SERVER = "sql-docchat-dev-abc123.database.windows.net"


def test_correlation_id_is_32_lower_case_hex() -> None:
    for _ in range(50):
        assert re.fullmatch(r"[0-9a-f]{32}", new_correlation_id())


def test_an_all_zero_draw_is_redrawn() -> None:
    draws = iter(["0" * 32, "ab" * 16])

    def draw(size: int) -> str:
        assert size == 16
        return next(draws)

    assert new_correlation_id(draw) == "ab" * 16


def test_correlation_scope_makes_the_run_trace_current() -> None:
    assert current_correlation_id() is None
    with correlation_scope("0af7651916cd43dd8448eb211c80319c"):
        assert current_correlation_id() == "0af7651916cd43dd8448eb211c80319c"
    assert current_correlation_id() is None


def test_ops_database_connects_as_docchat_ops(
    fake_credential: FakeCredential, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator's shell settings change nothing: never the compose services.
    monkeypatch.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    monkeypatch.setenv("DOCCHAT_SQL_THREADS", "1")
    engine = ops_database(SqlTarget(SERVER, "docchat"), fake_credential)
    assert isinstance(engine, SqlEngine)
    odbc: str = engine._odbc
    assert f"Server=tcp:{SERVER},1433" in odbc
    assert "Encrypt=strict" in odbc
    assert odbc.endswith("APP=docchat-ops")
    assert engine._credential is fake_credential


async def test_connect_returns_the_actor_and_opens_a_connection(
    fake_database: FakeDatabase,
) -> None:
    assert await connect(fake_database) == ACTOR
    assert fake_database.units == [("ops_connection_check", "transient")]
    ((statement, _),) = fake_database.executed
    assert isinstance(statement, Select)


async def test_connect_without_a_token_exits_3(fake_database: FakeDatabase) -> None:
    fake_database.token = None
    with pytest.raises(AccessDenied):
        await connect(fake_database)
    assert fake_database.units == []


def _event(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "action": "test_users.rotated",
        "actor_object_id": ACTOR,
        "target_type": "test_users",
        "target_id": None,
        "metadata": TestUsersRotatedMetadata(environment="dev", alias_count=4),
    }
    return operator_event(**{**values, **overrides})


def test_operator_event_is_an_operator_row() -> None:
    event = _event()
    assert event.actor_type == "operator"
    assert str(event.actor_object_id) == ACTOR
    assert event.metadata_json() == '{"environment":"dev","aliasCount":4}'


def test_operator_event_is_checked_against_the_catalogue() -> None:
    with pytest.raises(ValidationError):
        _event(target_id="x")
    with pytest.raises(ValidationError):
        _event(metadata=None)


async def test_write_event_inserts_the_row_with_the_run_correlation_id(
    fake_database: FakeDatabase,
) -> None:
    with correlation_scope("4bf92f3577b34da6a3ce929d0e0e4736"):
        await write_event(fake_database, _event())
    assert fake_database.units == [("write_audit_event", "none")]
    (row,) = fake_database.audit_rows
    assert row["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert row["action"] == "test_users.rotated"
    assert json.loads(row["metadata_json"]) == {"environment": "dev", "aliasCount": 4}


def test_unit_names_are_statement_set_names() -> None:
    for name in (audit.CONNECTION_CHECK, audit.WRITE_AUDIT_EVENT):
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name)
