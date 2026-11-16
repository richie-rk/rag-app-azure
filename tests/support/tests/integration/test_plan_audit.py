"""T-567, the plan audit meta-test (`84-testing.md` section 2.11): against the compose SQL Server,
the plan audit fails a registered statement that scans a 2,000-row table, and passes one that
seeks it and the bounded range scans of T-067 (a scan a `TOP` of at most 1,000 rows cuts, and a
scan of a filtered index whose filter admits at most 1,000 rows)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from functools import partial

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, column, create_engine, literal_column, select, table
from sqlalchemy.pool import NullPool

from docchat_core.settings import LocalSettings, load_settings
from docchat_core.sql.engine import (
    RequestPathEntry,
    SqlEngine,
    emulator_connection_string,
    open_connection,
)
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, top_rows
from docchat_core.sql.settings import SqlSettings
from docchat_testing.data_audit import PlanAudit, PlanScan
from docchat_testing.plugin import ComposeService

ROWS = 2_000
_PLAN_ROWS = table("plan_rows", column("id"), column("payload"), schema="dbo")


@dataclass(frozen=True)
class PlanDatabase:
    name: str
    password: str
    engine: Engine


def _engine(password: str, database: str) -> Engine:
    odbc = emulator_connection_string("ops", password=password, database=database)
    creator = partial(
        open_connection, odbc, access_token=None, login_timeout=30, command_timeout=60
    )
    return create_engine("mssql+pyodbc://", creator=creator, poolclass=NullPool)


@pytest.fixture
def plan_database(compose_sqlserver: ComposeService) -> Iterator[PlanDatabase]:
    """A database of its own holding one 2,000-row table, dropped afterwards."""
    secret = load_settings(LocalSettings).local_sql_sa_password
    if secret is None:
        pytest.fail(
            "DOCCHAT_LOCAL_SQL_SA_PASSWORD is unset: run docchat-ops --env local local init"
        )
    password = secret.get_secret_value()
    name = f"docchat_plan_{secrets.token_hex(4)}"
    master = _engine(password, "master")
    with master.connect() as base:
        base.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql(
            f"CREATE DATABASE [{name}]"
        )
    engine = _engine(password, name)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE dbo.plan_rows (id INT IDENTITY(1, 1) "
            "CONSTRAINT PK_plan_rows PRIMARY KEY CLUSTERED, payload INT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO dbo.plan_rows (payload) SELECT TOP (2000) "
            "ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) "
            "FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b"
        )
        # A filtered index whose filter admits 10 of the 2,000 rows.
        connection.exec_driver_sql(
            "CREATE INDEX IX_plan_rows_flagged ON dbo.plan_rows (id) WHERE payload <= 10"
        )
    try:
        yield PlanDatabase(name=name, password=password, engine=engine)
    finally:
        engine.dispose()
        with master.connect() as base:
            autocommit = base.execution_options(isolation_level="AUTOCOMMIT")
            autocommit.exec_driver_sql(
                f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
            )
            autocommit.exec_driver_sql(f"DROP DATABASE [{name}]")
        master.dispose()


@pytest.fixture
async def sql_engine(plan_database: PlanDatabase) -> AsyncIterator[SqlEngine]:
    engine = SqlEngine(
        "api",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(plan_database.password)
        ),
        credential=None,
        odbc_connection_string=emulator_connection_string(
            "api", password=plan_database.password, database=plan_database.name
        ),
    )
    yield engine
    await engine.dispose()


async def scan_by_payload(engine: SqlEngine, *, payload: int) -> None:
    await engine.fetch_all(
        select(_PLAN_ROWS.c.id).where(_PLAN_ROWS.c.payload == payload), name="plan_scan"
    )


async def seek_by_id(engine: SqlEngine, *, row_id: int) -> None:
    await engine.fetch_one(
        select(_PLAN_ROWS.c.payload).where(_PLAN_ROWS.c.id == row_id), name="plan_seek"
    )


async def first_rows(engine: SqlEngine, *, limit_plus_one: int) -> None:
    await engine.fetch_all(
        top_rows(select(_PLAN_ROWS.c.id).order_by(_PLAN_ROWS.c.id)),
        {LIMIT_PLUS_ONE: limit_plus_one},
        name="plan_page",
    )


async def flagged_ids(engine: SqlEngine) -> None:
    await engine.fetch_all(
        select(_PLAN_ROWS.c.id).where(_PLAN_ROWS.c.payload <= literal_column("10")),
        name="plan_flagged",
    )


REGISTRY = {
    "meta.scan_by_payload": RequestPathEntry(
        function=scan_by_payload, example_params={"payload": 7}
    ),
    "meta.seek_by_id": RequestPathEntry(function=seek_by_id, example_params={"row_id": 5}),
}

BOUNDED = {
    # The first 51 rows of the clustered index: a scan a `TOP` cuts after 51 rows.
    "meta.first_page": RequestPathEntry(function=first_rows, example_params={LIMIT_PLUS_ONE: 51}),
    # Every row of the filtered index: 10 rows, whatever the table holds.
    "meta.flagged": RequestPathEntry(function=flagged_ids, example_params={}),
}


async def test_t567_the_plan_audit_fails_a_scan_of_a_2000_row_table(
    plan_database: PlanDatabase, sql_engine: SqlEngine
) -> None:
    audit = PlanAudit(plan_database.engine, registry=REGISTRY)

    findings = await audit.run(sql_engine)

    assert [(f.function, f.scan, f.rows) for f in findings] == [
        (
            "meta.scan_by_payload",
            PlanScan("Clustered Index Scan", "dbo", "plan_rows", "PK_plan_rows"),
            ROWS,
        )
    ]


async def test_t567_an_exempt_statement_passes(
    plan_database: PlanDatabase, sql_engine: SqlEngine
) -> None:
    audit = PlanAudit(plan_database.engine, registry=REGISTRY, exempt={"meta.scan_by_payload"})

    assert await audit.run(sql_engine) == []


async def test_t567_bounded_range_scans_pass(
    plan_database: PlanDatabase, sql_engine: SqlEngine
) -> None:
    audit = PlanAudit(plan_database.engine, registry=BOUNDED)

    assert await audit.run(sql_engine) == []


async def test_t567_a_top_above_the_limit_doesnt_bound_a_scan(
    plan_database: PlanDatabase, sql_engine: SqlEngine
) -> None:
    registry = {
        "meta.first_page": RequestPathEntry(
            function=first_rows, example_params={LIMIT_PLUS_ONE: 1_500}
        )
    }
    audit = PlanAudit(plan_database.engine, registry=registry)

    findings = await audit.run(sql_engine)

    assert [(f.function, f.scan, f.rows) for f in findings] == [
        (
            "meta.first_page",
            PlanScan("Clustered Index Scan", "dbo", "plan_rows", "PK_plan_rows", 1_500),
            1_500,
        )
    ]
