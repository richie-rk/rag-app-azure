"""Raw SQL, and the only module that calls `sqlalchemy.text` (`01-repository.md` sections 5.2 and
6.2 rule 10; ADR 0129). Its code owner is `docchat-security`.

Every other statement is a SQLAlchemy Core construct over `docchat_core.sql.tables`. What can't be
one lives here as a module-level constant with bound parameters only, or, for the DDL of the grants
step, which can't bind parameters, as a constant template whose every substituted value is
validated against a closed pattern first (`80-data-model.md` section 2.22; ADR 0125).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from docchat_core.sql.tables.schema import DATABASE_ROLES

type UserType = Literal["E", "X"]

LOGIN_PROBE: Final = text("SELECT 1")
"""The first statement after a login, which proves the database accepts the session."""

SET_XACT_ABORT_ON: Final = text("SET XACT_ABORT ON")
"""The first statement of a unit of work that `11-api.md` runs with `SET XACT_ABORT ON`
(first-sign-in provisioning, the administrator writes): any error rolls the whole transaction
back on the server, so no statement after a failed one can commit."""

SET_SNAPSHOT_ISOLATION: Final = text("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
"""The first statement of the user-data export's read-only transaction (`80-data-model.md` section
2.23): every read of the unit sees the database as of its first read, so the rows and actor
references of one export agree with each other. It runs before any data is read, because a
transaction that started at another level can't switch to `SNAPSHOT`."""

SET_READ_COMMITTED_ISOLATION: Final = text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
"""The last statement of that transaction: the level belongs to the session, so the pooled
connection goes back at the database's default (READ COMMITTED with row versioning)."""

DATABASE_PRINCIPALS: Final = text(
    "SELECT name, type, sid FROM sys.database_principals WHERE type IN (N'E', N'X', N'S', N'U', "
    "N'G', N'C', N'K', N'R')"
)
"""Every user of the database, of any authentication type, and every database role (`R`)."""

ROLE_MEMBERS: Final = text(
    "SELECT r.name AS role_name, m.name AS member_name "
    "FROM sys.database_role_members AS rm "
    "JOIN sys.database_principals AS r ON r.principal_id = rm.role_principal_id "
    "JOIN sys.database_principals AS m ON m.principal_id = rm.member_principal_id"
)
"""Every membership of every database role, the built-in roles included."""

DATABASE_PERMISSIONS: Final = text(
    "SELECT p.class_desc AS class_desc, g.name AS grantee, "
    "COALESCE(s.name, ss.name) AS schema_name, o.name AS object_name, c.name AS column_name, "
    "p.permission_name AS permission_name, p.state_desc AS state_desc "
    "FROM sys.database_permissions AS p "
    "JOIN sys.database_principals AS g ON g.principal_id = p.grantee_principal_id "
    "LEFT JOIN sys.objects AS o ON p.class = 1 AND o.object_id = p.major_id "
    "LEFT JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
    "LEFT JOIN sys.schemas AS ss ON p.class = 3 AND ss.schema_id = p.major_id "
    "LEFT JOIN sys.columns AS c ON p.class = 1 AND p.minor_id <> 0 "
    "AND c.object_id = p.major_id AND c.column_id = p.minor_id"
)
"""Every permission row: an object's or a schema's permission names its schema, and a non-zero
`minor_id` is a column grant, named by its column."""

_CREATE_USER: Final = "CREATE USER [{name}] WITH SID = 0x{sid}, TYPE = {type}"
_DROP_USER: Final = "DROP USER [{name}]"
_ADD_ROLE_MEMBER: Final = "ALTER ROLE {role} ADD MEMBER [{name}]"
_DROP_ROLE_MEMBER: Final = "ALTER ROLE {role} DROP MEMBER [{name}]"

CONTAINED_USER_PATTERN: Final = (
    r"^(?:id-docchat-(?:api|ingest)-(?:dev|prod|load)"
    r"|sg-docchat-(?:dev|prod|load)-(?:config-operators|conversation-readers|user-data-operators))$"
)
"""The five contained users of an environment (`80-data-model.md` section 2.22)."""

_CONTAINED_USER_RE: Final = re.compile(CONTAINED_USER_PATTERN)
_SID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_PRINCIPAL_NAME_MAX: Final = 128


def _contained_user(name: str) -> str:
    if not _CONTAINED_USER_RE.fullmatch(name):
        raise ValueError("not one of the five contained users")
    return name


def _role(role: str) -> str:
    if role not in DATABASE_ROLES:
        raise ValueError("not one of the five database roles")
    return role


def _quoted_member(name: str) -> str:
    # A member found in the database can have any name, so it is quoted as QUOTENAME does:
    # every `]` doubled inside the brackets.
    if not name or len(name) > _PRINCIPAL_NAME_MAX or any(not char.isprintable() for char in name):
        raise ValueError("not a database principal name")
    return name.replace("]", "]]")


def create_user(name: str, sid_hex: str, user_type: UserType) -> TextClause:
    """`CREATE USER [<name>] WITH SID = 0x<32 hex>, TYPE = E|X`."""
    if not _SID_RE.fullmatch(sid_hex):
        raise ValueError("a SID is 32 lower-case hexadecimal characters")
    if user_type not in ("E", "X"):
        raise ValueError("a contained user is of type E or X")
    return text(_CREATE_USER.format(name=_contained_user(name), sid=sid_hex, type=user_type))


def drop_user(name: str) -> TextClause:
    """`DROP USER [<name>]`, only for one of the five users, re-created under a new SID."""
    return text(_DROP_USER.format(name=_contained_user(name)))


def add_role_member(role: str, name: str) -> TextClause:
    """`ALTER ROLE <role> ADD MEMBER [<name>]`."""
    return text(_ADD_ROLE_MEMBER.format(role=_role(role), name=_contained_user(name)))


def drop_role_member(role: str, name: str) -> TextClause:
    """`ALTER ROLE <role> DROP MEMBER [<name>]`, for any member that isn't expected."""
    return text(_DROP_ROLE_MEMBER.format(role=_role(role), name=_quoted_member(name)))
