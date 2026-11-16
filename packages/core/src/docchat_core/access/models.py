"""The positive results of the access functions (`11-api.md` section 1.4.3; ADR 0020 item 1).

A caller holds an `AccessibleProject` only after `authorize_project_for_chat` allowed it, and a
`ResolvedIndex` only after `resolve_chat_index` found an active physical index for that project,
so code that needs either can't skip the check. `ProjectSummaryPage` holds the rows of
`listMyProjects`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

type ProjectAccess = Literal["admin", "member"]
"""How the caller reaches a project; `admin` when both apply."""

type IndexSource = Literal["user_override", "project_default"]
"""Where the chat index came from, the value the stored turn trace and the dashboards use."""

MAX_PAGE_LIMIT: Final = 100


@dataclass(frozen=True, slots=True)
class AccessibleProject:
    """An active project the caller reaches, read in the same round trip as the check."""

    id: UUID
    name: str
    description: str
    instructions: str
    example_questions: tuple[str, ...]
    access: ProjectAccess


@dataclass(frozen=True, slots=True)
class PhysicalIndex:
    """The active physical index chat searches."""

    id: UUID
    name: str
    schema_version: int
    embedding_model: str
    embedding_dimensions: int
    query_embedding_deployment: str


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """A logical index's status and its active physical index: what the index-record cache
    holds. A record exists only for a logical index that has an active physical index."""

    search_index_id: UUID
    status: str
    physical_index: PhysicalIndex


@dataclass(frozen=True, slots=True)
class ResolvedIndex:
    """The index a chat turn searches, with its source and the override's expiry."""

    search_index_id: UUID
    source: IndexSource
    override_expires_at: datetime | None
    physical_index: PhysicalIndex


@dataclass(frozen=True, slots=True)
class ProjectSummaryRow:
    """One `ProjectSummary` of `listMyProjects`."""

    id: UUID
    name: str
    description: str
    example_questions: tuple[str, ...]
    has_default_index: bool


@dataclass(frozen=True, slots=True)
class ProjectSummaryPage:
    """A page of `listMyProjects`; `next_after` is the last item's name when another page
    exists, the value the next cursor carries."""

    items: tuple[ProjectSummaryRow, ...]
    next_after: str | None


def example_questions_from(text: str) -> tuple[str, ...]:
    """The `example_questions_json` column as a tuple. The check constraint keeps it a JSON
    array and the API writes only strings, so anything else is a broken row and raises."""
    value = json.loads(text)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("example_questions_json isn't an array of strings")
    return tuple(value)
