"""First-sign-in provisioning and the user search against the SQL Server container (T-012;
`11-api.md` sections 1.4.4 Q6, 3.2.1 and 3.8.1; ADR 0021 obligations).

`ensure_user` runs through the data-access layer as the `docchat_api` role, as the API runs it;
each concurrent call gets its own throttle, as requests served by different workers and
instances do. Rows are arranged and read back as `sa`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, insert, select, update

from docchat_core.auth import Principal
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.users import UserPosition
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import audit_events, project_members, projects, users
from docchat_core.users import LastSeenThrottle, ensure_user, search_users
from docchat_testing.builders import project_member_row, project_row, user_row
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
        sql=SqlSettings(sql_threads=8),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_api"),
    )
    yield engine
    await engine.dispose()


def principal(
    oid: UUID | None = None, *, admin: bool = False, display_name: str = "Ada", email: str = ""
) -> Principal:
    return Principal(
        oid=oid or uuid4(),
        tid=SESSION_AUTH.tenant_id,
        azp=SESSION_AUTH.web_client_id,
        client="web",
        roles=frozenset({"Admin"} if admin else {"User"}),
        display_name=display_name,
        email=email,
    )


def put(engine: Engine, table: Any, *rows: dict[str, Any]) -> None:
    with engine.begin() as connection:
        for row in rows:
            connection.execute(insert(table), row)


def count(engine: Engine, statement: Any) -> int:
    with engine.connect() as connection:
        return int(connection.execute(statement).scalar_one())


def default_projects(engine: Engine, number: int) -> list[UUID]:
    rows = [project_row(is_default=True) for _ in range(number)]
    put(engine, projects, *rows)
    return [row["id"] for row in rows]


async def test_t012_32_concurrent_first_requests_provision_one_user(
    api: SqlEngine, admin: Engine
) -> None:
    project_ids = default_projects(admin, 2)
    caller = principal()

    outcomes = await asyncio.gather(
        *(ensure_user(api, caller, LastSeenThrottle()) for _ in range(32))
    )

    assert outcomes.count("provisioned") == 1
    assert set(outcomes) <= {"provisioned", "touched"}
    assert count(admin, select(func.count()).where(users.c.object_id == caller.oid)) == 1
    memberships = select(project_members.c.project_id, project_members.c.grant_source).where(
        project_members.c.user_object_id == caller.oid
    )
    with admin.connect() as connection:
        rows = connection.execute(memberships).all()
    assert sorted((row.project_id, row.grant_source) for row in rows) == sorted(
        (project_id, "default") for project_id in project_ids
    )
    provisioned = select(audit_events.c.metadata_json).where(
        audit_events.c.action == "user.provisioned",
        audit_events.c.subject_object_id == caller.oid,
    )
    with admin.connect() as connection:
        audit = connection.execute(provisioned).all()
    assert [row.metadata_json for row in audit] == ['{"defaultProjectCount":2}']


async def test_t012_first_requests_of_50_users_at_once_all_succeed(
    api: SqlEngine, admin: Engine
) -> None:
    default_projects(admin, 1)
    callers = [principal() for _ in range(50)]

    outcomes = await asyncio.gather(
        *(ensure_user(api, caller, LastSeenThrottle()) for caller in callers)
    )

    assert outcomes == ["provisioned"] * 50
    assert count(admin, select(func.count()).select_from(users)) == 50
    assert count(admin, select(func.count()).select_from(project_members)) == 50


async def test_a_project_flagged_default_later_reaches_only_new_users(
    api: SqlEngine, admin: Engine
) -> None:
    first = principal()
    assert await ensure_user(api, first, LastSeenThrottle()) == "provisioned"
    later = project_row()
    put(admin, projects, later)
    with admin.begin() as connection:
        connection.execute(
            update(projects).where(projects.c.id == later["id"]).values(is_default=True)
        )

    assert await ensure_user(api, first, LastSeenThrottle()) == "touched"
    second = principal()
    assert await ensure_user(api, second, LastSeenThrottle()) == "provisioned"

    members = select(project_members.c.user_object_id).where(
        project_members.c.project_id == later["id"]
    )
    with admin.connect() as connection:
        assert [row.user_object_id for row in connection.execute(members)] == [second.oid]


async def test_an_empty_display_claim_never_erases_the_stored_value(
    api: SqlEngine, admin: Engine
) -> None:
    oid = uuid4()
    await ensure_user(
        api, principal(oid, display_name="Ada", email="ada@example.test"), LastSeenThrottle()
    )

    outcome = await ensure_user(
        api, principal(oid, admin=True, display_name="", email=""), LastSeenThrottle()
    )

    assert outcome == "touched"
    with admin.connect() as connection:
        row = connection.execute(select(users).where(users.c.object_id == oid)).one()
    assert (row.display_name, row.email, row.last_known_role) == (
        "Ada",
        "ada@example.test",
        "Admin",
    )
    assert row.last_seen_at >= row.first_seen_at


async def test_the_throttle_skips_sql_within_15_minutes(api: SqlEngine, admin: Engine) -> None:
    caller = principal()
    throttle = LastSeenThrottle()
    await ensure_user(api, caller, throttle)
    with admin.begin() as connection:
        connection.execute(update(users).values(display_name="Changed"))

    assert await ensure_user(api, principal(caller.oid, display_name="Ada"), throttle) == "skipped"
    with admin.connect() as connection:
        assert connection.execute(select(users.c.display_name)).scalar_one() == "Changed"


async def test_the_user_search_pages_prefix_matches(api: SqlEngine, admin: Engine) -> None:
    project = project_row()
    put(admin, projects, project)
    rows = [
        user_row(display_name=name, email=f"{name.lower()}@example.test")
        for name in ("Ada", "Adam", "Adele", "Bob", "zz_a", "zzza")
    ]
    put(admin, users, *rows)
    put(admin, project_members, project_member_row(rows[1]["object_id"], project["id"]))

    first = await search_users(api, q="ad", project_id=project["id"], after=None, limit=2)
    assert [(item.display_name, item.is_member) for item in first.items] == [
        ("Ada", False),
        ("Adam", True),
    ]
    assert first.next_position == UserPosition("Adam", rows[1]["object_id"])
    second = await search_users(
        api, q="ad", project_id=project["id"], after=first.next_position, limit=2
    )
    assert [item.display_name for item in second.items] == ["Adele"]
    assert second.next_position is None

    # `_` is escaped, so it matches only itself.
    escaped = await search_users(api, q="zz_", project_id=None, after=None, limit=10)
    assert [item.display_name for item in escaped.items] == ["zz_a"]
    by_email = await search_users(api, q="BOB@", project_id=None, after=None, limit=10)
    assert [item.display_name for item in by_email.items] == ["Bob"]
    by_id = await search_users(
        api, q=str(rows[3]["object_id"]).upper(), project_id=None, after=None, limit=10
    )
    assert [(item.display_name, item.is_member) for item in by_id.items] == [("Bob", None)]
    everyone = await search_users(
        api, q=None, project_id=None, after=UserPosition("Adele", rows[2]["object_id"]), limit=1
    )
    assert [item.display_name for item in everyone.items] == ["Bob"]
    assert everyone.next_position == UserPosition("Bob", rows[3]["object_id"])
