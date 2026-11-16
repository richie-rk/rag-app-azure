"""`adminSearchUsers` — `GET /v1/admin/users` (`11-api.md` sections 1.4.4 Q6, 1.8, 3.8.1 and 4.4).

The people pickers of the administrator screens. Only people who have signed in have an
`app.users` row, so only they appear, and the API has no Microsoft Graph permission to find
anyone else. `q`, normalised as single-line text, matches the object ID exactly when it is a
canonical UUID and otherwise is a case-insensitive prefix of the display name or the e-mail
address; without `q` every user is listed. With `projectId` (`404 project_not_found` when no
project, active or archived, has it) each item says whether that person is a member. Pages are
ordered by display name, then user ID, and the cursor carries the `q` and `projectId` it was
issued for.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field, StringConstraints

from docchat_api.http.access import access
from docchat_api.http.models import QueryModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import ApiProcess, current_process
from docchat_api.http.text import SingleLineText
from docchat_api.routes.admin_common import DEFAULT_LIMIT, next_cursor
from docchat_core.access import MAX_PAGE_LIMIT, ProjectNotFound
from docchat_core.auth.principal import DISPLAY_NAME_MAX_LENGTH, EMAIL_MAX_LENGTH
from docchat_core.models import CanonicalUuid, UtcDateTime
from docchat_core.sql.repositories import projects
from docchat_core.users import UserPosition, UserRow, search_users

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "admin-users"
SEARCH_TEXT_MAX_LENGTH: Final = 80


class AdminSearchUsersQuery(QueryModel):
    q: Annotated[str, SingleLineText(1, SEARCH_TEXT_MAX_LENGTH)] | None = None
    project_id: CanonicalUuid | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_LIMIT
    cursor: CursorText | None = None


class UsersCursor(CursorModel):
    """`{"v":1,"d":"<displayName>","u":"<userId>","q":"<q>" or null,"p":"<projectId>" or null}`."""

    d: Annotated[str, StringConstraints(max_length=DISPLAY_NAME_MAX_LENGTH)]
    u: UUID
    q: Annotated[str, StringConstraints(min_length=1, max_length=SEARCH_TEXT_MAX_LENGTH)] | None
    p: UUID | None


class UserSummary(ResponseModel):
    user_id: UUID
    display_name: Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH)]
    email: Annotated[str, Field(max_length=EMAIL_MAX_LENGTH)]
    last_known_role: Literal["Admin", "User"]
    first_seen_at: UtcDateTime
    last_seen_at: UtcDateTime
    is_member: bool | None


class UserSummaryPage(ResponseModel):
    items: Annotated[list[UserSummary], Field(max_length=MAX_PAGE_LIMIT)]
    next_cursor: CursorText | None


def user_summary(row: UserRow) -> UserSummary:
    return UserSummary(
        user_id=row.object_id,
        display_name=row.display_name,
        email=row.email,
        last_known_role=row.last_known_role,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        is_member=row.is_member,
    )


@router.get(
    "/admin/users",
    operation_id="adminSearchUsers",
    tags=[TAG],
    response_model=UserSummaryPage,
)
@access("admin", scoped_params=["projectId"], error_codes=["project_not_found"])
async def admin_search_users(
    query: Annotated[AdminSearchUsersQuery, Query()],
    process: Annotated[ApiProcess, Depends(current_process)],
) -> UserSummaryPage:
    after = None
    if query.cursor is not None:
        filters = {"q": query.q, "p": query.project_id}
        cursor = decode_cursor(query.cursor, UsersCursor, filters=filters)
        after = UserPosition(display_name=cursor.d, object_id=cursor.u)
    if query.project_id is not None and not await projects.project_exists(
        process.sql, project_id=query.project_id
    ):
        raise ProjectNotFound()
    page = await search_users(
        process.sql, q=query.q, project_id=query.project_id, after=after, limit=query.limit
    )
    items, cursor_text = next_cursor(
        page.items,
        page.next_position is not None,
        lambda row: UsersCursor(d=row.display_name, u=row.object_id, q=query.q, p=query.project_id),
    )
    return UserSummaryPage(items=[user_summary(row) for row in items], next_cursor=cursor_text)
