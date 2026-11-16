from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from docchat_api.http.paging import encode_cursor
from docchat_api.routes.projects import MyProjectsCursor, ProjectSummaryPage
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal


class FakeSql(Protocol):
    projects: list[SimpleNamespace]
    calls: list[Any]


def _row(name: str, *, has_default_index: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid5(NAMESPACE_URL, name),
        name=name,
        description=f"About {name}",
        example_questions_json='["What changed?"]',
        has_default_index=1 if has_default_index else 0,
    )


async def test_an_empty_first_page(started: ApiUnderTest, alice: SharedPrincipal) -> None:
    response = await started.client.get("/v1/projects", headers=alice.authorization())

    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}


async def test_the_items_follow_q5_with_has_default_index(
    started: ApiUnderTest, fake_sql: FakeSql, alice: SharedPrincipal
) -> None:
    fake_sql.projects = [_row("Beta", has_default_index=False), _row("Alpha")]

    response = await started.client.get("/v1/projects", headers=alice.authorization())

    page = ProjectSummaryPage.model_validate(response.json())
    assert [(item.name, item.has_default_index) for item in page.items] == [
        ("Alpha", True),
        ("Beta", False),
    ]
    assert page.items[0].example_questions == ["What changed?"]
    assert page.next_cursor is None
    assert fake_sql.calls[-1].parameters == {"oid": alice.oid, "limit_plus_one": 51}


async def test_an_administrator_reads_every_active_project(
    started: ApiUnderTest, fake_sql: FakeSql, carol: SharedPrincipal
) -> None:
    await started.client.get("/v1/projects", headers=carol.authorization())

    assert "oid" not in fake_sql.calls[-1].parameters


async def test_pages_follow_the_cursor_to_a_null_cursor(
    started: ApiUnderTest, fake_sql: FakeSql, alice: SharedPrincipal
) -> None:
    fake_sql.projects = [_row(name) for name in ("A", "B", "C")]

    first = (
        await started.client.get("/v1/projects", params={"limit": 2}, headers=alice.authorization())
    ).json()
    second = (
        await started.client.get(
            "/v1/projects",
            params={"limit": 2, "cursor": first["nextCursor"]},
            headers=alice.authorization(),
        )
    ).json()

    assert [item["name"] for item in first["items"]] == ["A", "B"]
    assert first["nextCursor"] == encode_cursor(MyProjectsCursor(n="B"))
    assert [item["name"] for item in second["items"]] == ["C"]
    assert second["nextCursor"] is None


async def test_parameters_are_validated(started: ApiUnderTest, alice: SharedPrincipal) -> None:
    response = await started.client.get(
        "/v1/projects",
        params={"limit": 101, "cursor": "!!", "projectId": "x"},
        headers=alice.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"path": "query.limit", "reason": "out_of_range"},
        {"path": "query.cursor", "reason": "invalid_cursor"},
        {"path": "query.projectId", "reason": "unknown_field"},
    ]


async def test_a_tampered_cursor_is_invalid_cursor(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.get(
        "/v1/projects", params={"cursor": "eyJ2IjoyfQ"}, headers=alice.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]
