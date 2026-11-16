"""A migrated database on the `compose.yaml` SQL Server for the data-access integration tests,
with one test-only SQL login per database role (`84-testing.md` sections 1.4 and 2.1).

The database is created the way every test database is (`80-data-model.md` sections 2.1 and
2.23): collation `SQL_Latin1_General_CP1_CI_AS`, `READ_COMMITTED_SNAPSHOT ON` and
`ALLOW_SNAPSHOT_ISOLATION ON`, then `alembic upgrade head`. The SQL container has SQL
authentication only, so the logins stand in for the contained users of Azure.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import Service, SqlEngine, emulator_connection_string, open_connection
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables import DATABASE_ROLES

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "db" / "alembic.ini"
PASSWORD_SETTING = "DOCCHAT_LOCAL_SQL_SA_PASSWORD"


def sync_engine(odbc: str) -> Engine:
    creator = partial(
        open_connection, odbc, access_token=None, login_timeout=30, command_timeout=60
    )
    return create_engine("mssql+pyodbc://", creator=creator, poolclass=NullPool)


@pytest.fixture(scope="session")
def sa_password() -> str:
    password = os.environ.get(PASSWORD_SETTING)
    if not password:
        # An integration session fails rather than skips without its services.
        pytest.fail(f"{PASSWORD_SETTING} is not set; run docchat-ops --env local local init")
    return password


@pytest.fixture(scope="session")
def database(sa_password: str) -> Iterator[str]:
    name = f"docchat_test_{secrets.token_hex(4)}"
    master = sync_engine(
        emulator_connection_string("migrations", password=sa_password, database="master")
    ).execution_options(isolation_level="AUTOCOMMIT")
    with master.connect() as connection:
        connection.exec_driver_sql(f"CREATE DATABASE [{name}] COLLATE SQL_Latin1_General_CP1_CI_AS")
        connection.exec_driver_sql(f"ALTER DATABASE [{name}] SET READ_COMMITTED_SNAPSHOT ON")
        connection.exec_driver_sql(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
    migrations = sync_engine(
        emulator_connection_string("migrations", password=sa_password, database=name)
    )
    with migrations.connect() as connection:
        config = Config(str(ALEMBIC_INI))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    yield name
    with master.connect() as connection:
        connection.exec_driver_sql(
            f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
        )
        connection.exec_driver_sql(f"DROP DATABASE [{name}]")
    master.dispose()


@pytest.fixture(scope="session")
def admin(sa_password: str, database: str) -> Engine:
    """`sa` on the test database: arranges rows, reads catalogue views."""
    return sync_engine(emulator_connection_string("ops", password=sa_password, database=database))


@pytest.fixture(scope="session")
def role_logins(sa_password: str, database: str) -> Iterator[dict[str, tuple[str, str]]]:
    """One login and user per database role: role → (login, password)."""
    master = sync_engine(
        emulator_connection_string("grants", password=sa_password, database="master")
    ).execution_options(isolation_level="AUTOCOMMIT")
    target = sync_engine(
        emulator_connection_string("grants", password=sa_password, database=database)
    ).execution_options(isolation_level="AUTOCOMMIT")
    logins: dict[str, tuple[str, str]] = {}
    suffix = secrets.token_hex(4)
    with master.connect() as server, target.connect() as connection:
        for role in DATABASE_ROLES:
            login = f"{role}_{suffix}"
            password = f"Aa1!{secrets.token_urlsafe(18)}"
            server.exec_driver_sql(f"CREATE LOGIN [{login}] WITH PASSWORD = N'{password}'")
            connection.exec_driver_sql(f"CREATE USER [{login}] FOR LOGIN [{login}]")
            connection.exec_driver_sql(f"ALTER ROLE {role} ADD MEMBER [{login}]")
            logins[role] = (login, password)
    yield logins
    with master.connect() as server:
        for login, _ in logins.values():
            server.exec_driver_sql(f"DROP LOGIN [{login}]")


@pytest.fixture(scope="session")
def role_engines(database: str, role_logins: dict[str, tuple[str, str]]) -> dict[str, Engine]:
    return {
        role: sync_engine(
            emulator_connection_string("api", password=password, database=database, user=login)
        )
        for role, (login, password) in role_logins.items()
    }


type SqlEngineFactory = Callable[..., SqlEngine]


@pytest.fixture
async def make_sql_engine(sa_password: str, database: str) -> AsyncIterator[SqlEngineFactory]:
    """Async engines on the test database as `sa`; the test passes the service and options."""
    created: list[SqlEngine] = []

    def make(service: Service, **options: Any) -> SqlEngine:
        engine = SqlEngine(
            service,
            sql=SqlSettings(),
            local=LocalSettings(local_emulators=True, local_sql_sa_password=SecretStr(sa_password)),
            credential=None,
            odbc_connection_string=emulator_connection_string(
                service, password=sa_password, database=database
            ),
            **options,
        )
        created.append(engine)
        return engine

    yield make
    for engine in created:
        await engine.dispose()
