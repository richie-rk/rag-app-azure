"""The user search of `adminSearchUsers` (`11-api.md` sections 1.4.4 Q6, 1.8 and 3.8.1).

Only people who have signed in have an `app.users` row, so only they appear. `q`, already
normalised by the API, matches the object ID exactly when it is a canonical UUID and otherwise is
a case-insensitive prefix of the display name or the e-mail address; without `q` every user is
listed. With a project, each item says whether the user is a member of it. Pages are keyset pages
ordered by display name, then object ID. The caller checks the project exists first
(`404 project_not_found`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.users import UserPosition, UserRow, read_users

MAX_LIMIT: Final = 100


@dataclass(frozen=True, slots=True)
class UserSearchPage:
    """A page of `UserSummary` rows; `next_position` is the last item's position when another
    page exists, the values the next cursor carries."""

    items: tuple[UserRow, ...]
    next_position: UserPosition | None


async def search_users(
    engine: SqlEngine,
    *,
    q: str | None,
    project_id: UUID | None,
    after: UserPosition | None,
    limit: int,
) -> UserSearchPage:
    """Up to `limit` users after `after` (the decoded cursor)."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must lie within 1 to {MAX_LIMIT}")
    rows = await read_users(
        engine, q=q, project_id=project_id, after=after, limit_plus_one=limit + 1
    )
    items = tuple(rows[:limit])
    next_position = None
    if len(rows) > limit:
        last = items[-1]
        next_position = UserPosition(display_name=last.display_name, object_id=last.object_id)
    return UserSearchPage(items=items, next_position=next_position)
