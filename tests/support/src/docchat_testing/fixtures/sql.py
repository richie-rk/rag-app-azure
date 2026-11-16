"""The SQL Server fixture (`84-testing.md` section 1.4; `80-data-model.md` sections 2.1 and 2.24).

One database `docchat_test_<session ID>` per session on the `compose.yaml` SQL Server, with the
collation and `READ_COMMITTED_SNAPSHOT ON` of production, migrated to head once by the Alembic
revisions themselves. Right after the migration the fixture keeps every row the revisions wrote
(the ops-config seed, the ingestion methods and the dispatcher heartbeat); after each test one
batch empties every `app` table in reverse foreign-key order and the kept rows are inserted
again exactly as the migrations wrote them. One SQL login per database role lets grant tests run
as each role. The database and the logins are dropped at session end.

Every call here blocks on the driver, so the fixtures are synchronous and run outside any test's
event loop; code under test reaches the database through `docchat_core.sql.engine.SqlEngine` with
`connection_string(...)` of the role it runs as.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Final

import pytest
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    BindParameter,
    Connection,
    Engine,
    Insert,
    Table,
    create_engine,
    delete,
    event,
    insert,
    null,
    select,
    update,
)
from sqlalchemy.dialects import mssql
from sqlalchemy.dialects.mssql import ROWVERSION
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.pool import NullPool

from docchat_core.settings import LocalSettings, load_settings
from docchat_core.sql.engine import (
    LOCAL_SQL_SERVER,
    Service,
    emulator_connection_string,
    open_connection,
)
from docchat_core.sql.tables import DATABASE_ROLES, DatabaseRole, metadata
from docchat_core.sql.tables.ingestion import document_versions
from docchat_testing.fixtures.session import session_id_of

DATABASE_PREFIX: Final = "docchat_test_"
MASTER_DATABASE: Final = "master"
VERSION_TABLE_SCHEMA: Final = "dbo"
"""Where `db/migrations/env.py` keeps `alembic_version`; the fixture migrates the same way."""
CONNECT_TIMEOUT_SECONDS: Final = 30
COMMAND_TIMEOUT_SECONDS: Final = 300
ALEMBIC_INI: Final = Path(__file__).resolve().parents[5] / "db" / "alembic.ini"

# Names and passwords reach the server as parameters and are quoted there with QUOTENAME, so no
# value is ever spliced into statement text.
CREATE_DATABASE: Final = """\
DECLARE @name sysname = ?;
DECLARE @create nvarchar(max) =
    N'CREATE DATABASE ' + QUOTENAME(@name) + N' COLLATE SQL_Latin1_General_CP1_CI_AS';
DECLARE @snapshot nvarchar(max) =
    N'ALTER DATABASE ' + QUOTENAME(@name) + N' SET READ_COMMITTED_SNAPSHOT ON';
EXEC (@create);
EXEC (@snapshot);"""

DROP_DATABASE: Final = """\
DECLARE @name sysname = ?;
IF DB_ID(@name) IS NOT NULL
BEGIN
    DECLARE @single nvarchar(max) =
        N'ALTER DATABASE ' + QUOTENAME(@name) + N' SET SINGLE_USER WITH ROLLBACK IMMEDIATE';
    DECLARE @drop nvarchar(max) = N'DROP DATABASE ' + QUOTENAME(@name);
    EXEC (@single);
    EXEC (@drop);
END"""

CREATE_LOGIN: Final = """\
DECLARE @name sysname = ?, @password nvarchar(128) = ?;
DECLARE @create nvarchar(max) = N'CREATE LOGIN ' + QUOTENAME(@name)
    + N' WITH PASSWORD = ' + QUOTENAME(@password, N'''') + N', CHECK_POLICY = OFF';
EXEC (@create);"""

DROP_LOGIN: Final = """\
DECLARE @name sysname = ?;
IF SUSER_ID(@name) IS NOT NULL
BEGIN
    DECLARE @drop nvarchar(max) = N'DROP LOGIN ' + QUOTENAME(@name);
    EXEC (@drop);
END"""

CREATE_ROLE_USER: Final = """\
DECLARE @name sysname = ?, @role sysname = ?;
DECLARE @user nvarchar(max) = N'CREATE USER ' + QUOTENAME(@name) + N' FOR LOGIN ' + QUOTENAME(@name);
DECLARE @member nvarchar(max) = N'ALTER ROLE ' + QUOTENAME(@role) + N' ADD MEMBER ' + QUOTENAME(@name);
EXEC (@user);
EXEC (@member);"""  # noqa: E501 - one statement per line reads better than a wrapped one

ROLE_SERVICES: Final[Mapping[DatabaseRole, Service]] = {
    "docchat_api": "api",
    "docchat_ingestion": "ingestion",
    "docchat_config_operator": "ops",
    "docchat_conversation_reader": "ops",
    "docchat_user_data_operator": "ops",
}
"""The `APP=docchat-<service>` each role's connection names, as the component that holds the
role in production would."""


def database_name(session_id: str) -> str:
    return f"{DATABASE_PREFIX}{session_id}"


def login_name(database: str, role: DatabaseRole) -> str:
    """`docchat_test_<id>_<role without docchat_>`, unique per session on a shared server."""
    return f"{database}_{role.removeprefix('docchat_')}"


def generate_password() -> str:
    """A login password with every character class; the logins live for one session."""
    return f"{secrets.token_hex(12)}Aa1-"


@dataclass(frozen=True, slots=True)
class RoleLogin:
    role: DatabaseRole
    login: str
    password: str = field(repr=False)


# The columns `app.trg_document_versions_scan_insert` holds at their initial values on INSERT
# (80-data-model.md section 2.13); the product sets them later with UPDATEs.
VERSION_LATER_COLUMNS: Final = (
    "scan_status",
    "scan_time",
    "scan_raw_result",
    "converter_version",
    "converted_sha256",
    "document_pdf_status",
)
_AFTER_INSERT: Final = "docchat_after_insert"
"""The connection's stack of rewritten INSERTs, each with the work its `after_execute` finishes:
the further row groups, then the version UPDATEs."""

type Row = dict[str, Any]
type VersionUpdate = tuple[Any, Row]


def _initial_version_values(row: Mapping[str, Any]) -> Row:
    status = "awaiting" if row.get("scan_required") else "not_required"
    return {"scan_status": status} | dict.fromkeys(VERSION_LATER_COLUMNS[1:])


def _given_rows(statement: Insert, multiparams: Any, params: Any) -> list[Row] | None:
    """The rows of a plain `insert(table)` run with parameter sets, or of a single-row
    `insert(table).values(...)` of plain values; `None` for every other INSERT (from a SELECT,
    with RETURNING, with SQL expressions), which runs untouched."""
    if (
        statement.select is not None
        or statement._multi_values
        or statement._returning
        or statement._post_values_clause is not None
    ):
        return None
    if statement._values:
        if multiparams or params:
            return None
        row: Row = {}
        for column, value in statement._values.items():
            if not isinstance(value, BindParameter) or value.callable is not None:
                return None
            row[str(getattr(column, "key", column))] = value.value
        return [row]
    rows = list(multiparams) if multiparams else ([params] if params else [])
    if not rows or not all(
        isinstance(row, Mapping) and all(isinstance(key, str) for key in row) for row in rows
    ):
        return None
    return [dict(row) for row in rows]


def _initial_versions(rows: list[Row]) -> tuple[list[Row], list[VersionUpdate]]:
    """Version rows at their initial scan and rendition values, and the UPDATE each row that a
    test builds in a later state (scanned, converted, rendered) needs afterwards."""
    if not all("id" in row for row in rows):
        return rows, []
    initial_rows: list[Row] = []
    updates: list[VersionUpdate] = []
    for row in rows:
        initial = _initial_version_values(row)
        later: Row = {
            column: row[column]
            for column in VERSION_LATER_COLUMNS
            if column in row and row[column] != initial[column]
        }
        initial_rows.append(row | initial)
        if later:
            updates.append((row["id"], later))
    return initial_rows, updates


def _row_groups(rows: list[Row]) -> list[list[Row]]:
    """The rows grouped by the columns they name, in the order each set first appears."""
    groups: dict[frozenset[str], list[Row]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    return list(groups.values())


def _split_insert(
    connection: Connection,
    statement: Any,
    multiparams: Any,
    params: Any,
    execution_options: Any,
) -> tuple[Any, Any, Any]:
    """Run a test's INSERT the way its rows mean it:

    - rows that name different columns run as one INSERT per column set, each row with exactly
      its own columns (one executemany compiles every row against the first row's columns, so
      it would drop the extra columns of a later row and refuse one that names fewer);
    - `app.document_versions` rows built in a later state run as the product writes them: the
      rows at their initial values, then one UPDATE each (`80-data-model.md` section 2.13).

    The first group runs as this statement; `_finish_insert` runs the rest once it has run."""
    del execution_options
    if not isinstance(statement, Insert):
        return statement, multiparams, params
    given = _given_rows(statement, multiparams, params)
    if given is None:
        return statement, multiparams, params
    rows: list[Row] = given
    updates: list[VersionUpdate] = []
    if statement.table is document_versions:
        rows, updates = _initial_versions(given)
    groups = _row_groups(rows)
    if len(groups) == 1 and not updates and rows == given:
        return statement, multiparams, params
    first = insert(statement.table)
    connection.info.setdefault(_AFTER_INSERT, []).append((first, groups[1:], updates))
    return first, groups[0], {}


def _finish_insert(connection: Connection, statement: Any, *_: Any) -> None:
    stack: list[tuple[Any, list[list[Row]], list[VersionUpdate]]] = connection.info.get(
        _AFTER_INSERT, []
    )
    if not stack or stack[-1][0] is not statement:
        return
    _, groups, updates = stack.pop()
    for group in groups:
        connection.execute(insert(statement.table), group)
    for version_id, later in updates:
        connection.execute(
            update(document_versions).where(document_versions.c.id == version_id).values(**later)
        )


def _forget_pending(context: ExceptionContext) -> None:
    """A failed statement leaves no rewritten INSERT behind on its connection."""
    if context.connection is not None:
        context.connection.info.pop(_AFTER_INSERT, None)


def insert_versions_as_the_product_does(engine: Engine) -> None:
    """Install `_split_insert` on a setup engine (as `sa`): the two-step version INSERT, and
    rows that name different columns inserted with exactly their own columns. Role engines keep
    SQLAlchemy's own INSERT and the trigger's refusal, which the schema tests prove."""
    event.listen(engine, "before_execute", _split_insert, retval=True)
    event.listen(engine, "after_execute", _finish_insert)
    event.listen(engine, "handle_error", _forget_pending)


def make_engine(connection_string: str, *, pooled: bool = False) -> Engine:
    """A synchronous engine over `docchat_core.sql.engine.open_connection`, for the fixture's own
    setup, clean-up and inspection: without a pool by default, or keeping its connections."""
    creator = partial(
        open_connection,
        connection_string,
        access_token=None,
        login_timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if pooled:
        return create_engine(
            "mssql+pyodbc://", creator=creator, pool_size=2, max_overflow=8, pool_pre_ping=False
        )
    return create_engine("mssql+pyodbc://", creator=creator, poolclass=NullPool)


def migrate_to_head(connection: Connection, ini_path: Path = ALEMBIC_INI) -> None:
    """Run every revision of `db/migrations/versions` up to head on `connection`, as
    `alembic upgrade head` does, with the version table in `dbo` as `env.py` keeps it.

    `env.py` connects to the configured database (`docchat`), so the fixture drives the same
    revisions through Alembic's environment API on its own connection instead.
    """
    config = Config(str(ini_path))
    script = ScriptDirectory.from_config(config)

    def upgrade(revision: Any, context: MigrationContext) -> Any:
        del context
        return script._upgrade_revs("head", revision)

    with EnvironmentContext(config, script, fn=upgrade, destination_rev="head") as environment:
        environment.configure(
            connection=connection,
            target_metadata=metadata,
            version_table_schema=VERSION_TABLE_SCHEMA,
            transaction_per_migration=True,
        )
        with environment.begin_transaction():
            environment.run_migrations()


def _kept_columns(table: Table) -> list[str]:
    # A rowversion is generated by the server and an identity by its seed; neither is inserted.
    return [
        column.name
        for column in table.columns
        if not isinstance(column.type, ROWVERSION) and column.identity is None
    ]


def snapshot_rows(connection: Connection) -> dict[str, list[dict[str, Any]]]:
    """Every row of every `app` table, keyed by table name, leaving out empty tables and the
    columns the server generates: taken right after the migration, these are its seed rows."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for table in metadata.sorted_tables:
        columns = [table.c[name] for name in _kept_columns(table)]
        found = [dict(row._mapping) for row in connection.execute(select(*columns))]
        if found:
            rows[table.name] = found
    return rows


def reset_batch() -> str:
    """The clean-up batch: the two circular foreign keys set to `NULL`, then one `DELETE` per
    `app` table in reverse foreign-key order. No statement carries a value."""
    dialect = mssql.dialect()  # type: ignore[no-untyped-call]
    statements: list[Any] = [
        update(table).values({column.name: null() for column in constraint.columns})
        for table in metadata.sorted_tables
        for constraint in table.foreign_key_constraints
        if constraint.use_alter
    ]
    statements.extend(delete(table) for table in reversed(metadata.sorted_tables))
    compiled = [
        str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        for statement in statements
    ]
    return ";\n".join(compiled) + ";"


def restore_rows(connection: Connection, rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Insert the kept rows in foreign-key order."""
    for table in metadata.sorted_tables:
        table_rows = rows.get(table.name)
        if table_rows:
            connection.execute(insert(table), [dict(row) for row in table_rows])


@dataclass(slots=True, kw_only=True)
class SqlTestDatabase:
    """The session's database: its name, the `sa` password, one login per role and the rows
    the migrations seeded."""

    name: str
    sa_password: str = field(repr=False)
    server: str = LOCAL_SQL_SERVER
    logins: dict[DatabaseRole, RoleLogin] = field(default_factory=dict)
    seed_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def connection_string(
        self, role: DatabaseRole | None = None, *, service: Service | None = None
    ) -> str:
        """The ODBC connection string of this database as `role`'s login, or as `sa` when
        `role` is `None`; pass it to `SqlEngine(odbc_connection_string=...)`."""
        if role is None:
            return emulator_connection_string(
                service or "migrations",
                password=self.sa_password,
                database=self.name,
                server=self.server,
            )
        login = self.logins[role]
        return emulator_connection_string(
            service or ROLE_SERVICES[role],
            password=login.password,
            database=self.name,
            user=login.login,
            server=self.server,
        )

    def engine(self, role: DatabaseRole | None = None) -> Engine:
        """A synchronous engine as `role` (or `sa`), for test setup and assertions.

        It keeps its connections and opens the first one here, in the synchronous fixture that
        asks for it: a SQL login takes up to a second, which inside a test's coroutine would hold
        the event loop past the guard's 100 ms, while a statement on an open connection takes a
        few milliseconds. The fixture disposes of the engine after the test."""
        engine = make_engine(self.connection_string(role), pooled=True)
        if role is None:
            insert_versions_as_the_product_does(engine)
        with engine.connect():
            pass
        return engine

    def _master(self) -> Engine:
        return make_engine(
            emulator_connection_string(
                "migrations",
                password=self.sa_password,
                database=MASTER_DATABASE,
                server=self.server,
            )
        )

    def create(self) -> None:
        """Create the database, migrate it to head, keep its seed rows and add the role logins."""
        master = self._master()
        try:
            with master.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(CREATE_DATABASE, (self.name,))
                for role in DATABASE_ROLES:
                    login = RoleLogin(role, login_name(self.name, role), generate_password())
                    connection.exec_driver_sql(CREATE_LOGIN, (login.login, login.password))
                    self.logins[role] = login
        finally:
            master.dispose()
        engine = self.engine()
        try:
            with engine.connect() as connection:
                migrate_to_head(connection)
            with engine.begin() as connection:
                self.seed_rows = snapshot_rows(connection)
                for login in self.logins.values():
                    connection.exec_driver_sql(CREATE_ROLE_USER, (login.login, login.role))
        finally:
            engine.dispose()

    def reset(self) -> None:
        """Empty every `app` table and put the seed rows back, in one transaction."""
        engine = self.engine()
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(reset_batch())
                restore_rows(connection, self.seed_rows)
        finally:
            engine.dispose()

    def drop(self) -> None:
        """Drop the database (closing its sessions) and the session's logins."""
        master = self._master()
        try:
            with master.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(DROP_DATABASE, (self.name,))
                for role in DATABASE_ROLES:
                    connection.exec_driver_sql(DROP_LOGIN, (login_name(self.name, role),))
        finally:
            master.dispose()


def sa_password_or_fail() -> str:
    password = load_settings(LocalSettings).local_sql_sa_password
    if password is None:
        pytest.fail(
            "DOCCHAT_LOCAL_SQL_SA_PASSWORD is not set: run `docchat-ops --env local local init` "
            "and `docker compose up -d --wait`",
            pytrace=False,
        )
    return password.get_secret_value()


@pytest.fixture(scope="session")
def sql_server(pytestconfig: pytest.Config) -> Iterator[SqlTestDatabase]:
    """The session's migrated database with its role logins; dropped at session end."""
    database = SqlTestDatabase(
        name=database_name(session_id_of(pytestconfig)), sa_password=sa_password_or_fail()
    )
    try:
        database.create()
        yield database
    finally:
        database.drop()


@pytest.fixture
def sql_database(sql_server: SqlTestDatabase) -> Iterator[SqlTestDatabase]:
    """The session's database for one test, emptied and re-seeded after it."""
    yield sql_server
    sql_server.reset()
