"""`listMyProjects` — `GET /v1/projects` (`11-api.md` sections 1.8, 3.2.2 and 4.4).

The projects the caller can reach now, ordered by name: every active project for an
administrator, the active projects with a membership row for anyone else. One index-backed query
(Q5, `docchat_core.access`) also resolves each project's default mapping to its active physical
index, so `hasDefaultIndex` is true exactly when `createChatTurn` would resolve the project's
default. An empty first page is the web client's "No projects" state.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field, StringConstraints

from docchat_api.http.access import access
from docchat_api.http.models import QueryModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor, encode_cursor
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_core.access import MAX_PAGE_LIMIT, ProjectSummaryRow

router = APIRouter(route_class=DocChatRoute)

DEFAULT_LIMIT = 50
PROJECT_NAME_MAX_LENGTH = 80
DESCRIPTION_MAX_LENGTH = 400
EXAMPLE_QUESTIONS_MAX = 4
EXAMPLE_QUESTION_MAX_LENGTH = 160


class ListMyProjectsQuery(QueryModel):
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_LIMIT
    cursor: CursorText | None = None


class MyProjectsCursor(CursorModel):
    """`{"v":1,"n":"<name>"}`: the name of the last project returned; no filter members."""

    n: Annotated[str, StringConstraints(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]


class ProjectSummary(ResponseModel):
    project_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]
    description: Annotated[str, Field(max_length=DESCRIPTION_MAX_LENGTH)]
    example_questions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=EXAMPLE_QUESTION_MAX_LENGTH)]],
        Field(max_length=EXAMPLE_QUESTIONS_MAX),
    ]
    has_default_index: bool


class ProjectSummaryPage(ResponseModel):
    items: Annotated[list[ProjectSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


def project_summary(row: ProjectSummaryRow) -> ProjectSummary:
    return ProjectSummary(
        project_id=row.id,
        name=row.name,
        description=row.description,
        example_questions=list(row.example_questions),
        has_default_index=row.has_default_index,
    )


@router.get(
    "/projects",
    operation_id="listMyProjects",
    tags=["projects"],
    response_model=ProjectSummaryPage,
)
@access("user")
async def list_my_projects(
    principal: CurrentPrincipal,
    query: Annotated[ListMyProjectsQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> ProjectSummaryPage:
    after = None if query.cursor is None else decode_cursor(query.cursor, MyProjectsCursor).n
    page = await process.access.list_my_projects(principal, after, query.limit)
    return ProjectSummaryPage(
        items=[project_summary(row) for row in page.items],
        next_cursor=(
            None if page.next_after is None else encode_cursor(MyProjectsCursor(n=page.next_after))
        ),
    )
