"""`listMyProjects` and `getCurrentUser` against SQL Server (T-010's HTTP half, T-493's problem
half; `11-api.md` sections 3.2.1 and 3.2.2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, insert, select, update

from docchat_core.sql.tables.access import (
    audit_events,
    project_indexes,
    project_members,
    projects,
    users,
)
from docchat_core.sql.tables.ingestion import physical_indexes, search_indexes
from docchat_testing.builders import (
    project_index_row,
    project_member_row,
    project_row,
    user_row,
)
from docchat_testing.builders.ingestion import physical_index_row, search_index_row
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def default_index(admin: Engine, project_id: UUID, *, physical_state: str | None) -> None:
    index = search_index_row(status="ready" if physical_state == "active" else "provisioning")
    rows: list[tuple[Any, dict[str, Any]]] = [(search_indexes, index)]
    physical = None
    if physical_state is not None:
        physical = physical_index_row(index["id"], state=physical_state, provisioned_at=sql_now())
        rows.append((physical_indexes, physical))
    rows.append((project_indexes, project_index_row(project_id, index["id"], is_default=True)))
    put(admin, *rows)
    if physical is not None:
        with admin.begin() as connection:
            connection.execute(
                update(search_indexes)
                .where(search_indexes.c.id == index["id"])
                .values(active_physical_index_id=physical["id"])
            )


@pytest.mark.parametrize(
    ("mapping", "has_default_index"),
    [(None, False), ("no-physical", False), ("building", False), ("active", True)],
)
async def test_t010_has_default_index_follows_the_active_physical_index(
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    mapping: str | None,
    has_default_index: bool,
) -> None:
    project = project_row()
    put(
        admin,
        (users, user_row(object_id=alice.oid)),
        (projects, project),
        (project_members, project_member_row(alice.oid, project["id"])),
    )
    if mapping is not None:
        default_index(
            admin, project["id"], physical_state=None if mapping == "no-physical" else mapping
        )

    response = await api.client.get("/v1/projects", headers=alice.authorization())

    assert response.status_code == 200
    (item,) = response.json()["items"]
    assert item["projectId"] == str(project["id"])
    assert item["hasDefaultIndex"] is has_default_index


async def test_t010_an_archived_project_leaves_the_list(
    api: ApiUnderTest, admin: Engine, alice: SharedPrincipal
) -> None:
    project = project_row()
    put(
        admin,
        (users, user_row(object_id=alice.oid)),
        (projects, project),
        (project_members, project_member_row(alice.oid, project["id"])),
    )
    with admin.begin() as connection:
        connection.execute(
            update(projects)
            .where(projects.c.id == project["id"])
            .values(status="archived", archived_at=sql_now(), archived_by=alice.oid)
        )

    response = await api.client.get("/v1/projects", headers=alice.authorization())

    assert response.json() == {"items": [], "nextCursor": None}


async def test_pages_follow_the_name_order_to_a_null_cursor(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    for _ in range(3):
        put(admin, (projects, project_row()))
    names: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({} if cursor is None else {"cursor": cursor})
        page = (
            await api.client.get("/v1/projects", params=params, headers=carol.authorization())
        ).json()
        names.extend(item["name"] for item in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break

    assert len(names) == 3
    assert names == sorted(names)


async def test_t013_the_first_request_provisions_the_caller(
    api: ApiUnderTest, admin: Engine, dave: SharedPrincipal
) -> None:
    response = await api.client.get("/v1/me", headers=dave.authorization())

    assert response.status_code == 200
    with admin.connect() as connection:
        row = connection.execute(select(users).where(users.c.object_id == dave.oid)).one()
        audit = connection.execute(
            select(audit_events.c.action, audit_events.c.correlation_id).where(
                audit_events.c.subject_object_id == dave.oid
            )
        ).all()
    assert row.last_known_role == "User"
    assert [entry.action for entry in audit] == ["user.provisioned"]


async def test_t493_a_problem_names_the_trace_of_the_traceparent(api: ApiUnderTest) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    response = await api.client.get(
        "/v1/me", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"}
    )

    assert response.status_code == 401
    assert response.json()["correlationId"] == trace_id
