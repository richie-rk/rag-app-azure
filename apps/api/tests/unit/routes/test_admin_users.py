"""Unit tests of `adminSearchUsers` (`11-api.md` section 3.8.1) through the in-process app, with
the project check and the user search replaced by recorders."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from docchat_api.http.paging import encode_cursor
from docchat_api.routes import admin_users
from docchat_api.routes.admin_users import UsersCursor
from docchat_core.sql.repositories import projects
from docchat_core.users import UserPosition, UserRow, UserSearchPage
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

PROJECT = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
STAMP = datetime(2026, 10, 30, 9, 15, 2, 123000, tzinfo=UTC)
STAMP_TEXT = "2026-10-30T09:15:02.123Z"


@dataclass
class Recorder:
    result: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, engine: object, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


@dataclass
class Stubs:
    exists: Recorder
    search: Recorder


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Stubs:
    found = Stubs(exists=Recorder(True), search=Recorder(UserSearchPage((), None)))
    monkeypatch.setattr(projects, "project_exists", found.exists)
    monkeypatch.setattr(admin_users, "search_users", found.search)
    return found


def user(name: str, *, is_member: bool | None = None) -> UserRow:
    return UserRow(
        object_id=uuid4(),
        display_name=name,
        email=f"{name.lower()}@example.test",
        last_known_role="User",
        first_seen_at=STAMP,
        last_seen_at=STAMP,
        is_member=is_member,
    )


async def test_every_user_is_listed_without_q(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    ada = user("Ada")
    stubs.search.result = UserSearchPage((ada,), None)

    response = await started.client.get("/v1/admin/users", headers=carol.authorization())

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "userId": str(ada.object_id),
                "displayName": "Ada",
                "email": "ada@example.test",
                "lastKnownRole": "User",
                "firstSeenAt": STAMP_TEXT,
                "lastSeenAt": STAMP_TEXT,
                "isMember": None,
            }
        ],
        "nextCursor": None,
    }
    assert stubs.search.calls == [{"q": None, "project_id": None, "after": None, "limit": 50}]
    assert stubs.exists.calls == []


async def test_a_project_sets_is_member_and_must_exist(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    stubs.search.result = UserSearchPage((user("Bo", is_member=True),), None)

    response = await started.client.get(
        "/v1/admin/users",
        params={"projectId": str(PROJECT).upper(), "q": "  b  "},
        headers=carol.authorization(),
    )

    assert response.json()["items"][0]["isMember"] is True
    assert stubs.exists.calls == [{"project_id": PROJECT}]
    assert stubs.search.calls[0] | {"after": None} == {
        "q": "b",
        "project_id": PROJECT,
        "after": None,
        "limit": 50,
    }


async def test_an_unknown_project_is_not_found(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    stubs.exists.result = False

    response = await started.client.get(
        "/v1/admin/users", params={"projectId": str(uuid4())}, headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert stubs.search.calls == []


async def test_the_next_cursor_carries_the_filters(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    last = user("Cy")
    stubs.search.result = UserSearchPage(
        (user("Bo"), last), UserPosition(display_name="Cy", object_id=last.object_id)
    )

    response = await started.client.get(
        "/v1/admin/users",
        params={"q": "c", "projectId": str(PROJECT), "limit": 2},
        headers=carol.authorization(),
    )

    assert response.json()["nextCursor"] == encode_cursor(
        UsersCursor(d="Cy", u=last.object_id, q="c", p=PROJECT)
    )


async def test_a_cursor_resumes_after_its_position(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    after = uuid4()
    cursor = encode_cursor(UsersCursor(d="Cy", u=after, q=None, p=None))

    await started.client.get(
        "/v1/admin/users", params={"cursor": cursor}, headers=carol.authorization()
    )

    assert stubs.search.calls[0]["after"] == UserPosition(display_name="Cy", object_id=after)


@pytest.mark.parametrize(
    "params",
    [
        {"q": "d"},
        {"projectId": str(PROJECT)},
    ],
)
async def test_a_cursor_of_other_filters_is_invalid(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal, params: dict[str, str]
) -> None:
    cursor = encode_cursor(UsersCursor(d="Cy", u=uuid4(), q=None, p=None))

    response = await started.client.get(
        "/v1/admin/users", params={"cursor": cursor} | params, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]
    assert stubs.search.calls == []
    assert stubs.exists.calls == []


async def test_the_parameters_are_validated(
    started: ApiUnderTest, stubs: Stubs, carol: SharedPrincipal
) -> None:
    response = await started.client.get(
        "/v1/admin/users",
        params={"q": "a\u200eb", "projectId": "{" + str(PROJECT) + "}", "limit": "101"},
        headers=carol.authorization(),
    )

    assert response.json()["errors"] == [
        {"path": "query.q", "reason": "invalid_characters"},
        {"path": "query.projectId", "reason": "invalid_uuid"},
        {"path": "query.limit", "reason": "out_of_range"},
    ]


def test_the_router_serves_only_the_search(stubs: Stubs) -> None:
    del stubs
    routes: list[Callable[..., Any]] = [route.endpoint for route in admin_users.router.routes]  # type: ignore[attr-defined]
    assert routes == [admin_users.admin_search_users]
