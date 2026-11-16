"""`adminSearchUsers` against SQL Server (T-015; `11-api.md` sections 1.4.4 Q6 and 3.8.1).

The search matches display-name and e-mail prefixes case-insensitively, treats `%`, `_` and `[`
in `q` as literal characters, matches a canonical UUID exactly and sets `isMember` for the
project given.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, insert

from docchat_core.sql.tables.access import project_members, projects, users
from docchat_testing.builders import project_member_row, project_row, user_row
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


async def search(api: ApiUnderTest, caller: SharedPrincipal, **params: str) -> list[str]:
    response = await api.client.get(
        "/v1/admin/users", params=params, headers=caller.authorization()
    )
    assert response.status_code == 200
    return [item["displayName"] for item in response.json()["items"]]


async def test_t015_prefixes_of_names_and_addresses_match(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    put(
        admin,
        (users, user_row(display_name="Ada Lovelace", email="countess@example.test")),
        (users, user_row(display_name="Bea", email="ada.b@example.test")),
        (users, user_row(display_name="Cy", email="cy@example.test")),
    )

    assert await search(api, carol, q="ada") == ["Ada Lovelace", "Bea"]
    assert await search(api, carol, q="COUNT") == ["Ada Lovelace"]
    assert await search(api, carol, q="lovelace") == []


@pytest.mark.parametrize("special", ["%", "_", "["])
async def test_t015_like_wildcards_in_q_are_literal(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, special: str
) -> None:
    put(
        admin,
        (users, user_row(display_name=f"10{special}off")),
        (users, user_row(display_name="10xoff")),
        (users, user_row(display_name="10]off")),
    )

    assert await search(api, carol, q=f"10{special}") == [f"10{special}off"]


async def test_t015_a_uuid_matches_exactly_and_is_member_follows_the_project(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    member = user_row(display_name="Member")
    outsider = user_row(display_name="Outsider")
    project = project_row()
    put(
        admin,
        (users, member),
        (users, outsider),
        (projects, project),
        (project_members, project_member_row(member["object_id"], project["id"])),
    )

    exact = await api.client.get(
        "/v1/admin/users",
        params={"q": str(outsider["object_id"]).upper(), "projectId": str(project["id"])},
        headers=carol.authorization(),
    )
    listed = await api.client.get(
        "/v1/admin/users", params={"projectId": str(project["id"])}, headers=carol.authorization()
    )
    unknown = await api.client.get(
        "/v1/admin/users", params={"projectId": str(uuid4())}, headers=carol.authorization()
    )

    assert [(item["displayName"], item["isMember"]) for item in exact.json()["items"]] == [
        ("Outsider", False)
    ]
    membership = {item["displayName"]: item["isMember"] for item in listed.json()["items"]}
    assert (membership["Member"], membership["Outsider"]) == (True, False)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "project_not_found")
