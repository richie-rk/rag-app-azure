"""SQLAlchemy Core metadata of every application table (`80-data-model.md` section 2.1 item 8).

Module `access` defines the seven access and configuration tables and module `ingestion` the
eleven ingestion tables, all on one `MetaData(schema="app")`; each area module also holds its rows
of the grant matrix. Repositories and the apps build every statement from these `Table` objects.
"""

from types import MappingProxyType
from typing import Final

from docchat_core.sql.tables.access import ACCESS_GRANTS, ACCESS_TABLES
from docchat_core.sql.tables.ingestion import INGESTION_GRANTS, INGESTION_TABLES
from docchat_core.sql.tables.schema import (
    DATABASE_ROLES,
    SCHEMA,
    VERBS,
    DatabaseRole,
    GrantMatrix,
    RoleRights,
    Verb,
    metadata,
)

GRANT_MATRIX: Final[GrantMatrix] = MappingProxyType({**ACCESS_GRANTS, **INGESTION_GRANTS})
"""The grant matrix of `80-data-model.md` section 2.22, one row per `app` table."""

ACTOR_COLUMNS: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType(
    {
        table.name: tuple(column.name for column in table.columns if column.info.get("actor"))
        for table in (*ACCESS_TABLES, *INGESTION_TABLES)
        if any(column.info.get("actor") for column in table.columns)
    }
)
"""Each table's actor columns, which the user-data export reads (`80-data-model.md` 2.23)."""

__all__ = [
    "ACCESS_TABLES",
    "ACTOR_COLUMNS",
    "DATABASE_ROLES",
    "GRANT_MATRIX",
    "INGESTION_TABLES",
    "SCHEMA",
    "VERBS",
    "DatabaseRole",
    "GrantMatrix",
    "RoleRights",
    "Verb",
    "metadata",
]
