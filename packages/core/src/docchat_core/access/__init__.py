"""Authorisation: the access functions, `AccessibleProject`, `ResolvedIndex` and the 60-second
index-record cache (`11-api.md` section 1.4; `01-repository.md` section 2.2; ADR 0020)."""

from docchat_core.access.cache import CACHE_CAPACITY, CACHE_LIFETIME_SECONDS, IndexRecordCache
from docchat_core.access.checks import AccessChecks, require_admin
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
)
from docchat_core.access.telemetry import DenialKind, record_denial

__all__ = [
    "CACHE_CAPACITY",
    "CACHE_LIFETIME_SECONDS",
    "MAX_PAGE_LIMIT",
    "AccessChecks",
    "AccessibleProject",
    "AdminRequired",
    "DenialKind",
    "IndexRecord",
    "IndexRecordCache",
    "IndexSource",
    "PhysicalIndex",
    "ProjectAccess",
    "ProjectIndexUnavailable",
    "ProjectNotFound",
    "ProjectSummaryPage",
    "ProjectSummaryRow",
    "ResolvedIndex",
    "record_denial",
    "require_admin",
]
