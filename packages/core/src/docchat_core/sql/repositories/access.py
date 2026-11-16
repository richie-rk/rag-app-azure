"""The access queries Q1 to Q5 (`11-api.md` section 1.4.4; ADR 0020 item 7), which
`docchat_core.access` runs on every request that needs them.

Each is one parameterised statement over index seeks, built once at import as one constant text
per case (the administrator and member forms, with and without a cursor), never with an
`:x IS NULL OR …` predicate, so every plan stays a seek. Every value is a bound parameter; the
literals `1` of `is_default = 1` and `N'active'` stay in the text so SQL Server can match the
filtered and status indexes. The functions return rows; `docchat_core.access` decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, Select, and_, bindparam, case, literal_column, select

from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, ONE, ZERO, top_rows, utc
from docchat_core.sql.tables.access import (
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
)
from docchat_core.sql.tables.ingestion import physical_indexes, search_indexes
from docchat_core.sql.tables.schema import BIT_TRUE, SYSUTCDATETIME, nvarchar_literal

ACTIVE: Final = nvarchar_literal("active")

# Statement-set names: the unit of work's span `docchat.sql.<name>` and its metrics.
AUTHORIZE_PROJECT: Final = "authorize_project"
RESOLVE_INDEX: Final = "resolve_index"
READ_INDEX_RECORD: Final = "read_index_record"
REACHABLE_PROJECTS: Final = "reachable_projects"
LIST_MY_PROJECTS: Final = "list_my_projects"

# Fixed identifiers for the plan audit: a plan doesn't depend on whether the rows exist.
_EXAMPLE_USER: Final = UUID("6f1c2a4e-8b3d-4e5f-9a7b-1c2d3e4f5a60")
_EXAMPLE_PROJECT: Final = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
_EXAMPLE_INDEX: Final = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
_EXAMPLE_PAGE: Final = 51


# --- Q1 ------------------------------------------------------------------------------------


def _q1() -> Select[Any]:
    p = projects.alias("p")
    m = project_members.alias("m")
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.description,
            p.c.instructions,
            p.c.example_questions_json,
            p.c.status,
            case((m.c.user_object_id.is_(None), ZERO), else_=ONE).label("is_member"),
        )
        .select_from(
            p.outerjoin(m, and_(m.c.user_object_id == bindparam("oid"), m.c.project_id == p.c.id))
        )
        .where(p.c.id == bindparam("project_id"))
    )


PROJECT_FOR_CHAT: Final = _q1()
"""Q1: the project row and whether the caller has a membership row for it (seeks on
`PK_projects` and `PK_project_members`)."""


@dataclass(frozen=True, slots=True)
class ProjectForChatRow:
    id: UUID
    name: str
    description: str
    instructions: str
    example_questions_json: str
    status: str
    is_member: bool


@request_path(example_params={"oid": _EXAMPLE_USER, "project_id": _EXAMPLE_PROJECT})
async def read_project_for_chat(
    engine: SqlEngine, *, oid: UUID, project_id: UUID
) -> ProjectForChatRow | None:
    """Q1; `None` when no project has this ID."""
    row = await engine.fetch_one(
        PROJECT_FOR_CHAT, {"oid": oid, "project_id": project_id}, name=AUTHORIZE_PROJECT
    )
    if row is None:
        return None
    return ProjectForChatRow(
        id=row.id,
        name=row.name,
        description=row.description,
        instructions=row.instructions,
        example_questions_json=row.example_questions_json,
        status=row.status,
        is_member=row.is_member == 1,
    )


# --- Q2 ------------------------------------------------------------------------------------


def _q2() -> Select[Any]:
    anchor = select(literal_column("1").label("anchor")).subquery("a")
    o = user_index_overrides.alias("o")
    d = project_indexes.alias("d")
    return select(
        o.c.search_index_id.label("override_index_id"),
        o.c.expires_at.label("override_expires_at"),
        d.c.search_index_id.label("default_index_id"),
    ).select_from(
        anchor.outerjoin(
            o,
            and_(
                o.c.user_object_id == bindparam("oid"),
                o.c.project_id == bindparam("project_id"),
                o.c.expires_at > SYSUTCDATETIME,
            ),
        ).outerjoin(d, and_(d.c.project_id == bindparam("project_id"), d.c.is_default == BIT_TRUE))
    )


INDEX_CHOICE: Final = _q2()
"""Q2: the caller's unexpired override and the project's default mapping, from a one-row anchor
(seeks on `PK_user_index_overrides` and `UX_project_indexes_default`)."""


@dataclass(frozen=True, slots=True)
class IndexChoiceRow:
    override_index_id: UUID | None
    override_expires_at: datetime | None
    default_index_id: UUID | None


@request_path(example_params={"oid": _EXAMPLE_USER, "project_id": _EXAMPLE_PROJECT})
async def read_index_choice(engine: SqlEngine, *, oid: UUID, project_id: UUID) -> IndexChoiceRow:
    """Q2; the anchor always yields one row, with `None` for each branch that has no row."""
    row = await engine.fetch_one(
        INDEX_CHOICE, {"oid": oid, "project_id": project_id}, name=RESOLVE_INDEX
    )
    if row is None:
        return IndexChoiceRow(None, None, None)
    expires_at = row.override_expires_at
    return IndexChoiceRow(
        override_index_id=row.override_index_id,
        override_expires_at=None if expires_at is None else utc(expires_at),
        default_index_id=row.default_index_id,
    )


# --- Q3 ------------------------------------------------------------------------------------


def _q3() -> Select[Any]:
    s = search_indexes.alias("s")
    p = physical_indexes.alias("p")
    return (
        select(
            s.c.id,
            s.c.status,
            p.c.id.label("physical_index_id"),
            p.c.name,
            p.c.schema_version,
            p.c.embedding_model,
            p.c.embedding_dimensions,
            p.c.query_embedding_deployment,
        )
        .select_from(
            s.outerjoin(p, and_(p.c.id == s.c.active_physical_index_id, p.c.state == ACTIVE))
        )
        .where(s.c.id == bindparam("search_index_id"))
    )


INDEX_RECORD: Final = _q3()
"""Q3: a logical index's status and its active physical index (primary-key seeks)."""


@dataclass(frozen=True, slots=True)
class PhysicalIndexRow:
    id: UUID
    name: str
    schema_version: int
    embedding_model: str
    embedding_dimensions: int
    query_embedding_deployment: str


@dataclass(frozen=True, slots=True)
class IndexRecordRow:
    search_index_id: UUID
    status: str
    physical_index: PhysicalIndexRow | None


@request_path(example_params={"search_index_id": _EXAMPLE_INDEX})
async def read_index_record(engine: SqlEngine, *, search_index_id: UUID) -> IndexRecordRow | None:
    """Q3; `None` when no logical index has this ID, and `physical_index` `None` when it has no
    active physical index."""
    row = await engine.fetch_one(
        INDEX_RECORD, {"search_index_id": search_index_id}, name=READ_INDEX_RECORD
    )
    if row is None:
        return None
    physical = None
    if row.physical_index_id is not None:
        physical = PhysicalIndexRow(
            id=row.physical_index_id,
            name=row.name,
            schema_version=row.schema_version,
            embedding_model=row.embedding_model,
            embedding_dimensions=row.embedding_dimensions,
            query_embedding_deployment=row.query_embedding_deployment,
        )
    return IndexRecordRow(search_index_id=row.id, status=row.status, physical_index=physical)


# --- Q4 ------------------------------------------------------------------------------------


def _q4_admin() -> Select[Any]:
    p = projects.alias("p")
    return select(p.c.id).where(p.c.status == ACTIVE)


def _q4_member() -> Select[Any]:
    m = project_members.alias("m")
    p = projects.alias("p")
    return (
        select(m.c.project_id)
        .select_from(m.join(p, p.c.id == m.c.project_id))
        .where(m.c.user_object_id == bindparam("oid"), p.c.status == ACTIVE)
    )


ACTIVE_PROJECT_IDS: Final = _q4_admin()
"""Q4, administrator form: every active project (seek on `IX_projects_status_name`)."""

MEMBER_PROJECT_IDS: Final = _q4_member()
"""Q4, member form: the caller's memberships of active projects."""


@request_path(example_params={})
async def read_active_project_ids(engine: SqlEngine) -> frozenset[UUID]:
    """Q4 for an administrator."""
    rows = await engine.fetch_all(ACTIVE_PROJECT_IDS, name=REACHABLE_PROJECTS)
    return frozenset(row.id for row in rows)


@request_path(example_params={"oid": _EXAMPLE_USER})
async def read_member_project_ids(engine: SqlEngine, *, oid: UUID) -> frozenset[UUID]:
    """Q4 for anyone else."""
    rows = await engine.fetch_all(MEMBER_PROJECT_IDS, {"oid": oid}, name=REACHABLE_PROJECTS)
    return frozenset(row.project_id for row in rows)


# --- Q5 ------------------------------------------------------------------------------------


def _q5(*, admin: bool, cursor: bool) -> Select[Any]:
    p = projects.alias("p")
    d = project_indexes.alias("d")
    s = search_indexes.alias("s")
    ph = physical_indexes.alias("ph")
    m = project_members.alias("m")
    reachable = p if admin else m.join(p, p.c.id == m.c.project_id)
    source = (
        reachable.outerjoin(d, and_(d.c.project_id == p.c.id, d.c.is_default == BIT_TRUE))
        .outerjoin(s, s.c.id == d.c.search_index_id)
        .outerjoin(ph, and_(ph.c.id == s.c.active_physical_index_id, ph.c.state == ACTIVE))
    )
    conditions = [p.c.status == ACTIVE]
    if not admin:
        conditions.insert(0, m.c.user_object_id == bindparam("oid"))
    if cursor:
        conditions.append(p.c.name > bindparam("after_name"))
    # The administrator form seeks `IX_projects_status_name`, which holds every column of `p` the
    # page selects (`80-data-model.md` section 2.3), so a page reads `limit_plus_one` rows in name
    # order with no lookup and no sort. The member form starts from the caller's memberships
    # (`PK_project_members`).
    return top_rows(
        select(
            p.c.id,
            p.c.name,
            p.c.description,
            p.c.example_questions_json,
            case((ph.c.id.is_not(None), ONE), else_=ZERO).label("has_default_index"),
        )
        .select_from(source)
        .where(*conditions)
        .order_by(p.c.name)
    )


MY_PROJECTS: Final[Mapping[tuple[bool, bool], Select[Any]]] = MappingProxyType(
    {
        (admin, cursor): _q5(admin=admin, cursor=cursor)
        for admin in (True, False)
        for cursor in (False, True)
    }
)
"""Q5 by (administrator form, with cursor): a page of reachable projects ordered by name, with
whether the default mapping resolves to an active physical index, exactly as Q2's default
branch and Q3 would."""


@dataclass(frozen=True, slots=True)
class MyProjectRow:
    id: UUID
    name: str
    description: str
    example_questions_json: str
    has_default_index: bool


def _my_project(row: Row[Any]) -> MyProjectRow:
    return MyProjectRow(
        id=row.id,
        name=row.name,
        description=row.description,
        example_questions_json=row.example_questions_json,
        has_default_index=row.has_default_index == 1,
    )


@request_path(example_params={"after_name": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE})
async def read_all_active_projects(
    engine: SqlEngine, *, after_name: str | None, limit_plus_one: int
) -> list[MyProjectRow]:
    """Q5 for an administrator: every active project after `after_name`."""
    parameters: dict[str, object] = {LIMIT_PLUS_ONE: limit_plus_one}
    if after_name is not None:
        parameters["after_name"] = after_name
    rows = await engine.fetch_all(
        MY_PROJECTS[(True, after_name is not None)], parameters, name=LIST_MY_PROJECTS
    )
    return [_my_project(row) for row in rows]


@request_path(
    example_params={"oid": _EXAMPLE_USER, "after_name": None, LIMIT_PLUS_ONE: _EXAMPLE_PAGE}
)
async def read_member_projects(
    engine: SqlEngine, *, oid: UUID, after_name: str | None, limit_plus_one: int
) -> list[MyProjectRow]:
    """Q5 for anyone else: the caller's active projects after `after_name`."""
    parameters: dict[str, object] = {"oid": oid, LIMIT_PLUS_ONE: limit_plus_one}
    if after_name is not None:
        parameters["after_name"] = after_name
    rows = await engine.fetch_all(
        MY_PROJECTS[(False, after_name is not None)], parameters, name=LIST_MY_PROJECTS
    )
    return [_my_project(row) for row in rows]
