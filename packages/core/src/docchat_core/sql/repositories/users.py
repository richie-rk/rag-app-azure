"""First-sign-in provisioning and the user search query Q6 (`11-api.md` sections 1.4.4, 3.2.1 and
3.8.1; `80-data-model.md` sections 2.2 and 2.4; ADR 0021).

**Provisioning** is one unit of work under `SET XACT_ABORT ON`: an `UPDATE … WITH (UPDLOCK,
HOLDLOCK)` of the caller's row, and only when that touched no row, the row's insert, one
`default` membership per default project and the `user.provisioned` audit row. `HOLDLOCK` takes
a key-range lock on the absent key, so two first requests of one person serialise and the second
updates instead of inserting. An empty display name or e-mail address never overwrites a stored
one.

**Q6** pages `app.users` by display name, then object ID: by exact object ID when `q` is a
canonical UUID, by a case-insensitive prefix of the display name or the e-mail address (two
seeks combined with `UNION`), or over every user without `q`; each with or without the
membership column of a project and with or without a cursor, one constant text per case.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from types import MappingProxyType
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import (
    Connection,
    Select,
    and_,
    bindparam,
    case,
    insert,
    null,
    or_,
    select,
    union,
    update,
)
from sqlalchemy.dialects.mssql import UNIQUEIDENTIFIER

from docchat_core.audit.models import AuditEvent, UserProvisionedMetadata
from docchat_core.audit.writer import write_audit_event
from docchat_core.models.identifiers import parse_uuid
from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, ONE, ZERO, top_rows, utc
from docchat_core.sql.statements import SET_XACT_ABORT_ON
from docchat_core.sql.tables.access import project_members, projects, users
from docchat_core.sql.tables.schema import (
    BIT_TRUE,
    EMPTY_NVARCHAR,
    SYSUTCDATETIME,
    nvarchar_literal,
)

type UserRole = Literal["Admin", "User"]

ENSURE_USER: Final = "ensure_user"
SEARCH_USERS: Final = "search_users"
"""Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics."""

LIKE_ESCAPE: Final = "\\"
_LIKE_SPECIALS: Final = ("\\", "%", "_", "[")

# Fixed identifiers for the plan audit; the provisioning example inserts this user once.
_EXAMPLE_USER: Final = UUID("9c4d5e6f-7a8b-4c0d-9e2f-3a4b5c6d7e63")
_EXAMPLE_TENANT: Final = UUID("0d5e6f7a-8b9c-4d1e-8f3a-4b5c6d7e8f64")
_EXAMPLE_PROJECT: Final = UUID("1e6f7a8b-9c0d-4e2f-9a4b-5c6d7e8f9a65")
_EXAMPLE_PAGE: Final = 51


# --- Provisioning ------------------------------------------------------------------------------


def _unless_empty(column: str) -> Any:
    value = bindparam(column, type_=users.c[column].type)
    return case((value == EMPTY_NVARCHAR, users.c[column]), else_=value)


UPDATE_USER: Final = (
    update(users)
    .with_hint("WITH (UPDLOCK, HOLDLOCK)", dialect_name="mssql")
    .where(users.c.object_id == bindparam("oid"))
    .values(
        tenant_id=bindparam("tid"),
        display_name=_unless_empty("display_name"),
        email=_unless_empty("email"),
        last_known_role=bindparam("role"),
        # SYSUTCDATETIME() is read when the statement starts, before it waits for the key lock,
        # so a request queued behind a concurrent first sign-in would stamp a time earlier than
        # the new row's first_seen_at and break CK_users_seen_order (11-api.md section 3.2.1).
        last_seen_at=case(
            (users.c.first_seen_at > SYSUTCDATETIME, users.c.first_seen_at),
            else_=SYSUTCDATETIME,
        ),
    )
)
"""The common case: the caller's row exists and gets the token's values and `last_seen_at`."""

INSERT_USER: Final = insert(users).values(
    object_id=bindparam("oid"),
    tenant_id=bindparam("tid"),
    display_name=bindparam("display_name"),
    email=bindparam("email"),
    last_known_role=bindparam("role"),
)


def _default_memberships() -> Any:
    p = projects.alias("p")
    return insert(project_members).from_select(
        ["user_object_id", "project_id", "grant_source", "granted_by"],
        select(
            bindparam("oid", type_=UNIQUEIDENTIFIER()),
            p.c.id,
            nvarchar_literal("default"),
            null(),
        ).where(p.c.is_default == BIT_TRUE),
    )


INSERT_DEFAULT_MEMBERSHIPS: Final = _default_memberships()
"""One `default` membership per project flagged default at this moment, read through the filtered
index `IX_projects_default`; a default project is always active."""


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """`provisioned` when the row was created, with the default memberships it got."""

    provisioned: bool
    default_project_count: int


def _provision(
    connection: Connection,
    *,
    oid: UUID,
    tid: UUID,
    display_name: str,
    email: str,
    role: UserRole,
) -> ProvisionResult:
    connection.execute(SET_XACT_ABORT_ON)
    values = {
        "oid": oid,
        "tid": tid,
        "display_name": display_name,
        "email": email,
        "role": role,
    }
    if connection.execute(UPDATE_USER, values).rowcount:
        return ProvisionResult(provisioned=False, default_project_count=0)
    connection.execute(INSERT_USER, values)
    count = connection.execute(INSERT_DEFAULT_MEMBERSHIPS, {"oid": oid}).rowcount
    write_audit_event(
        connection,
        AuditEvent(
            action="user.provisioned",
            actor_type="user",
            actor_object_id=oid,
            subject_object_id=oid,
            target_type="user",
            target_id=str(oid),
            metadata=UserProvisionedMetadata(default_project_count=count),
        ),
    )
    return ProvisionResult(provisioned=True, default_project_count=count)


@request_path(
    example_params={
        "oid": _EXAMPLE_USER,
        "tid": _EXAMPLE_TENANT,
        "display_name": "",
        "email": "",
        "role": "User",
    }
)
async def provision_user(
    engine: SqlEngine,
    *,
    oid: UUID,
    tid: UUID,
    display_name: str,
    email: str,
    role: UserRole,
) -> ProvisionResult:
    """Update or create the caller's row in one transaction. The unit re-runs as a whole on a
    transient error (a deadlock included): after its rollback nothing of it remains, and the
    update-first form makes a repeat after a lost commit acknowledgement an update."""
    work = partial(_provision, oid=oid, tid=tid, display_name=display_name, email=email, role=role)
    return await engine.run(work, name=ENSURE_USER, retry="transient")


# --- Q6 -------------------------------------------------------------------------------------

type SearchForm = Literal["prefix", "object_id", "all"]


def like_prefix(q: str) -> str:
    """The `LIKE` pattern of a prefix search: `q` with `\\`, `%`, `_` and `[` escaped by a
    preceding `\\`, followed by `%`."""
    escaped = q
    for special in _LIKE_SPECIALS:
        escaped = escaped.replace(special, LIKE_ESCAPE + special)
    return escaped + "%"


def _q6(form: SearchForm, *, project: bool, cursor: bool) -> Select[Any]:
    u = users.alias("u")
    m = project_members.alias("m")
    columns: list[Any] = [
        u.c.object_id,
        u.c.display_name,
        u.c.email,
        u.c.last_known_role,
        u.c.first_seen_at,
        u.c.last_seen_at,
    ]
    conditions: list[Any] = []
    if form == "prefix":
        prefix = bindparam("prefix", type_=users.c.email.type)
        hit = union(
            select(users.c.object_id).where(users.c.display_name.like(prefix, escape=LIKE_ESCAPE)),
            select(users.c.object_id).where(users.c.email.like(prefix, escape=LIKE_ESCAPE)),
        ).subquery("hit")
        source: Any = hit.join(u, u.c.object_id == hit.c.object_id)
    else:
        source = u
        if form == "object_id":
            conditions.append(u.c.object_id == bindparam("oid"))
    if project:
        columns.append(case((m.c.user_object_id.is_(None), ZERO), else_=ONE).label("is_member"))
        source = source.outerjoin(
            m, and_(m.c.user_object_id == u.c.object_id, m.c.project_id == bindparam("project_id"))
        )
    if cursor:
        after_name = bindparam("after_display_name", type_=users.c.display_name.type)
        conditions.append(
            or_(
                u.c.display_name > after_name,
                and_(
                    u.c.display_name == after_name,
                    u.c.object_id > bindparam("after_object_id"),
                ),
            )
        )
    return top_rows(
        select(*columns)
        .select_from(source)
        .where(*conditions)
        .order_by(u.c.display_name, u.c.object_id)
    )


USER_PAGES: Final[Mapping[tuple[SearchForm, bool, bool], Select[Any]]] = MappingProxyType(
    {
        (form, project, cursor): _q6(form, project=project, cursor=cursor)
        for form in ("prefix", "object_id", "all")
        for project in (False, True)
        for cursor in (False, True)
    }
)
"""Q6 by (form, with the project's membership column, with a cursor)."""


@dataclass(frozen=True, slots=True)
class UserPosition:
    """A keyset position in the user list: the last item's display name and object ID."""

    display_name: str
    object_id: UUID


@dataclass(frozen=True, slots=True)
class UserRow:
    object_id: UUID
    display_name: str
    email: str
    last_known_role: UserRole
    first_seen_at: datetime
    last_seen_at: datetime
    is_member: bool | None


def search_form(q: str | None) -> SearchForm:
    """`object_id` for a canonical UUID, `prefix` for any other text, `all` without `q`."""
    if q is None:
        return "all"
    try:
        parse_uuid(q)
    except ValueError:
        return "prefix"
    return "object_id"


@request_path(
    example_params={
        "q": "a",
        "project_id": _EXAMPLE_PROJECT,
        "after": None,
        LIMIT_PLUS_ONE: _EXAMPLE_PAGE,
    }
)
async def read_users(
    engine: SqlEngine,
    *,
    q: str | None,
    project_id: UUID | None,
    after: UserPosition | None,
    limit_plus_one: int,
) -> list[UserRow]:
    """Q6: up to `limit_plus_one` users after `after`, ordered by display name, then object ID;
    `is_member` is `None` unless `project_id` is given. `q` is the normalised search text."""
    form = search_form(q)
    parameters: dict[str, object] = {LIMIT_PLUS_ONE: limit_plus_one}
    if form == "prefix" and q is not None:
        parameters["prefix"] = like_prefix(q)
    elif form == "object_id" and q is not None:
        parameters["oid"] = parse_uuid(q)
    if project_id is not None:
        parameters["project_id"] = project_id
    if after is not None:
        parameters["after_display_name"] = after.display_name
        parameters["after_object_id"] = after.object_id
    statement = USER_PAGES[(form, project_id is not None, after is not None)]
    rows = await engine.fetch_all(statement, parameters, name=SEARCH_USERS)
    return [
        UserRow(
            object_id=row.object_id,
            display_name=row.display_name,
            email=row.email,
            last_known_role=row.last_known_role,
            first_seen_at=utc(row.first_seen_at),
            last_seen_at=utc(row.last_seen_at),
            is_member=None if project_id is None else row.is_member == 1,
        )
        for row in rows
    ]
