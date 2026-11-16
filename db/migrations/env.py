"""Alembic environment of the DocChat schema (`80-data-model.md` section 2.24; ADR 0027).

Migrations run synchronously, outside any event loop: in the `postprovision` hook as the deploy
identity, and against the local SQL Server in local-emulator mode. The connection comes from
`docchat_core.sql.engine`'s shared helpers, so the session carries `APP=docchat-migrations`, and
outside local-emulator mode its access token comes from the Azure CLI's sign-in (`azure/login`
in the deploy job, or the operator's `az login`). `alembic check` compares the migrated schema
with `docchat_core.sql.tables`, names included; only schema `app` takes part, and Alembic's own
version table lives in `dbo`, where no role holds a right.
"""

from __future__ import annotations

from typing import Any

from alembic import context
from azure.identity import AzureCliCredential
from sqlalchemy import Column, Connection
from sqlalchemy.dialects import mssql
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.types import TypeEngine

from docchat_core.settings import LocalSettings, load_settings
from docchat_core.sql.engine import TOKEN_SCOPE, create_sync_engine
from docchat_core.sql.tables import SCHEMA, metadata

VERSION_TABLE_SCHEMA = "dbo"
OFFLINE_DIALECT = "mssql"


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Admit only schema `app`, so the comparison ignores `dbo` (and `alembic_version`)."""
    del parent_names
    if type_ == "schema":
        return name == SCHEMA
    return True


def compare_type(
    migration_context: object,
    inspected_column: Column[Any] | ReflectedColumn,
    metadata_column: Column[Any],
    inspected_type: TypeEngine[Any],
    metadata_type: TypeEngine[Any],
) -> bool | None:
    """Alembic's type comparison, except that SQL Server reflects a `ROWVERSION` column as
    `timestamp`, its older name for the same type."""
    del migration_context, inspected_column, metadata_column
    if isinstance(metadata_type, mssql.ROWVERSION) and isinstance(inspected_type, mssql.TIMESTAMP):
        return False
    return None


def configure_options() -> dict[str, Any]:
    return {
        "target_metadata": metadata,
        "include_schemas": True,
        "include_name": include_name,
        "compare_type": compare_type,
        "transaction_per_migration": True,
        "version_table_schema": VERSION_TABLE_SCHEMA,
    }


def access_token() -> str | None:
    """The deploy identity's (or operator's) SQL token; none against the local emulator."""
    if load_settings(LocalSettings).local_emulators:
        return None
    token: str = AzureCliCredential().get_token(TOKEN_SCOPE).token
    return token


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: render the T-SQL without connecting."""
    context.configure(
        dialect_name=OFFLINE_DIALECT,
        literal_binds=True,
        **configure_options(),
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, **configure_options())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A test fixture that builds one database per session hands Alembic its own connection
    # (Alembic's `config.attributes` convention); every deployment connects here.
    given = context.config.attributes.get("connection")
    if isinstance(given, Connection):
        _migrate(given)
        return
    engine = create_sync_engine("migrations", access_token=access_token())
    with engine.connect() as connection:
        _migrate(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
