"""The access functions against the SQL Server container (the SQL half of T-010, with the
agreement of `hasDefaultIndex` with `resolve_chat_index`; `11-api.md` section 1.4 and
acceptance criterion 15; ADR 0020 item 11).

The functions run through the data-access layer as the `docchat_api` role, as the API runs
them; rows are arranged and changed as `sa`, as the administrator operations and `docchat-ops`
would change them. The API-level halves (`adminRemoveProjectMember` followed by a chat request,
`listMyProjects` over HTTP) are the API's tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, delete, insert, update
from sqlalchemy.exc import IntegrityError

from docchat_core.access import (
    AccessChecks,
    ProjectIndexUnavailable,
    ProjectNotFound,
)
from docchat_core.auth import Principal
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import (
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import physical_indexes, search_indexes
from docchat_testing.builders import (
    project_index_row,
    project_member_row,
    project_row,
    user_index_override_row,
    user_row,
)
from docchat_testing.builders.ingestion import physical_index_row, search_index_row
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.tokens import SESSION_AUTH

pytestmark = pytest.mark.integration


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
async def api(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = SqlEngine(
        "api",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_api"),
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def checks(api: SqlEngine) -> AccessChecks:
    return AccessChecks(api)


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def caller(admin: Engine, *, is_admin: bool = False) -> Principal:
    row = user_row(last_known_role="Admin" if is_admin else "User")
    put(admin, (users, row))
    return Principal(
        oid=row["object_id"],
        tid=SESSION_AUTH.tenant_id,
        azp=SESSION_AUTH.web_client_id,
        client="web",
        roles=frozenset({"Admin"} if is_admin else {"User"}),
    )


def archive(admin: Engine, project_id: UUID) -> None:
    with admin.begin() as connection:
        connection.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(status="archived", archived_at=sql_now(), archived_by=uuid4())
        )


def restore(admin: Engine, project_id: UUID) -> None:
    with admin.begin() as connection:
        connection.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(status="active", archived_at=None, archived_by=None)
        )


async def test_t010_a_removed_membership_refuses_the_next_check(
    checks: AccessChecks, admin: Engine
) -> None:
    member = caller(admin)
    project = project_row()
    put(
        admin, (projects, project), (project_members, project_member_row(member.oid, project["id"]))
    )
    assert (await checks.authorize_project_for_chat(member, project["id"])).access == "member"

    with admin.begin() as connection:
        connection.execute(
            delete(project_members).where(project_members.c.user_object_id == member.oid)
        )

    with pytest.raises(ProjectNotFound):
        await checks.authorize_project_for_chat(member, project["id"])


async def test_t010_an_archived_project_is_refused_for_everyone_until_restored(
    checks: AccessChecks, admin: Engine
) -> None:
    member, administrator = caller(admin), caller(admin, is_admin=True)
    project = project_row()
    put(
        admin, (projects, project), (project_members, project_member_row(member.oid, project["id"]))
    )

    archive(admin, project["id"])
    for who in (member, administrator):
        with pytest.raises(ProjectNotFound):
            await checks.authorize_project_for_chat(who, project["id"])
        assert project["id"] not in await checks.list_reachable_project_ids(who)

    restore(admin, project["id"])
    assert (await checks.authorize_project_for_chat(member, project["id"])).access == "member"
    assert (await checks.authorize_project_for_chat(administrator, project["id"])).access == "admin"
    assert project["id"] in await checks.list_reachable_project_ids(member)


async def test_an_outsider_and_a_random_id_get_the_same_refusal(
    checks: AccessChecks, admin: Engine
) -> None:
    outsider = caller(admin)
    project = project_row()
    put(admin, (projects, project))

    for project_id in (project["id"], uuid4()):
        with pytest.raises(ProjectNotFound) as caught:
            await checks.authorize_project_for_chat(outsider, project_id)
        assert caught.value.detail == ProjectNotFound().detail


def _index(admin: Engine, project_id: UUID, *, default: bool, physical_state: str | None) -> UUID:
    index = search_index_row(status="ready" if physical_state == "active" else "provisioning")
    rows: list[tuple[Any, dict[str, Any]]] = [(search_indexes, index)]
    physical = None
    if physical_state is not None:
        physical = physical_index_row(index["id"], state=physical_state, provisioned_at=sql_now())
        rows.append((physical_indexes, physical))
    rows.append((project_indexes, project_index_row(project_id, index["id"], is_default=default)))
    put(admin, *rows)
    if physical is not None:
        with admin.begin() as connection:
            connection.execute(
                update(search_indexes)
                .where(search_indexes.c.id == index["id"])
                .values(active_physical_index_id=physical["id"])
            )
    return UUID(str(index["id"]))


@pytest.mark.parametrize(
    ("mapping", "resolvable"),
    [
        (None, False),
        ("no-physical", False),
        ("building", False),
        ("retiring", False),
        ("active", True),
    ],
)
async def test_t010_has_default_index_and_resolution_agree(
    checks: AccessChecks, admin: Engine, mapping: str | None, resolvable: bool
) -> None:
    member = caller(admin)
    project = project_row()
    put(
        admin, (projects, project), (project_members, project_member_row(member.oid, project["id"]))
    )
    if mapping is not None:
        state = None if mapping == "no-physical" else mapping
        _index(admin, project["id"], default=True, physical_state=state)

    (summary,) = (await checks.list_my_projects(member, None, 50)).items
    accessible = await checks.authorize_project_for_chat(member, project["id"])
    if resolvable:
        resolved = await checks.resolve_chat_index(member, accessible)
        assert resolved.source == "project_default"
    else:
        with pytest.raises(ProjectIndexUnavailable):
            await checks.resolve_chat_index(member, accessible)
    assert summary.has_default_index is resolvable


async def test_an_unexpired_override_wins_and_an_expired_one_is_ignored(
    checks: AccessChecks, admin: Engine
) -> None:
    member = caller(admin)
    project = project_row()
    put(
        admin, (projects, project), (project_members, project_member_row(member.oid, project["id"]))
    )
    default = _index(admin, project["id"], default=True, physical_state="active")
    other = _index(admin, project["id"], default=False, physical_state="active")
    accessible = await checks.authorize_project_for_chat(member, project["id"])

    put(admin, (user_index_overrides, user_index_override_row(member.oid, project["id"], other)))
    resolved = await checks.resolve_chat_index(member, accessible)
    assert (resolved.search_index_id, resolved.source) == (other, "user_override")
    assert resolved.override_expires_at is not None

    set_at = sql_now() - timedelta(days=10)
    with admin.begin() as connection:
        connection.execute(
            update(user_index_overrides)
            .where(user_index_overrides.c.user_object_id == member.oid)
            .values(set_at=set_at, expires_at=set_at + timedelta(days=9))
        )
    resolved = await checks.resolve_chat_index(member, accessible)
    assert (resolved.search_index_id, resolved.source) == (default, "project_default")


async def test_an_override_needs_its_index_mapped_to_the_project(admin: Engine) -> None:
    member = caller(admin)
    project = project_row()
    index = search_index_row()
    put(admin, (projects, project), (search_indexes, index))

    with pytest.raises(IntegrityError):
        put(
            admin,
            (user_index_overrides, user_index_override_row(member.oid, project["id"], index["id"])),
        )


async def test_the_project_picker_pages_by_name(checks: AccessChecks, admin: Engine) -> None:
    member, administrator = caller(admin), caller(admin, is_admin=True)
    rows = [project_row(name=f"Project {letter}") for letter in "DCBA"]
    archived = project_row(name="Project E")
    put(admin, *((projects, row) for row in [*rows, archived]))
    put(admin, *((project_members, project_member_row(member.oid, row["id"])) for row in rows[:3]))
    archive(admin, archived["id"])

    first = await checks.list_my_projects(administrator, None, 3)
    assert [item.name for item in first.items] == ["Project A", "Project B", "Project C"]
    assert first.next_after == "Project C"
    second = await checks.list_my_projects(administrator, first.next_after, 3)
    assert [item.name for item in second.items] == ["Project D"]
    assert second.next_after is None

    mine = await checks.list_my_projects(member, None, 50)
    assert [item.name for item in mine.items] == ["Project B", "Project C", "Project D"]
    assert await checks.list_reachable_project_ids(member) == {row["id"] for row in rows[:3]}
