"""The authorisation signals (`85-observability.md` sections 4.3, 4.8 and 4.12; ADR 0020 item
10): the spans `docchat.access.authorize_project`, `docchat.access.resolve_index` and
`docchat.access.reachable_projects`, the counters `docchat.access.denials` and
`docchat.access.index_overrides_used`, and the custom event `index_override_used` behind alert
A40.

The access functions don't know the operation they serve, so the API records each denial with
its operationId through `record_denial` when it answers `role_not_assigned`, `admin_required` or
a `404` of a scoped parameter. The instruments are created once, at import.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from opentelemetry.trace import Span

from docchat_core.access.models import IndexSource, ProjectAccess
from docchat_core.models.utc import format_utc
from docchat_core.telemetry import emit_event, set_attributes, start_span
from docchat_core.telemetry.instruments import counter

type DenialKind = Literal["role_not_assigned", "admin_required", "not_found"]
type IndexCache = Literal["hit", "miss"]

AUTHORIZE_SPAN: Final = "docchat.access.authorize_project"
RESOLVE_SPAN: Final = "docchat.access.resolve_index"
REACHABLE_SPAN: Final = "docchat.access.reachable_projects"
OVERRIDE_EVENT: Final = "index_override_used"

PROJECT_ATTRIBUTE: Final = "docchat.project_id"
INDEX_ATTRIBUTE: Final = "docchat.index_id"
OUTCOME_ATTRIBUTE: Final = "docchat.access.outcome"
VIA_ATTRIBUTE: Final = "docchat.access.via"
SOURCE_ATTRIBUTE: Final = "docchat.access.index_source"
CACHE_ATTRIBUTE: Final = "docchat.access.index_cache"
PROJECT_COUNT_ATTRIBUTE: Final = "docchat.access.project_count"

DENIALS: Final = counter("docchat.access.denials")
OVERRIDES_USED: Final = counter("docchat.access.index_overrides_used")


def authorize_span(project_id: UUID) -> AbstractContextManager[Span]:
    return start_span(AUTHORIZE_SPAN, **{PROJECT_ATTRIBUTE: str(project_id)})


def record_authorize(span: Span, via: ProjectAccess | None) -> None:
    """`allowed` with how the caller reaches the project, or `not_found` (`via` `None`)."""
    if via is None:
        set_attributes(span, **{OUTCOME_ATTRIBUTE: "not_found"})
    else:
        set_attributes(span, **{OUTCOME_ATTRIBUTE: "allowed", VIA_ATTRIBUTE: via})


def resolve_span(project_id: UUID) -> AbstractContextManager[Span]:
    return start_span(RESOLVE_SPAN, **{PROJECT_ATTRIBUTE: str(project_id)})


def record_choice(span: Span, search_index_id: UUID, source: IndexSource) -> None:
    """The logical index the override or the default mapping chose."""
    set_attributes(span, **{INDEX_ATTRIBUTE: str(search_index_id), SOURCE_ATTRIBUTE: source})


def record_resolution(span: Span, *, resolved: bool, cache: IndexCache | None) -> None:
    """`ok` or `unavailable`, with whether the index record came from the cache (`None` when
    nothing was chosen to look up)."""
    attributes: dict[str, object] = {OUTCOME_ATTRIBUTE: "ok" if resolved else "unavailable"}
    if cache is not None:
        attributes[CACHE_ATTRIBUTE] = cache
    set_attributes(span, **attributes)


def report_override_used(
    oid: UUID, project_id: UUID, search_index_id: UUID, expires_at: datetime
) -> None:
    """A chat turn served from a per-user override: the counter and the event of alert A40."""
    OVERRIDES_USED.add(1)
    emit_event(
        OVERRIDE_EVENT,
        oid=str(oid),
        projectId=str(project_id),
        indexId=str(search_index_id),
        expiresAt=format_utc(expires_at),
    )


def reachable_span() -> AbstractContextManager[Span]:
    return start_span(REACHABLE_SPAN)


def record_reachable(span: Span, project_count: int) -> None:
    set_attributes(span, **{PROJECT_COUNT_ATTRIBUTE: project_count})


def record_denial(kind: DenialKind, operation: str) -> None:
    """One refused request of `operation` (its operationId), for the access dashboard and alert
    A37."""
    DENIALS.add(1, kind=kind, operation=operation)
