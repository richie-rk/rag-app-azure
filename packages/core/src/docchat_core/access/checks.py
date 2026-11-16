"""The access functions (`11-api.md` sections 1.4.2 to 1.4.5; `82-security.md` SEC-11 to SEC-14
and SEC-21; ADR 0020).

Deny by default, in one module: every function returns a positive result or raises, none returns
a boolean a caller could ignore, and none takes a user identifier other than the validated
`Principal`. Each check is one fresh SQL statement (`docchat_core.sql.repositories.access`):
memberships, overrides, mappings and project status are never cached, so a removed membership,
an archive or an unmapping acts on the next request. Only the index records of
`resolve_chat_index` are cached, for at most 60 seconds.

A project is reachable when its row exists, its status is `active`, and the caller holds `Admin`
or a membership row for it; an archived project is unreachable on every user path for everyone,
administrators included. An unreachable project raises the same `ProjectNotFound` as an absent
one. SQL failures propagate as the data-access layer's `SqlError`, which the API answers with
`503 dependency_unavailable` (`SqlUnavailable`, `SqlTimeout`), so a check fails closed.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from docchat_core.access import telemetry
from docchat_core.access.cache import IndexRecordCache
from docchat_core.access.exceptions import AdminRequired, ProjectIndexUnavailable, ProjectNotFound
from docchat_core.access.models import (
    MAX_PAGE_LIMIT,
    AccessibleProject,
    IndexRecord,
    IndexSource,
    PhysicalIndex,
    ProjectAccess,
    ProjectSummaryPage,
    ProjectSummaryRow,
    ResolvedIndex,
    example_questions_from,
)
from docchat_core.access.telemetry import IndexCache
from docchat_core.auth import Principal
from docchat_core.models.clock import Clock
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories import access as queries

PROJECT_ACTIVE: Final = "active"


def require_admin(principal: Principal) -> None:
    """Step 4 of every `/v1/admin` route, before any validation or lookup: `AdminRequired`
    unless the token grants `Admin`."""
    if not principal.is_admin:
        raise AdminRequired()


class AccessChecks:
    """One per process, built in the lifespan with the data-access layer; it owns the
    index-record cache."""

    def __init__(
        self,
        engine: SqlEngine,
        *,
        clock: Clock | None = None,
        cache: IndexRecordCache | None = None,
    ) -> None:
        self._engine = engine
        self._cache = IndexRecordCache(clock) if cache is None else cache

    async def authorize_project_for_chat(
        self, principal: Principal, project_id: UUID
    ) -> AccessibleProject:
        """Q1: the project when it is active and the caller is an administrator or a member;
        `ProjectNotFound` otherwise, the same for an absent, archived or unreachable project."""
        with telemetry.authorize_span(project_id) as span:
            row = await queries.read_project_for_chat(
                self._engine, oid=principal.oid, project_id=project_id
            )
            via: ProjectAccess | None = None
            if row is not None and row.status == PROJECT_ACTIVE:
                if principal.is_admin:
                    via = "admin"
                elif row.is_member:
                    via = "member"
            telemetry.record_authorize(span, via)
        # Raised outside the span: a refusal is the check's outcome, not an error.
        if row is None or via is None:
            raise ProjectNotFound()
        return AccessibleProject(
            id=row.id,
            name=row.name,
            description=row.description,
            instructions=row.instructions,
            example_questions=example_questions_from(row.example_questions_json),
            access=via,
        )

    async def resolve_chat_index(
        self, principal: Principal, project: AccessibleProject
    ) -> ResolvedIndex:
        """Q2, then the index record (Q3 on a cache miss): the caller's unexpired override for
        the project, else the project's default mapping, resolved to its logical index's active
        physical index. `ProjectIndexUnavailable` with neither, or with no active physical
        index; a logical index being re-ingested or rebuilt still serves from its active one."""
        resolved: ResolvedIndex | None = None
        with telemetry.resolve_span(project.id) as span:
            choice = await queries.read_index_choice(
                self._engine, oid=principal.oid, project_id=project.id
            )
            chosen = _chosen(choice.override_index_id, choice.default_index_id)
            cache: IndexCache | None = None
            if chosen is not None:
                search_index_id, source = chosen
                telemetry.record_choice(span, search_index_id, source)
                record, cache = await self._index_record(search_index_id)
                if record is not None:
                    resolved = ResolvedIndex(
                        search_index_id=search_index_id,
                        source=source,
                        override_expires_at=(
                            choice.override_expires_at if source == "user_override" else None
                        ),
                        physical_index=record.physical_index,
                    )
            telemetry.record_resolution(span, resolved=resolved is not None, cache=cache)
        if resolved is None:
            raise ProjectIndexUnavailable()
        if resolved.source == "user_override" and resolved.override_expires_at is not None:
            telemetry.report_override_used(
                principal.oid, project.id, resolved.search_index_id, resolved.override_expires_at
            )
        return resolved

    async def _index_record(self, search_index_id: UUID) -> tuple[IndexRecord | None, IndexCache]:
        cached = self._cache.get(search_index_id)
        if cached is not None:
            return cached, "hit"
        row = await queries.read_index_record(self._engine, search_index_id=search_index_id)
        if row is None or row.physical_index is None:
            return None, "miss"
        physical = row.physical_index
        record = IndexRecord(
            search_index_id=row.search_index_id,
            status=row.status,
            physical_index=PhysicalIndex(
                id=physical.id,
                name=physical.name,
                schema_version=physical.schema_version,
                embedding_model=physical.embedding_model,
                embedding_dimensions=physical.embedding_dimensions,
                query_embedding_deployment=physical.query_embedding_deployment,
            ),
        )
        self._cache.put(record)
        return record, "miss"

    async def list_reachable_project_ids(self, principal: Principal) -> frozenset[UUID]:
        """Q4: every active project for an administrator, the active projects with a membership
        row for anyone else; possibly empty."""
        with telemetry.reachable_span() as span:
            if principal.is_admin:
                ids = await queries.read_active_project_ids(self._engine)
            else:
                ids = await queries.read_member_project_ids(self._engine, oid=principal.oid)
            telemetry.record_reachable(span, len(ids))
        return ids

    async def list_my_projects(
        self, principal: Principal, after: str | None, limit: int
    ) -> ProjectSummaryPage:
        """Q5: up to `limit` reachable projects ordered by name after the name `after` (the
        decoded cursor), with whether each project's default resolves to an active physical
        index."""
        if not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"limit must lie within 1 to {MAX_PAGE_LIMIT}")
        if principal.is_admin:
            rows = await queries.read_all_active_projects(
                self._engine, after_name=after, limit_plus_one=limit + 1
            )
        else:
            rows = await queries.read_member_projects(
                self._engine, oid=principal.oid, after_name=after, limit_plus_one=limit + 1
            )
        items = tuple(
            ProjectSummaryRow(
                id=row.id,
                name=row.name,
                description=row.description,
                example_questions=example_questions_from(row.example_questions_json),
                has_default_index=row.has_default_index,
            )
            for row in rows[:limit]
        )
        next_after = items[-1].name if len(rows) > limit else None
        return ProjectSummaryPage(items=items, next_after=next_after)


def _chosen(
    override_index_id: UUID | None, default_index_id: UUID | None
) -> tuple[UUID, IndexSource] | None:
    # An unexpired override wins over the default; an override whose index has no active
    # physical index refuses rather than falling back, so a tester never silently gets the
    # default.
    if override_index_id is not None:
        return override_index_id, "user_override"
    if default_index_id is not None:
        return default_index_id, "project_default"
    return None
