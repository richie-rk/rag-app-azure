from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Insert, Select, insert

from docchat_core.sql.tables import DATABASE_ROLES, metadata
from docchat_core.sql.tables.ingestion import document_versions
from docchat_testing.fixtures import sql
from docchat_testing.fixtures.sql import (
    ALEMBIC_INI,
    SqlTestDatabase,
    database_name,
    generate_password,
    login_name,
    reset_batch,
    restore_rows,
    snapshot_rows,
)

SEEDED_KEY = "chat.max_turn_seconds"


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[Any]:
        return iter(type("Row", (), {"_mapping": row})() for row in self._rows)


class FakeConnection:
    """Records statements; answers a `SELECT` with the rows given for its table."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables = tables or {}
        self.driver_sql: list[tuple[str, tuple[Any, ...] | None]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.selected_columns: dict[str, list[str]] = {}
        self.isolation: str | None = None

    def execution_options(self, **options: Any) -> FakeConnection:
        self.isolation = options.get("isolation_level")
        return self

    def exec_driver_sql(self, statement: str, parameters: tuple[Any, ...] | None = None) -> None:
        self.driver_sql.append((statement, parameters))

    def execute(self, statement: Any, parameters: list[dict[str, Any]] | None = None) -> FakeResult:
        if isinstance(statement, Select):
            table = statement.get_final_froms()[0].name  # type: ignore[attr-defined]
            self.selected_columns[table] = [column.name for column in statement.selected_columns]
            return FakeResult(self.tables.get(table, []))
        self.inserts.append((statement.table.name, list(parameters or [])))
        return FakeResult([])

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> FakeConnection:
        return self.connection

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        yield self.connection

    def dispose(self) -> None:
        self.disposed = True


def test_names_and_passwords() -> None:
    name = database_name("0123abcd")
    assert name == "docchat_test_0123abcd"
    logins = [login_name(name, role) for role in DATABASE_ROLES]
    assert logins[0] == "docchat_test_0123abcd_api"
    assert "docchat_test_0123abcd_user_data_operator" in logins
    assert len(set(logins)) == 5
    assert all(len(login) <= 128 for login in logins)
    password = generate_password()
    assert re.search(r"[a-z]", password)
    assert re.search(r"[A-Z]", password)
    assert re.search(r"[0-9]", password)
    assert re.search(r"[^A-Za-z0-9]", password)
    assert generate_password() != password


def test_reset_batch_deletes_children_before_parents() -> None:
    statements = [line.rstrip(";") for line in reset_batch().splitlines()]
    assert statements[:2] == [
        "UPDATE app.search_indexes SET active_physical_index_id=NULL",
        "UPDATE app.documents SET current_version_id=NULL",
    ]
    deletes = [statement.removeprefix("DELETE FROM app.") for statement in statements[2:]]
    assert sorted(deletes) == sorted(table.name for table in metadata.sorted_tables)
    assert len(deletes) == 18
    position = {name: index for index, name in enumerate(deletes)}
    for table in metadata.sorted_tables:
        for constraint in table.foreign_key_constraints:
            parent = constraint.referred_table.name
            # The two circular keys are set to NULL before any delete.
            if parent != table.name and not constraint.use_alter:
                assert position[table.name] < position[parent], (table.name, parent)
    assert "?" not in reset_batch()


def test_snapshot_keeps_seed_rows_without_generated_columns() -> None:
    row = {
        "config_key": SEEDED_KEY,
        "value_json": '{"value":180}',
        "updated_at": "2026-01-01T00:00:00.000",
    }
    connection = FakeConnection({"ops_config": [row], "ingestion_dispatcher_state": [{"id": 1}]})
    rows = snapshot_rows(connection)  # type: ignore[arg-type]
    assert rows == {"ops_config": [row], "ingestion_dispatcher_state": [{"id": 1}]}
    assert "row_version" not in connection.selected_columns["ops_config"]
    assert "id" not in connection.selected_columns["audit_events"]
    assert "queue_seq" not in connection.selected_columns["ingestion_run_items"]


def test_restore_inserts_in_foreign_key_order() -> None:
    connection = FakeConnection()
    restore_rows(
        connection,  # type: ignore[arg-type]
        {
            "ops_config": [{"config_key": SEEDED_KEY}],
            "ingestion_methods": [{"method_key": "x"}],
            "users": [],
        },
    )
    tables = [table for table, _ in connection.inserts]
    assert sorted(tables) == ["ingestion_methods", "ops_config"]
    assert dict(connection.inserts)["ops_config"] == [{"config_key": SEEDED_KEY}]
    connection = FakeConnection()
    restore_rows(
        connection,  # type: ignore[arg-type]
        {"search_indexes": [{"id": 1}], "ingestion_methods": [{"method_key": "x"}]},
    )
    assert [table for table, _ in connection.inserts] == ["ingestion_methods", "search_indexes"]


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, FakeConnection]]:
    connections: dict[str, FakeConnection] = {}
    migrated: list[FakeConnection] = []

    def migrate_to_head(connection: FakeConnection) -> None:
        migrated.append(connection)

    def make_engine(connection_string: str, *, pooled: bool = False) -> FakeEngine:
        del pooled
        database = re.search(r"Database=([^;]+);", connection_string)
        assert database is not None
        connection = connections.setdefault(
            database.group(1), FakeConnection({"ops_config": [{"config_key": SEEDED_KEY}]})
        )
        return FakeEngine(connection)

    monkeypatch.setattr(sql, "make_engine", make_engine)
    monkeypatch.setattr(sql, "migrate_to_head", migrate_to_head)
    monkeypatch.setattr(sql, "insert_versions_as_the_product_does", lambda engine: None)
    yield connections
    assert migrated == [connections["docchat_test_0123abcd"]]


def test_create_reset_and_drop(engines: dict[str, FakeConnection]) -> None:
    database = SqlTestDatabase(name="docchat_test_0123abcd", sa_password="Sa-password-1")
    database.create()
    master = engines["master"]
    assert master.isolation == "AUTOCOMMIT"
    assert master.driver_sql[0] == (sql.CREATE_DATABASE, ("docchat_test_0123abcd",))
    created_logins = [
        params for statement, params in master.driver_sql if statement == sql.CREATE_LOGIN
    ]
    assert [params[0] for params in created_logins if params] == [
        login_name(database.name, role) for role in DATABASE_ROLES
    ]
    assert set(database.logins) == set(DATABASE_ROLES)
    own = engines["docchat_test_0123abcd"]
    users = [params for statement, params in own.driver_sql if statement == sql.CREATE_ROLE_USER]
    assert ("docchat_test_0123abcd_ingestion", "docchat_ingestion") in users
    assert database.seed_rows == {"ops_config": [{"config_key": SEEDED_KEY}]}

    database.reset()
    assert own.driver_sql[-1] == (reset_batch(), None)
    assert own.inserts[-1] == ("ops_config", [{"config_key": SEEDED_KEY}])

    database.drop()
    assert master.driver_sql[-6] == (sql.DROP_DATABASE, ("docchat_test_0123abcd",))
    assert master.driver_sql[-1] == (sql.DROP_LOGIN, ("docchat_test_0123abcd_user_data_operator",))


def test_connection_strings_name_the_login_and_component() -> None:
    database = SqlTestDatabase(name="docchat_test_0123abcd", sa_password="Sa;password}1")
    database.logins["docchat_api"] = sql.RoleLogin(
        "docchat_api", "docchat_test_0123abcd_api", "pw-1"
    )
    as_sa = database.connection_string()
    assert "UID=sa;" in as_sa
    assert "PWD={Sa;password}}1};" in as_sa
    assert "Database=docchat_test_0123abcd;" in as_sa
    assert as_sa.endswith("APP=docchat-migrations")
    as_api = database.connection_string("docchat_api")
    assert "UID=docchat_test_0123abcd_api;PWD=pw-1;" in as_api
    assert as_api.endswith("APP=docchat-api")
    assert database.connection_string("docchat_api", service="ops").endswith("APP=docchat-ops")
    assert "Sa;password" not in repr(database)
    assert "pw-1" not in repr(database.logins["docchat_api"])


def test_migrate_to_head_drives_the_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Environment:
        def __init__(self, config: Any, script: Any, *, fn: Any, destination_rev: str) -> None:
            seen["steps"] = fn(None, None)
            seen["destination"] = destination_rev

        def __enter__(self) -> Environment:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def configure(self, **options: Any) -> None:
            seen["options"] = options

        @contextmanager
        def begin_transaction(self) -> Iterator[None]:
            yield

        def run_migrations(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(sql, "EnvironmentContext", Environment)
    connection = object()
    sql.migrate_to_head(connection)  # type: ignore[arg-type]
    assert ALEMBIC_INI.is_file()
    assert [step.revision.revision for step in seen["steps"]] == ["0001", "0002"]
    assert seen["destination"] == "head"
    assert seen["options"]["connection"] is connection
    assert seen["options"]["version_table_schema"] == "dbo"
    assert seen["options"]["transaction_per_migration"] is True
    assert seen["ran"]


def test_sa_password_comes_from_the_local_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", raising=False)
    with pytest.raises(pytest.fail.Exception, match="docchat-ops --env local local init"):
        sql.sa_password_or_fail()
    monkeypatch.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", "Abcdefghijklmnopqrstuv1!")
    assert sql.sa_password_or_fail() == "Abcdefghijklmnopqrstuv1!"


class _Info:
    """A connection's `info`, and the statements run on it."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.executed: list[tuple[Any, Any]] = []

    def execute(self, statement: Any, parameters: Any = None) -> None:
        self.executed.append((statement, parameters))


def test_a_version_built_in_a_later_state_is_inserted_at_its_initial_values_first() -> None:
    connection = _Info()
    scanned = {
        "id": "v1",
        "scan_required": True,
        "scan_status": "clean",
        "document_pdf_status": "ready",
    }
    fresh = {"id": "v2", "scan_required": False, "scan_status": "not_required"}

    statement, rows, params = sql._split_insert(
        cast("Any", connection), insert(document_versions), [scanned, fresh], {}, {}
    )

    assert params == {}
    assert [row["scan_status"] for row in rows] == ["awaiting", "not_required"]
    assert all(row["document_pdf_status"] is None for row in rows)
    assert isinstance(statement, Insert)
    sql._finish_insert(cast("Any", connection), statement)
    ((update_statement, _),) = connection.executed
    assert update_statement.compile().params == {
        "scan_status": "clean",
        "document_pdf_status": "ready",
        "id_1": "v1",
    }
    assert connection.info["docchat_after_insert"] == []


def test_rows_naming_different_columns_are_inserted_with_their_own_columns() -> None:
    connection = _Info()
    projects = metadata.tables["app.projects"]
    rows = [{"id": 1, "name": "a"}, {"id": 2}, {"id": 3, "name": "c"}]

    statement, first, params = sql._split_insert(
        cast("Any", connection), insert(projects), rows, {}, {}
    )
    sql._finish_insert(cast("Any", connection), statement)

    assert (first, params) == ([{"id": 1, "name": "a"}, {"id": 3, "name": "c"}], {})
    ((rest, group),) = connection.executed
    assert (rest.table, group) == (projects, [{"id": 2}])


def test_inserts_whose_rows_name_the_same_columns_pass_through() -> None:
    connection = _Info()
    statement = insert(metadata.tables["app.projects"])

    result = sql._split_insert(cast("Any", connection), statement, [{"id": 1}, {"id": 2}], {}, {})

    assert result == (statement, [{"id": 1}, {"id": 2}], {})
    assert connection.info == {}
    sql._finish_insert(cast("Any", connection), statement)
    assert connection.executed == []


def test_a_failed_statement_leaves_no_rewritten_insert_behind() -> None:
    connection = _Info()
    connection.info["docchat_after_insert"] = [("statement", [], [])]

    sql._forget_pending(cast("Any", SimpleNamespace(connection=connection)))

    assert connection.info == {}
