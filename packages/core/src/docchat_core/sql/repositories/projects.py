"""The administrator operations on projects, their members and their index mappings
(`11-api.md` sections 3.7 and 3.9; `80-data-model.md` sections 2.3 to 2.6, 2.21 and 7.2;
ADR 0022).

**Reads.** The administrator project list (the page's keys from `UQ_projects_name`, or
`IX_projects_status_name` with a status filter, then each row by `PK_projects` under a loop join,
so a page never reads the whole table; one constant text per filter and cursor case), one project
with its counts, default index and actors, a project's members (seek on
`IX_project_members_project`) and a project's mappings (seek on `PK_project_indexes`), each page a
keyset page over `TOP (:limit_plus_one)`. Archived projects are listed and read: administrator
routes have no reachability rule.

**Writes.** Each is one unit of work under `SET XACT_ABORT ON` that writes its audit rows in the
same transaction, so no change exists without its row and a failed audit insert fails the change.
`adminUpdateProject` and the four member and mapping operations share the two steps of section
3.7.1: the version check V (the project row under `UPDLOCK, ROWLOCK`, then the operation's own
lookups, then the caller's `If-Match` against `row_version`, then `409 project_archived`) and the
version move M (`updated_at` and `updated_by` set on the project, guarded by the row version it
was read with, its new `row_version` the response's `projectRowVersion`), which runs only when
the transaction changed a row. The project-row lock serialises every member and mapping change
with archiving, with field edits and with each other.

A refusal comes back as `Refused` with its code, never as an exception, so a refusal costs no
failed span and the query-plan audit can run every function with example parameters. The caller's
`If-Match` arrives as a `VersionCheck`, compared only after every lookup, because the order of
answers is `404`, `428`, `412`, `409` (`11-api.md` section 1.3). Every guarded write would refuse a
second application (the version or the unique name moved), so the units re-run as a whole on a
transient error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import (
    Connection,
    Row,
    Select,
    and_,
    bindparam,
    delete,
    exists,
    func,
    insert,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mssql import UNIQUEIDENTIFIER

from docchat_core.audit.models import (
    AuditEvent,
    ProjectArchivedMetadata,
    ProjectCreatedMetadata,
    ProjectField,
    ProjectIndexDefaultSetMetadata,
    ProjectIndexMappedMetadata,
    ProjectIndexUnmappedMetadata,
    ProjectMemberAddedMetadata,
    ProjectMemberRemovedMetadata,
    ProjectUpdatedMetadata,
)
from docchat_core.audit.writer import write_audit_event
from docchat_core.errors import DocChatProblem
from docchat_core.sql.engine import SqlEngine, SqlUniqueViolation, request_path
from docchat_core.sql.repositories.common import (
    LIMIT_PLUS_ONE,
    LOOP_JOIN,
    ONE,
    page_keys,
    top_rows,
    utc,
)
from docchat_core.sql.repositories.users import LIKE_ESCAPE, like_prefix
from docchat_core.sql.statements import SET_XACT_ABORT_ON
from docchat_core.sql.tables.access import (
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import search_indexes
from docchat_core.sql.tables.schema import BIT_FALSE, BIT_TRUE, SYSUTCDATETIME, nvarchar_literal

type ProjectStatus = Literal["active", "archived"]
type GrantSource = Literal["default", "admin"]
type IndexStatus = Literal[
    "provisioning",
    "ready",
    "reingest_required",
    "reingesting",
    "rebuilding",
    "failed",
    "deleting",
    "deleted",
]
type Refusal = Literal[
    "project_not_found",
    "user_not_found",
    "index_not_found",
    "precondition_required",
    "precondition_failed",
    "project_archived",
    "project_name_taken",
    "index_not_mappable",
    "default_index_required",
    "default_index_in_use",
    "status_change_with_fields",
]
"""Why a write refused: a problem code, or `status_change_with_fields`, the `422` of a `PATCH`
that changes the status together with other fields."""

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
LIST_PROJECTS: Final = "admin_list_projects"
READ_PROJECT: Final = "admin_read_project"
PROJECT_EXISTS: Final = "admin_project_exists"
CREATE_PROJECT: Final = "admin_create_project"
UPDATE_PROJECT: Final = "admin_update_project"
LIST_MEMBERS: Final = "admin_list_members"
ADD_MEMBER: Final = "admin_add_member"
REMOVE_MEMBER: Final = "admin_remove_member"
LIST_MAPPINGS: Final = "admin_list_mappings"
MAP_INDEX: Final = "admin_map_index"
UNMAP_INDEX: Final = "admin_unmap_index"

PROJECT_NAME_CONSTRAINT: Final = "UQ_projects_name"
MAPPABLE_STATUSES: Final = frozenset(
    {"provisioning", "ready", "reingest_required", "reingesting", "rebuilding"}
)
"""The logical index statuses a mapping may name (`80-data-model.md` section 7.2 rule 3);
`deleted` is absent (`404`), `failed` and `deleting` are refused (`409`)."""

_PROJECT_FIELDS: Final[tuple[tuple[str, ProjectField], ...]] = (
    ("name", "name"),
    ("description", "description"),
    ("instructions", "instructions"),
    ("example_questions", "exampleQuestions"),
    ("is_default", "isDefault"),
)
_REFUSED_VERSIONS: Final[frozenset[str]] = frozenset(
    {"precondition_required", "precondition_failed"}
)
_LOCKED: Final = "WITH (UPDLOCK, ROWLOCK)"

# Fixed identifiers for the plan audit: a plan doesn't depend on whether the rows exist.
_EXAMPLE_PROJECT: Final = UUID("2f7a8b9c-0d1e-4f3a-8b5c-6d7e8f9a0b66")
_EXAMPLE_USER: Final = UUID("3a8b9c0d-1e2f-4a4b-9c6d-7e8f9a0b1c67")
_EXAMPLE_INDEX: Final = UUID("4b9c0d1e-2f3a-4b5c-8d7e-8f9a0b1c2d68")
_EXAMPLE_CALLER: Final = UUID("5c0d1e2f-3a4b-4c6d-9e8f-9a0b1c2d3e69")
_EXAMPLE_PAGE: Final = 51


class VersionCheck(Protocol):
    """The caller's `If-Match`: `require_row_version` raises the `428` or `412` problem unless
    the tag names `current` (`docchat_api.http.IfMatchHeader`)."""

    def require_row_version(self, current: bytes, /) -> None: ...


class _AnyVersion:
    """The plan audit's `If-Match`: every version passes, so the example runs every statement
    its rows allow."""

    def require_row_version(self, current: bytes, /) -> None:
        del current


# --- Results ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Refused:
    """A write refused before it changed anything (or rolled back): the reason."""

    refusal: Refusal


@dataclass(frozen=True, slots=True)
class UserRefRow:
    """A person named by an actor column; `display_name` is `""` when no `app.users` row exists
    for the ID (actor columns carry no foreign key)."""

    user_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class IndexRefRow:
    index_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class AdminProjectSummaryRow:
    """One `AdminProjectSummary`."""

    id: UUID
    name: str
    description: str
    is_default: bool
    status: ProjectStatus
    member_count: int
    index_count: int
    default_index: IndexRefRow | None
    updated_at: datetime
    row_version: bytes


@dataclass(frozen=True, slots=True)
class AdminProjectRow:
    """One `AdminProject`."""

    id: UUID
    name: str
    description: str
    instructions: str
    example_questions: tuple[str, ...]
    is_default: bool
    status: ProjectStatus
    member_count: int
    index_count: int
    default_index: IndexRefRow | None
    created_at: datetime
    created_by: UserRefRow
    updated_at: datetime
    updated_by: UserRefRow
    archived_at: datetime | None
    archived_by: UserRefRow | None
    row_version: bytes


@dataclass(frozen=True, slots=True)
class ProjectMemberRow:
    """One `ProjectMember`; `granted_by` is `None` for a `default` membership."""

    user_id: UUID
    display_name: str
    email: str
    grant_source: GrantSource
    granted_at: datetime
    granted_by: UserRefRow | None
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectIndexMappingRow:
    """One `ProjectIndexMapping`."""

    index_id: UUID
    display_name: str
    index_status: IndexStatus
    is_default: bool
    mapped_at: datetime
    mapped_by: UserRefRow
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemberPosition:
    """A keyset position in a member list: the last item's display name and user ID."""

    display_name: str
    user_id: UUID


@dataclass(frozen=True, slots=True)
class MappingPosition:
    """A keyset position in a mapping list: whether the last item was the default, its display
    name and its index ID."""

    is_default: bool
    display_name: str
    index_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class NewProject:
    """A validated `ProjectCreate` with the project's new ID."""

    id: UUID
    name: str
    description: str
    instructions: str
    example_questions: tuple[str, ...]
    is_default: bool
    initial_member_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectChanges:
    """A validated `ProjectUpdate`: `None` is an absent field, which stays unchanged."""

    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    example_questions: tuple[str, ...] | None = None
    is_default: bool | None = None
    status: ProjectStatus | None = None

    def holds_fields(self) -> bool:
        """Whether any field other than `status` is present."""
        return any(getattr(self, attribute) is not None for attribute, _ in _PROJECT_FIELDS)


@dataclass(frozen=True, slots=True)
class ProjectWrite:
    """A created or updated project, read in the write's transaction, with the audit events the
    transaction wrote (none when nothing changed)."""

    project: AdminProjectRow
    events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class UnknownMembers:
    """`adminCreateProject` named initial members without an `app.users` row: their zero-based
    positions in the request; nothing was written."""

    positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MemberChange:
    """`adminAddProjectMember`: `created` when the membership was inserted."""

    member: ProjectMemberRow
    project_row_version: bytes
    created: bool
    events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class MappingChange:
    """`adminMapProjectIndex`: `created` when the mapping was inserted."""

    mapping: ProjectIndexMappingRow
    project_row_version: bytes
    created: bool
    events: tuple[AuditEvent, ...]


@dataclass(frozen=True, slots=True)
class PartRemoval:
    """`adminRemoveProjectMember` and `adminUnmapProjectIndex`: `removed` when a row was
    deleted."""

    removed: bool
    project_row_version: bytes
    events: tuple[AuditEvent, ...]


class _Abort(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Rolls a unit of work back after it wrote, carrying the refusal to answer."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal)
        self.refusal: Refusal = refusal


# --- Shared column sets ---------------------------------------------------------------------


def _member_count(p: Any) -> Any:
    m = project_members.alias("mc")
    return (
        select(func.count())
        .select_from(m)
        .where(m.c.project_id == p.c.id)
        .scalar_subquery()
        .label("member_count")
    )


def _index_count(p: Any) -> Any:
    i = project_indexes.alias("ic")
    return (
        select(func.count())
        .select_from(i)
        .where(i.c.project_id == p.c.id)
        .scalar_subquery()
        .label("index_count")
    )


def _with_default_index(p: Any, source: Any) -> tuple[Any, list[Any]]:
    """`source` joined with the project's default mapping (through `UX_project_indexes_default`)
    and that logical index's display name."""
    d = project_indexes.alias("d")
    s = search_indexes.alias("s")
    joined = source.outerjoin(d, and_(d.c.project_id == p.c.id, d.c.is_default == BIT_TRUE))
    joined = joined.outerjoin(s, s.c.id == d.c.search_index_id)
    return joined, [
        d.c.search_index_id.label("default_index_id"),
        s.c.display_name.label("default_index_name"),
    ]


def _default_index(row: Row[Any]) -> IndexRefRow | None:
    if row.default_index_id is None:
        return None
    return IndexRefRow(index_id=row.default_index_id, display_name=row.default_index_name)


def _user_ref(user_id: UUID, display_name: str | None) -> UserRefRow:
    return UserRefRow(user_id=user_id, display_name=display_name or "")


def _example_questions(text: str) -> tuple[str, ...]:
    # The check constraint keeps the column a JSON array, and only this module writes it.
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("example_questions_json isn't an array of strings")
    return tuple(value)


def _example_questions_json(questions: Sequence[str]) -> str:
    return json.dumps(list(questions), ensure_ascii=False, separators=(",", ":"))


# --- The project list ------------------------------------------------------------------------


def _project_list(*, status: bool, prefix: bool, cursor: bool) -> Select[Any]:
    """The page's keys from `UQ_projects_name`, or `IX_projects_status_name` with a status filter
    (both hold `name` and the clustering key `id`), then each page row by `PK_projects`.

    The keys come first because the plan audit counts a scan as bounded only when the `TOP` reads
    it directly (`84-testing.md` section 2.11), and the joins of a plain `SELECT TOP` sit between
    them. `LOOP_JOIN` stays because, for a page of 51 rows, the optimiser otherwise merge-joins
    full scans of `PK_projects`, `UX_project_indexes_default` and `PK_project_indexes` (2,000
    rows each at T-067's volumes); a covering name index would remove only the first of those."""
    k = projects.alias("k")
    conditions: list[Any] = []
    if status:
        conditions.append(k.c.status == bindparam("status", type_=projects.c.status.type))
    if prefix:
        conditions.append(
            k.c.name.like(bindparam("prefix", type_=projects.c.name.type), escape=LIKE_ESCAPE)
        )
    if cursor:
        conditions.append(k.c.name > bindparam("after_name", type_=projects.c.name.type))
    page = page_keys(select(k.c.id, k.c.name).where(*conditions), k.c.name).subquery("page")
    p = projects.alias("p")
    source, default_columns = _with_default_index(p, page.join(p, p.c.id == page.c.id))
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.description,
            p.c.is_default,
            p.c.status,
            _member_count(p),
            _index_count(p),
            *default_columns,
            p.c.updated_at,
            p.c.row_version,
        )
        .select_from(source)
        .order_by(page.c.name)
        .with_statement_hint(LOOP_JOIN, dialect_name="mssql")
    )


PROJECT_PAGES: Final[Mapping[tuple[bool, bool, bool], Select[Any]]] = MappingProxyType(
    {
        (status, prefix, cursor): _project_list(status=status, prefix=prefix, cursor=cursor)
        for status in (False, True)
        for prefix in (False, True)
        for cursor in (False, True)
    }
)
"""The administrator list by (with a status filter, with a name prefix, with a cursor)."""


@request_path(
    example_params={"status": None, "q": None, "after_name": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_admin_projects(
    engine: SqlEngine,
    *,
    status: ProjectStatus | None,
    q: str | None,
    after_name: str | None,
    limit_plus_one: int,
) -> list[AdminProjectSummaryRow]:
    """Up to `limit_plus_one` projects after `after_name`, ordered by name; `status` `None` lists
    both statuses and `q` is a case-insensitive name prefix (the collation is case-insensitive),
    its `LIKE` wildcards escaped."""
    parameters: dict[str, object] = {LIMIT_PLUS_ONE: limit_plus_one}
    if status is not None:
        parameters["status"] = status
    if q is not None:
        parameters["prefix"] = like_prefix(q)
    if after_name is not None:
        parameters["after_name"] = after_name
    statement = PROJECT_PAGES[(status is not None, q is not None, after_name is not None)]
    rows = await engine.fetch_all(statement, parameters, name=LIST_PROJECTS)
    return [
        AdminProjectSummaryRow(
            id=row.id,
            name=row.name,
            description=row.description,
            is_default=bool(row.is_default),
            status=row.status,
            member_count=row.member_count,
            index_count=row.index_count,
            default_index=_default_index(row),
            updated_at=utc(row.updated_at),
            row_version=bytes(row.row_version),
        )
        for row in rows
    ]


# --- One project ------------------------------------------------------------------------------


def _admin_project() -> Select[Any]:
    p = projects.alias("p")
    created = users.alias("cu")
    updated = users.alias("uu")
    archived = users.alias("au")
    source, default_columns = _with_default_index(p, p)
    source = (
        source.outerjoin(created, created.c.object_id == p.c.created_by)
        .outerjoin(updated, updated.c.object_id == p.c.updated_by)
        .outerjoin(archived, archived.c.object_id == p.c.archived_by)
    )
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.description,
            p.c.instructions,
            p.c.example_questions_json,
            p.c.is_default,
            p.c.status,
            _member_count(p),
            _index_count(p),
            *default_columns,
            p.c.created_at,
            p.c.created_by,
            created.c.display_name.label("created_by_name"),
            p.c.updated_at,
            p.c.updated_by,
            updated.c.display_name.label("updated_by_name"),
            p.c.archived_at,
            p.c.archived_by,
            archived.c.display_name.label("archived_by_name"),
            p.c.row_version,
        )
        .select_from(source)
        .where(p.c.id == bindparam("project_id"))
    )


ADMIN_PROJECT: Final = _admin_project()
"""One project with its member and index counts, its default index and the people its actor
columns name (primary-key seeks)."""

PROJECT_ID: Final = select(projects.c.id).where(projects.c.id == bindparam("project_id"))
"""Whether a project exists, archived or not (seek on `PK_projects`)."""


def _admin_project_row(row: Row[Any]) -> AdminProjectRow:
    archived_by = None
    if row.archived_by is not None:
        archived_by = _user_ref(row.archived_by, row.archived_by_name)
    return AdminProjectRow(
        id=row.id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        example_questions=_example_questions(row.example_questions_json),
        is_default=bool(row.is_default),
        status=row.status,
        member_count=row.member_count,
        index_count=row.index_count,
        default_index=_default_index(row),
        created_at=utc(row.created_at),
        created_by=_user_ref(row.created_by, row.created_by_name),
        updated_at=utc(row.updated_at),
        updated_by=_user_ref(row.updated_by, row.updated_by_name),
        archived_at=None if row.archived_at is None else utc(row.archived_at),
        archived_by=archived_by,
        row_version=bytes(row.row_version),
    )


def _read_project(connection: Connection, project_id: UUID) -> AdminProjectRow:
    """The project inside a write's transaction, which created or locked it."""
    row = connection.execute(ADMIN_PROJECT, {"project_id": project_id}).one()
    return _admin_project_row(row)


@request_path(example_params={"project_id": _EXAMPLE_PROJECT})
async def read_admin_project(engine: SqlEngine, *, project_id: UUID) -> AdminProjectRow | None:
    """`adminGetProject`; `None` when no project has this ID."""
    row = await engine.fetch_one(ADMIN_PROJECT, {"project_id": project_id}, name=READ_PROJECT)
    return None if row is None else _admin_project_row(row)


@request_path(example_params={"project_id": _EXAMPLE_PROJECT})
async def project_exists(engine: SqlEngine, *, project_id: UUID) -> bool:
    """Whether a project, active or archived, has this ID: the `404 project_not_found` of
    `adminSearchUsers`."""
    row = await engine.fetch_one(PROJECT_ID, {"project_id": project_id}, name=PROJECT_EXISTS)
    return row is not None


# --- Members ---------------------------------------------------------------------------------


def _members(*, cursor: bool, one: bool) -> Select[Any]:
    m = project_members.alias("m")
    u = users.alias("u")
    g = users.alias("g")
    conditions: list[Any] = [m.c.project_id == bindparam("project_id")]
    if one:
        conditions.append(m.c.user_object_id == bindparam("user_id"))
    if cursor:
        after_name = bindparam("after_display_name", type_=users.c.display_name.type)
        conditions.append(
            or_(
                u.c.display_name > after_name,
                and_(u.c.display_name == after_name, u.c.object_id > bindparam("after_user_id")),
            )
        )
    statement = (
        select(
            u.c.object_id,
            u.c.display_name,
            u.c.email,
            m.c.grant_source,
            m.c.granted_at,
            m.c.granted_by,
            g.c.display_name.label("granted_by_name"),
            u.c.last_seen_at,
        )
        .select_from(
            m.join(u, u.c.object_id == m.c.user_object_id).outerjoin(
                g, g.c.object_id == m.c.granted_by
            )
        )
        .where(*conditions)
    )
    if one:
        return statement
    return top_rows(statement.order_by(u.c.display_name, u.c.object_id))


MEMBER_PAGES: Final[Mapping[bool, Select[Any]]] = MappingProxyType(
    {cursor: _members(cursor=cursor, one=False) for cursor in (False, True)}
)
"""A project's members by display name, then user ID, with or without a cursor."""

ONE_MEMBER: Final = _members(cursor=False, one=True)
"""One membership with its person, for `adminAddProjectMember`'s answer."""


def _member_row(row: Row[Any]) -> ProjectMemberRow:
    granted_by = None
    if row.granted_by is not None:
        granted_by = _user_ref(row.granted_by, row.granted_by_name)
    return ProjectMemberRow(
        user_id=row.object_id,
        display_name=row.display_name,
        email=row.email,
        grant_source=row.grant_source,
        granted_at=utc(row.granted_at),
        granted_by=granted_by,
        last_seen_at=utc(row.last_seen_at),
    )


def _member_page(
    connection: Connection,
    *,
    project_id: UUID,
    after: MemberPosition | None,
    limit_plus_one: int,
) -> list[ProjectMemberRow] | None:
    if connection.execute(PROJECT_ID, {"project_id": project_id}).first() is None:
        return None
    parameters: dict[str, object] = {"project_id": project_id, LIMIT_PLUS_ONE: limit_plus_one}
    if after is not None:
        parameters["after_display_name"] = after.display_name
        parameters["after_user_id"] = after.user_id
    rows = connection.execute(MEMBER_PAGES[after is not None], parameters).all()
    return [_member_row(row) for row in rows]


@request_path(
    example_params={"project_id": _EXAMPLE_PROJECT, "after": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_project_members(
    engine: SqlEngine,
    *,
    project_id: UUID,
    after: MemberPosition | None,
    limit_plus_one: int,
) -> list[ProjectMemberRow] | None:
    """`adminListProjectMembers`: up to `limit_plus_one` members after `after`; `None` when no
    project, active or archived, has this ID."""
    work = partial(_member_page, project_id=project_id, after=after, limit_plus_one=limit_plus_one)
    return await engine.run(work, name=LIST_MEMBERS, retry="transient")


# --- Mappings --------------------------------------------------------------------------------


def _mappings(*, cursor: bool, one: bool) -> Select[Any]:
    pi = project_indexes.alias("pi")
    s = search_indexes.alias("s")
    c = users.alias("c")
    conditions: list[Any] = [pi.c.project_id == bindparam("project_id")]
    if one:
        conditions.append(pi.c.search_index_id == bindparam("index_id"))
    if cursor:
        after_default = bindparam("after_is_default", type_=project_indexes.c.is_default.type)
        after_name = bindparam("after_display_name", type_=search_indexes.c.display_name.type)
        conditions.append(
            or_(
                pi.c.is_default < after_default,
                and_(
                    pi.c.is_default == after_default,
                    or_(
                        s.c.display_name > after_name,
                        and_(
                            s.c.display_name == after_name,
                            s.c.id > bindparam("after_index_id"),
                        ),
                    ),
                ),
            )
        )
    statement = (
        select(
            s.c.id,
            s.c.display_name,
            s.c.status,
            pi.c.is_default,
            pi.c.created_at,
            pi.c.created_by,
            c.c.display_name.label("created_by_name"),
            pi.c.updated_at,
        )
        .select_from(
            pi.join(s, s.c.id == pi.c.search_index_id).outerjoin(
                c, c.c.object_id == pi.c.created_by
            )
        )
        .where(*conditions)
    )
    if one:
        return statement
    return top_rows(statement.order_by(pi.c.is_default.desc(), s.c.display_name, s.c.id))


MAPPING_PAGES: Final[Mapping[bool, Select[Any]]] = MappingProxyType(
    {cursor: _mappings(cursor=cursor, one=False) for cursor in (False, True)}
)
"""A project's mappings: the default first, then by display name, then index ID."""

ONE_MAPPING: Final = _mappings(cursor=False, one=True)
"""One mapping with its logical index, for `adminMapProjectIndex`'s answer."""


def _mapping_row(row: Row[Any]) -> ProjectIndexMappingRow:
    return ProjectIndexMappingRow(
        index_id=row.id,
        display_name=row.display_name,
        index_status=row.status,
        is_default=bool(row.is_default),
        mapped_at=utc(row.created_at),
        mapped_by=_user_ref(row.created_by, row.created_by_name),
        updated_at=utc(row.updated_at),
    )


def _mapping_page(
    connection: Connection,
    *,
    project_id: UUID,
    after: MappingPosition | None,
    limit_plus_one: int,
) -> list[ProjectIndexMappingRow] | None:
    if connection.execute(PROJECT_ID, {"project_id": project_id}).first() is None:
        return None
    parameters: dict[str, object] = {"project_id": project_id, LIMIT_PLUS_ONE: limit_plus_one}
    if after is not None:
        parameters["after_is_default"] = after.is_default
        parameters["after_display_name"] = after.display_name
        parameters["after_index_id"] = after.index_id
    rows = connection.execute(MAPPING_PAGES[after is not None], parameters).all()
    return [_mapping_row(row) for row in rows]


@request_path(
    example_params={"project_id": _EXAMPLE_PROJECT, "after": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_project_mappings(
    engine: SqlEngine,
    *,
    project_id: UUID,
    after: MappingPosition | None,
    limit_plus_one: int,
) -> list[ProjectIndexMappingRow] | None:
    """`adminListProjectIndexes`: up to `limit_plus_one` mappings after `after`; `None` when no
    project, active or archived, has this ID."""
    work = partial(_mapping_page, project_id=project_id, after=after, limit_plus_one=limit_plus_one)
    return await engine.run(work, name=LIST_MAPPINGS, retry="transient")


# --- The write statements --------------------------------------------------------------------

LOCK_PROJECT: Final = (
    select(
        projects.c.status,
        projects.c.is_default,
        projects.c.name,
        projects.c.description,
        projects.c.instructions,
        projects.c.example_questions_json,
        projects.c.row_version,
    )
    .with_hint(projects, _LOCKED, dialect_name="mssql")
    .where(projects.c.id == bindparam("project_id"))
)
"""Step V: the project row under `UPDLOCK, ROWLOCK`, with the fields `adminUpdateProject`
compares."""

LOCK_USER: Final = (
    select(ONE.label("found"))
    .select_from(users)
    .with_hint(users, _LOCKED, dialect_name="mssql")
    .where(users.c.object_id == bindparam("user_id"))
)
"""`adminAddProjectMember`'s lookup: the lock keeps a concurrent user deletion waiting until the
commit."""

LOCK_USERS: Final = (
    select(users.c.object_id)
    .with_hint(users, _LOCKED, dialect_name="mssql")
    .where(users.c.object_id.in_(bindparam("user_ids", expanding=True)))
)
"""`adminCreateProject`'s initial members, one expanding parameter of at most 50 values."""

LOCK_INDEX: Final = (
    select(search_indexes.c.status)
    .with_hint(search_indexes, _LOCKED, dialect_name="mssql")
    .where(search_indexes.c.id == bindparam("index_id"))
)
"""`adminMapProjectIndex`'s lookup of the logical index, locked like every index change locks
it, so a mapping and an index deletion can't pass each other's checks."""

MOVE_VERSION: Final = (
    update(projects)
    .where(projects.c.id == bindparam("project_id"), projects.c.row_version == bindparam("rv"))
    .values(updated_at=SYSUTCDATETIME, updated_by=bindparam("caller"))
    .returning(projects.c.row_version)
)
"""Step M: the project's version moves with each member or mapping change."""

INSERT_PROJECT: Final = insert(projects).values(
    id=bindparam("project_id"),
    name=bindparam("name"),
    description=bindparam("description"),
    instructions=bindparam("instructions"),
    example_questions_json=bindparam("example_questions_json"),
    is_default=bindparam("is_default"),
    created_by=bindparam("caller"),
    updated_by=bindparam("caller"),
)

UPDATE_FIELDS: Final = (
    update(projects)
    .where(projects.c.id == bindparam("project_id"), projects.c.row_version == bindparam("rv"))
    .values(
        name=bindparam("name"),
        description=bindparam("description"),
        instructions=bindparam("instructions"),
        example_questions_json=bindparam("example_questions_json"),
        is_default=bindparam("is_default"),
        updated_at=SYSUTCDATETIME,
        updated_by=bindparam("caller"),
    )
    .returning(projects.c.row_version)
)
"""A field edit: every editable column set to its new or kept value, one constant text."""

ARCHIVE_PROJECT: Final = (
    update(projects)
    .where(projects.c.id == bindparam("project_id"), projects.c.row_version == bindparam("rv"))
    .values(
        status=nvarchar_literal("archived"),
        archived_at=SYSUTCDATETIME,
        archived_by=bindparam("caller"),
        is_default=BIT_FALSE,
        updated_at=SYSUTCDATETIME,
        updated_by=bindparam("caller"),
    )
    .returning(projects.c.row_version)
)
"""Archiving, in one statement; an archived project is never default
(`CK_projects_default_active`)."""

RESTORE_PROJECT: Final = (
    update(projects)
    .where(projects.c.id == bindparam("project_id"), projects.c.row_version == bindparam("rv"))
    .values(
        status=nvarchar_literal("active"),
        archived_at=null(),
        archived_by=null(),
        updated_at=SYSUTCDATETIME,
        updated_by=bindparam("caller"),
    )
    .returning(projects.c.row_version)
)
"""Restoring: `is_default` stays `0`."""

INSERT_MEMBER: Final = insert(project_members).values(
    user_object_id=bindparam("user_id"),
    project_id=bindparam("project_id"),
    grant_source=nvarchar_literal("admin"),
    granted_by=bindparam("caller"),
)
"""An initial member of a new project."""


def _add_member() -> Any:
    existing = (
        select(ONE)
        .select_from(project_members)
        .where(
            project_members.c.user_object_id == bindparam("user_id"),
            project_members.c.project_id == bindparam("project_id"),
        )
    )
    return insert(project_members).from_select(
        ["user_object_id", "project_id", "grant_source", "granted_by"],
        select(
            bindparam("user_id", type_=UNIQUEIDENTIFIER()),
            bindparam("project_id", type_=UNIQUEIDENTIFIER()),
            nvarchar_literal("admin"),
            bindparam("caller", type_=UNIQUEIDENTIFIER()),
        ).where(~exists(existing)),
    )


ADD_MEMBER_IF_ABSENT: Final = _add_member()
"""The membership, unless one exists from either source."""

REMOVE_MEMBERSHIP: Final = (
    delete(project_members)
    .where(
        project_members.c.user_object_id == bindparam("user_id"),
        project_members.c.project_id == bindparam("project_id"),
    )
    .returning(project_members.c.grant_source)
)

PROJECT_MAPPINGS: Final = select(
    project_indexes.c.search_index_id, project_indexes.c.is_default
).where(project_indexes.c.project_id == bindparam("project_id"))
"""Every mapping of the project (seek on `PK_project_indexes`), read under the project lock."""

MAPPED_PROJECT: Final = "mapped_project_id"
"""The project parameter of the mapping updates: an `UPDATE` reserves a parameter named after
a column of its table (`project_id`) for that column's `SET` value."""

CLEAR_DEFAULT: Final = (
    update(project_indexes)
    .where(
        project_indexes.c.project_id == bindparam(MAPPED_PROJECT),
        project_indexes.c.is_default == BIT_TRUE,
    )
    .values(is_default=BIT_FALSE, updated_at=SYSUTCDATETIME, updated_by=bindparam("caller"))
)
"""The current default stops being the default before another becomes it, so the filtered
unique index never sees two."""

INSERT_MAPPING: Final = insert(project_indexes).values(
    project_id=bindparam("project_id"),
    search_index_id=bindparam("index_id"),
    is_default=bindparam("is_default"),
    created_by=bindparam("caller"),
    updated_by=bindparam("caller"),
)

SET_DEFAULT: Final = (
    update(project_indexes)
    .where(
        project_indexes.c.project_id == bindparam(MAPPED_PROJECT),
        project_indexes.c.search_index_id == bindparam("index_id"),
    )
    .values(is_default=BIT_TRUE, updated_at=SYSUTCDATETIME, updated_by=bindparam("caller"))
)

MAPPING_OVERRIDES: Final = (
    select(func.count())
    .select_from(user_index_overrides)
    .where(
        user_index_overrides.c.project_id == bindparam("project_id"),
        user_index_overrides.c.search_index_id == bindparam("index_id"),
    )
)
"""The overrides an unmapping removes (seek on `IX_user_index_overrides_mapping`)."""

DELETE_MAPPING: Final = delete(project_indexes).where(
    project_indexes.c.project_id == bindparam("project_id"),
    project_indexes.c.search_index_id == bindparam("index_id"),
)
"""The mapping; `FK_user_index_overrides_mapping` deletes its overrides."""


# --- Shared write steps ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Locked:
    status: ProjectStatus
    is_default: bool
    name: str
    description: str
    instructions: str
    example_questions: tuple[str, ...]
    row_version: bytes


def _lock_project(connection: Connection, project_id: UUID) -> _Locked | None:
    row = connection.execute(LOCK_PROJECT, {"project_id": project_id}).first()
    if row is None:
        return None
    return _Locked(
        status=row.status,
        is_default=bool(row.is_default),
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        example_questions=_example_questions(row.example_questions_json),
        row_version=bytes(row.row_version),
    )


def _version_refusal(version: VersionCheck, current: bytes) -> Refused | None:
    """The `If-Match` comparison, after every lookup: `428` or `412` as a refusal."""
    try:
        version.require_row_version(current)
    except DocChatProblem as problem:
        code = problem.entry.code
        if code not in _REFUSED_VERSIONS:
            raise
        return Refused(cast("Refusal", code))
    return None


def _moved(connection: Connection, statement: Any, parameters: Mapping[str, object]) -> bytes:
    """Run a version-guarded project update and return the new `row_version`. Under the lock
    it changes exactly one row; if it changed none, the transaction rolls back and one re-read
    decides between `404` and `412`."""
    row = connection.execute(statement, parameters).first()
    if row is None:
        gone = connection.execute(PROJECT_ID, {"project_id": parameters["project_id"]}).first()
        raise _Abort("project_not_found" if gone is None else "precondition_failed")
    return bytes(row.row_version)


def _move_version(
    connection: Connection, *, project_id: UUID, row_version: bytes, caller: UUID
) -> bytes:
    """Step M."""
    return _moved(
        connection, MOVE_VERSION, {"project_id": project_id, "rv": row_version, "caller": caller}
    )


def _audit(connection: Connection, event: AuditEvent) -> AuditEvent:
    write_audit_event(connection, event)
    return event


async def _run[T](engine: SqlEngine, work: Any, name: str) -> T | Refused:
    try:
        result: T = await engine.run(work, name=name, retry="transient")
    except _Abort as abort:
        return Refused(abort.refusal)
    return result


# --- adminCreateProject ----------------------------------------------------------------------


def _create(
    connection: Connection, *, project: NewProject, caller: UUID
) -> ProjectWrite | UnknownMembers:
    connection.execute(SET_XACT_ABORT_ON)
    members = project.initial_member_ids
    if members:
        found = {
            row.object_id
            for row in connection.execute(LOCK_USERS, {"user_ids": list(members)}).all()
        }
        unknown = tuple(index for index, user_id in enumerate(members) if user_id not in found)
        if unknown:
            return UnknownMembers(unknown)
    connection.execute(
        INSERT_PROJECT,
        {
            "project_id": project.id,
            "name": project.name,
            "description": project.description,
            "instructions": project.instructions,
            "example_questions_json": _example_questions_json(project.example_questions),
            "is_default": project.is_default,
            "caller": caller,
        },
    )
    for user_id in members:
        connection.execute(
            INSERT_MEMBER, {"user_id": user_id, "project_id": project.id, "caller": caller}
        )
    events = [
        _audit(
            connection,
            AuditEvent(
                action="project.created",
                actor_type="user",
                actor_object_id=caller,
                target_type="project",
                target_id=str(project.id),
                project_id=project.id,
                metadata=ProjectCreatedMetadata(is_default=project.is_default),
            ),
        )
    ]
    events.extend(
        _audit(connection, _member_added(caller, project.id, user_id)) for user_id in members
    )
    return ProjectWrite(project=_read_project(connection, project.id), events=tuple(events))


def _member_added(caller: UUID, project_id: UUID, user_id: UUID) -> AuditEvent:
    return AuditEvent(
        action="project_member.added",
        actor_type="user",
        actor_object_id=caller,
        subject_object_id=user_id,
        target_type="project_member",
        target_id=str(user_id),
        project_id=project_id,
        metadata=ProjectMemberAddedMetadata(grant_source="admin"),
    )


@request_path(
    example_params={
        "project": NewProject(
            id=_EXAMPLE_PROJECT,
            name="Plan audit example",
            description="",
            instructions="",
            example_questions=(),
            is_default=False,
            initial_member_ids=(_EXAMPLE_USER,),
        ),
        "caller": _EXAMPLE_CALLER,
    }
)
async def create_project(
    engine: SqlEngine, *, project: NewProject, caller: UUID
) -> ProjectWrite | UnknownMembers | Refused:
    """`adminCreateProject` in one transaction: the initial members locked and checked (any
    unknown one answers `UnknownMembers` with nothing written), the project, one `admin`
    membership per initial member, the `project.created` row and one `project_member.added` row
    per member. A name already used by any project, archived ones included and compared
    case-insensitively, is `project_name_taken`."""
    try:
        return await engine.run(
            partial(_create, project=project, caller=caller),
            name=CREATE_PROJECT,
            retry="transient",
        )
    except SqlUniqueViolation as violation:
        if violation.constraint == PROJECT_NAME_CONSTRAINT:
            return Refused("project_name_taken")
        raise


# --- adminUpdateProject ----------------------------------------------------------------------


def _changed_fields(locked: _Locked, changes: ProjectChanges) -> list[ProjectField]:
    changed: list[ProjectField] = []
    for attribute, field in _PROJECT_FIELDS:
        value = getattr(changes, attribute)
        if value is not None and value != getattr(locked, attribute):
            changed.append(field)
    return changed


def _project_event(
    action: Literal["project.updated", "project.archived", "project.restored"],
    caller: UUID,
    project_id: UUID,
    metadata: ProjectUpdatedMetadata | ProjectArchivedMetadata | None,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_type="user",
        actor_object_id=caller,
        target_type="project",
        target_id=str(project_id),
        project_id=project_id,
        metadata=metadata,
    )


def _update(
    connection: Connection,
    *,
    project_id: UUID,
    changes: ProjectChanges,
    version: VersionCheck,
    caller: UUID,
) -> ProjectWrite | Refused:
    connection.execute(SET_XACT_ABORT_ON)
    locked = _lock_project(connection, project_id)
    if locked is None:
        return Refused("project_not_found")
    refused = _version_refusal(version, locked.row_version)
    if refused is not None:
        return refused
    status_change = changes.status is not None and changes.status != locked.status
    if status_change and changes.holds_fields():
        return Refused("status_change_with_fields")
    if locked.status == "archived" and not status_change:
        # Only the restore body changes an archived project; an archived project is read-only.
        return Refused("project_archived")
    guard = {"project_id": project_id, "rv": locked.row_version, "caller": caller}
    if status_change and changes.status == "archived":
        _moved(connection, ARCHIVE_PROJECT, guard)
        event = _project_event(
            "project.archived",
            caller,
            project_id,
            ProjectArchivedMetadata(was_default=locked.is_default),
        )
    elif status_change:
        _moved(connection, RESTORE_PROJECT, guard)
        event = _project_event("project.restored", caller, project_id, None)
    else:
        changed = _changed_fields(locked, changes)
        if not changed:
            return ProjectWrite(project=_read_project(connection, project_id), events=())
        questions = changes.example_questions
        _moved(
            connection,
            UPDATE_FIELDS,
            guard
            | {
                "name": locked.name if changes.name is None else changes.name,
                "description": (
                    locked.description if changes.description is None else changes.description
                ),
                "instructions": (
                    locked.instructions if changes.instructions is None else changes.instructions
                ),
                "example_questions_json": _example_questions_json(
                    locked.example_questions if questions is None else questions
                ),
                "is_default": locked.is_default
                if changes.is_default is None
                else changes.is_default,
            },
        )
        event = _project_event(
            "project.updated", caller, project_id, ProjectUpdatedMetadata(changed_fields=changed)
        )
    events = (_audit(connection, event),)
    return ProjectWrite(project=_read_project(connection, project_id), events=events)


@request_path(
    example_params={
        "project_id": _EXAMPLE_PROJECT,
        "changes": ProjectChanges(description="Plan audit example"),
        "version": _AnyVersion(),
        "caller": _EXAMPLE_CALLER,
    }
)
async def update_project(
    engine: SqlEngine,
    *,
    project_id: UUID,
    changes: ProjectChanges,
    version: VersionCheck,
    caller: UUID,
) -> ProjectWrite | Refused:
    """`adminUpdateProject` (section 3.7.5): the row locked (`404`), `If-Match` (`428`, `412`),
    a status change with other fields refused (`422`), an archived project changed only by the
    restore body (`409`), a body equal to the stored values answered unchanged; then the archive,
    the restore or the field edit with its audit row. A name already in use is
    `project_name_taken`."""
    try:
        return await _run(
            engine,
            partial(
                _update, project_id=project_id, changes=changes, version=version, caller=caller
            ),
            UPDATE_PROJECT,
        )
    except SqlUniqueViolation as violation:
        if violation.constraint == PROJECT_NAME_CONSTRAINT:
            return Refused("project_name_taken")
        raise


# --- adminAddProjectMember and adminRemoveProjectMember -------------------------------------


def _checked_project(
    connection: Connection, project_id: UUID, version: VersionCheck, lookup: Any = None
) -> _Locked | Refused:
    """Step V: the locked project (`404`), the operation's own lookup, the version (`428`,
    `412`), then `409 project_archived`."""
    locked = _lock_project(connection, project_id)
    if locked is None:
        return Refused("project_not_found")
    if lookup is not None:
        refused: Refused | None = lookup()
        if refused is not None:
            return refused
    version_refused = _version_refusal(version, locked.row_version)
    if version_refused is not None:
        return version_refused
    if locked.status == "archived":
        return Refused("project_archived")
    return locked


def _add(
    connection: Connection,
    *,
    project_id: UUID,
    user_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> MemberChange | Refused:
    connection.execute(SET_XACT_ABORT_ON)

    def user_lookup() -> Refused | None:
        found = connection.execute(LOCK_USER, {"user_id": user_id}).first()
        return Refused("user_not_found") if found is None else None

    locked = _checked_project(connection, project_id, version, user_lookup)
    if isinstance(locked, Refused):
        return locked
    keys = {"user_id": user_id, "project_id": project_id}
    inserted = connection.execute(ADD_MEMBER_IF_ABSENT, keys | {"caller": caller}).rowcount
    row_version = locked.row_version
    events: tuple[AuditEvent, ...] = ()
    if inserted:
        events = (_audit(connection, _member_added(caller, project_id, user_id)),)
        row_version = _move_version(
            connection, project_id=project_id, row_version=row_version, caller=caller
        )
    member = _member_row(connection.execute(ONE_MEMBER, keys).one())
    return MemberChange(
        member=member, project_row_version=row_version, created=bool(inserted), events=events
    )


@request_path(
    example_params={
        "project_id": _EXAMPLE_PROJECT,
        "user_id": _EXAMPLE_USER,
        "version": _AnyVersion(),
        "caller": _EXAMPLE_CALLER,
    }
)
async def add_member(
    engine: SqlEngine,
    *,
    project_id: UUID,
    user_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> MemberChange | Refused:
    """`adminAddProjectMember` (section 3.7.7): V with the user locked (`404 user_not_found`),
    then the membership unless one exists from either source; a new one writes
    `project_member.added` and moves the project's version."""
    work = partial(_add, project_id=project_id, user_id=user_id, version=version, caller=caller)
    return await _run(engine, work, ADD_MEMBER)


def _remove(
    connection: Connection,
    *,
    project_id: UUID,
    user_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> PartRemoval | Refused:
    connection.execute(SET_XACT_ABORT_ON)
    locked = _checked_project(connection, project_id, version)
    if isinstance(locked, Refused):
        return locked
    removed = connection.execute(
        REMOVE_MEMBERSHIP, {"user_id": user_id, "project_id": project_id}
    ).first()
    if removed is None:
        return PartRemoval(removed=False, project_row_version=locked.row_version, events=())
    event = AuditEvent(
        action="project_member.removed",
        actor_type="user",
        actor_object_id=caller,
        subject_object_id=user_id,
        target_type="project_member",
        target_id=str(user_id),
        project_id=project_id,
        metadata=ProjectMemberRemovedMetadata(grant_source=removed.grant_source),
    )
    events = (_audit(connection, event),)
    row_version = _move_version(
        connection, project_id=project_id, row_version=locked.row_version, caller=caller
    )
    return PartRemoval(removed=True, project_row_version=row_version, events=events)


@request_path(
    example_params={
        "project_id": _EXAMPLE_PROJECT,
        "user_id": _EXAMPLE_USER,
        "version": _AnyVersion(),
        "caller": _EXAMPLE_CALLER,
    }
)
async def remove_member(
    engine: SqlEngine,
    *,
    project_id: UUID,
    user_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> PartRemoval | Refused:
    """`adminRemoveProjectMember` (section 3.7.8): V, then the membership deleted; a deleted row
    writes `project_member.removed` with its grant source and moves the project's version, and
    no row answers `removed: false` with the unchanged version."""
    work = partial(_remove, project_id=project_id, user_id=user_id, version=version, caller=caller)
    return await _run(engine, work, REMOVE_MEMBER)


# --- adminMapProjectIndex and adminUnmapProjectIndex -----------------------------------------


def _mapping_event(
    action: Literal["project_index.mapped", "project_index.default_set", "project_index.unmapped"],
    caller: UUID,
    project_id: UUID,
    index_id: UUID,
    metadata: ProjectIndexMappedMetadata
    | ProjectIndexDefaultSetMetadata
    | ProjectIndexUnmappedMetadata,
) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor_type="user",
        actor_object_id=caller,
        target_type="project_index",
        target_id=str(index_id),
        project_id=project_id,
        metadata=metadata,
    )


def _map(
    connection: Connection,
    *,
    project_id: UUID,
    index_id: UUID,
    is_default: bool,
    version: VersionCheck,
    caller: UUID,
) -> MappingChange | Refused:
    connection.execute(SET_XACT_ABORT_ON)
    index_status: list[str] = []

    def index_lookup() -> Refused | None:
        row = connection.execute(LOCK_INDEX, {"index_id": index_id}).first()
        if row is None or row.status == "deleted":
            return Refused("index_not_found")
        index_status.append(row.status)
        return None

    locked = _checked_project(connection, project_id, version, index_lookup)
    if isinstance(locked, Refused):
        return locked
    if index_status[0] not in MAPPABLE_STATUSES:
        return Refused("index_not_mappable")
    mappings = {
        row.search_index_id: bool(row.is_default)
        for row in connection.execute(PROJECT_MAPPINGS, {"project_id": project_id}).all()
    }
    current_default = next((key for key, default in mappings.items() if default), None)
    existing = mappings.get(index_id)
    if not is_default and (not mappings or existing is True):
        # The first mapping must be the default, and the default can't be unset directly.
        return Refused("default_index_required")
    keys = {"project_id": project_id, "index_id": index_id}
    events: list[AuditEvent] = []
    created = existing is None
    if existing is not None and existing == is_default:
        changed = False
    else:
        changed = True
        if is_default and current_default is not None:
            connection.execute(CLEAR_DEFAULT, {MAPPED_PROJECT: project_id, "caller": caller})
        if created:
            connection.execute(INSERT_MAPPING, keys | {"is_default": is_default, "caller": caller})
            events.append(
                _audit(
                    connection,
                    _mapping_event(
                        "project_index.mapped",
                        caller,
                        project_id,
                        index_id,
                        ProjectIndexMappedMetadata(is_default=is_default),
                    ),
                )
            )
        else:
            connection.execute(
                SET_DEFAULT, {MAPPED_PROJECT: project_id, "index_id": index_id, "caller": caller}
            )
        if is_default and current_default is not None:
            events.append(
                _audit(
                    connection,
                    _mapping_event(
                        "project_index.default_set",
                        caller,
                        project_id,
                        index_id,
                        ProjectIndexDefaultSetMetadata(previous_default_index_id=current_default),
                    ),
                )
            )
    row_version = locked.row_version
    if changed:
        row_version = _move_version(
            connection, project_id=project_id, row_version=row_version, caller=caller
        )
    mapping = _mapping_row(connection.execute(ONE_MAPPING, keys).one())
    return MappingChange(
        mapping=mapping, project_row_version=row_version, created=created, events=tuple(events)
    )


@request_path(
    example_params={
        "project_id": _EXAMPLE_PROJECT,
        "index_id": _EXAMPLE_INDEX,
        "is_default": True,
        "version": _AnyVersion(),
        "caller": _EXAMPLE_CALLER,
    }
)
async def map_index(
    engine: SqlEngine,
    *,
    project_id: UUID,
    index_id: UUID,
    is_default: bool,
    version: VersionCheck,
    caller: UUID,
) -> MappingChange | Refused:
    """`adminMapProjectIndex` (section 3.9.2): V with the logical index locked (missing or
    `deleted`: `404 index_not_found`); `failed` and `deleting` are `409 index_not_mappable`;
    then the default rules: a project's first mapping and its current default can't be
    non-default (`409 default_index_required`), and a new default first clears the old one. The
    created mapping writes `project_index.mapped`; becoming the default while another mapping was
    writes `project_index.default_set`; any change moves the project's version."""
    work = partial(
        _map,
        project_id=project_id,
        index_id=index_id,
        is_default=is_default,
        version=version,
        caller=caller,
    )
    return await _run(engine, work, MAP_INDEX)


def _unmap(
    connection: Connection,
    *,
    project_id: UUID,
    index_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> PartRemoval | Refused:
    connection.execute(SET_XACT_ABORT_ON)
    locked = _checked_project(connection, project_id, version)
    if isinstance(locked, Refused):
        return locked
    mappings = {
        row.search_index_id: bool(row.is_default)
        for row in connection.execute(PROJECT_MAPPINGS, {"project_id": project_id}).all()
    }
    was_default = mappings.get(index_id)
    if was_default is None:
        return PartRemoval(removed=False, project_row_version=locked.row_version, events=())
    if was_default and len(mappings) > 1:
        return Refused("default_index_in_use")
    keys = {"project_id": project_id, "index_id": index_id}
    overrides = int(connection.execute(MAPPING_OVERRIDES, keys).scalar_one())
    connection.execute(DELETE_MAPPING, keys)
    event = _mapping_event(
        "project_index.unmapped",
        caller,
        project_id,
        index_id,
        ProjectIndexUnmappedMetadata(was_default=was_default, overrides_removed=overrides),
    )
    events = (_audit(connection, event),)
    row_version = _move_version(
        connection, project_id=project_id, row_version=locked.row_version, caller=caller
    )
    return PartRemoval(removed=True, project_row_version=row_version, events=events)


@request_path(
    example_params={
        "project_id": _EXAMPLE_PROJECT,
        "index_id": _EXAMPLE_INDEX,
        "version": _AnyVersion(),
        "caller": _EXAMPLE_CALLER,
    }
)
async def unmap_index(
    engine: SqlEngine,
    *,
    project_id: UUID,
    index_id: UUID,
    version: VersionCheck,
    caller: UUID,
) -> PartRemoval | Refused:
    """`adminUnmapProjectIndex` (section 3.9.3): V; an absent mapping answers `removed: false`
    with the unchanged version; the default while other mappings exist is
    `409 default_index_in_use` (the only mapping can always go); then the overrides on it are
    counted, the mapping deleted (its overrides cascade), `project_index.unmapped` written and the
    project's version moved."""
    work = partial(_unmap, project_id=project_id, index_id=index_id, version=version, caller=caller)
    return await _run(engine, work, UNMAP_INDEX)
