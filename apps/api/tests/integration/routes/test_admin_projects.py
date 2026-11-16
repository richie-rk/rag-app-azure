"""The administrator project, member and mapping operations against SQL Server (`11-api.md`
sections 3.7 and 3.9 and acceptance criteria 6, 9, 10 and 21; T-021, T-049, T-054, T-056,
T-062 and T-010's membership half).

The app runs as the `docchat_api` role with the permissive limiters; rows are arranged and read
back as `sa`, because the API role can't read `app.audit_events`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, update

from docchat_core.access import AccessChecks, ProjectIndexUnavailable
from docchat_core.auth import Principal
from docchat_core.models import encode_row_version
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories import projects as repo
from docchat_core.sql.tables.access import (
    audit_events,
    project_indexes,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.ingestion import search_indexes
from docchat_core.sql.tables.schema import BIT_TRUE
from docchat_testing.builders import (
    project_index_row,
    project_member_row,
    project_row,
    search_index_row,
    user_index_override_row,
    user_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.tokens import SESSION_AUTH

pytestmark = pytest.mark.integration

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def version_of(engine: Engine, project_id: UUID) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            select(projects.c.row_version).where(projects.c.id == project_id)
        ).scalar_one()
    return encode_row_version(bytes(value))


def if_match(caller: SharedPrincipal, version: str) -> dict[str, str]:
    return caller.authorization() | {"If-Match": f'"{version}"'}


def audit(engine: Engine, project_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(audit_events)
                .where(audit_events.c.project_id == project_id)
                .order_by(audit_events.c.id)
            ).all()
        )


def count(engine: Engine, table: Any, *conditions: Any) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(table).where(*conditions)
            ).scalar_one()
        )


def mapped_project(
    engine: Engine, *, indexes: int = 1, status: str = "ready"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A project whose first index is its default mapping, the others mapped as non-default."""
    project = project_row()
    rows = [search_index_row(status=status) for _ in range(indexes)]
    put(
        engine,
        (projects, project),
        *((search_indexes, row) for row in rows),
        *(
            (
                project_indexes,
                project_index_row(project["id"], row["id"], is_default=position == 0),
            )
            for position, row in enumerate(rows)
        ),
    )
    return project, rows


def archive(engine: Engine, project_id: UUID, actor: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(status="archived", archived_at=sql_now(), archived_by=actor)
        )


# --- T-062 and T-021: projects --------------------------------------------------------------


async def test_t062_t021_initial_members_join_in_the_creating_transaction(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    people = [user_row() for _ in range(3)]
    put(admin, *((users, person) for person in people))

    response = await api.client.post(
        "/v1/admin/projects",
        json={
            "name": "Field notes",
            "isDefault": True,
            "initialMemberIds": [str(person["object_id"]) for person in people],
        },
        headers=carol.authorization() | {"traceparent": TRACEPARENT},
    )

    assert response.status_code == 201
    body = response.json()
    project_id = UUID(body["projectId"])
    assert (body["memberCount"], body["indexCount"], body["defaultIndex"]) == (3, 0, None)
    assert body["rowVersion"] == version_of(admin, project_id)
    assert count(admin, project_members, project_members.c.project_id == project_id) == 3
    rows = audit(admin, project_id)
    assert [row.action for row in rows] == ["project.created"] + ["project_member.added"] * 3
    assert rows[0].metadata_json == '{"isDefault":true}'
    assert {row.subject_object_id for row in rows[1:]} == {p["object_id"] for p in people}
    assert {row.correlation_id for row in rows} == {TRACE_ID}
    assert {row.actor_object_id for row in rows} == {carol.oid}


async def test_t062_an_unknown_member_writes_nothing(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    known = user_row()
    put(admin, (users, known))
    before = count(admin, projects)

    response = await api.client.post(
        "/v1/admin/projects",
        json={"name": "Field notes", "initialMemberIds": [str(known["object_id"]), str(uuid4())]},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body.initialMemberIds.1", "reason": "not_found"}]
    assert count(admin, projects) == before


@pytest.mark.parametrize(
    ("member_ids", "error"),
    [
        (
            ["0a11ce00-0000-4000-8000-000000000001"] * 2,
            {"path": "body.initialMemberIds.1", "reason": "duplicate"},
        ),
        (
            [str(uuid4()) for _ in range(51)],
            {"path": "body.initialMemberIds", "reason": "too_many_items"},
        ),
    ],
)
async def test_t062_repeated_and_too_many_members_are_refused(
    api: ApiUnderTest, carol: SharedPrincipal, member_ids: list[str], error: dict[str, str]
) -> None:
    response = await api.client.post(
        "/v1/admin/projects",
        json={"name": "Field notes", "initialMemberIds": member_ids},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [error]


async def test_t062_names_are_unique_case_insensitively_archived_included(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    taken = project_row(name="Field notes")
    put(admin, (projects, taken))
    archive(admin, taken["id"], carol.oid)

    response = await api.client.post(
        "/v1/admin/projects", json={"name": "FIELD NOTES"}, headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (409, "project_name_taken")


async def test_a_bidirectional_control_in_the_name_writes_nothing(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    before = count(admin, projects)

    response = await api.client.post(
        "/v1/admin/projects", json={"name": "Field\u202enotes"}, headers=carol.authorization()
    )

    assert response.json()["errors"] == [{"path": "body.name", "reason": "invalid_characters"}]
    assert count(admin, projects) == before


async def test_t062_an_archived_project_takes_only_the_restore_body(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    put(admin, (projects, project))
    path = f"/v1/admin/projects/{project['id']}"

    archived = await api.client.patch(
        path, json={"status": "archived"}, headers=if_match(carol, version_of(admin, project["id"]))
    )
    renamed = await api.client.patch(
        path, json={"name": "Renamed"}, headers=if_match(carol, archived.json()["rowVersion"])
    )
    restored = await api.client.patch(
        path, json={"status": "active"}, headers=if_match(carol, archived.json()["rowVersion"])
    )

    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.json()["archivedBy"]["userId"] == str(carol.oid)
    assert (renamed.status_code, renamed.json()["code"]) == (409, "project_archived")
    assert restored.status_code == 200
    assert (restored.json()["status"], restored.json()["archivedAt"]) == ("active", None)
    assert restored.json()["isDefault"] is False
    actions = [row.action for row in audit(admin, project["id"])]
    assert actions == ["project.archived", "project.restored"]


async def test_t021_a_field_edit_names_its_changed_fields(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    put(admin, (projects, project))

    response = await api.client.patch(
        f"/v1/admin/projects/{project['id']}",
        json={"description": "New notes", "isDefault": True, "name": project["name"]},
        headers=if_match(carol, version_of(admin, project["id"])),
    )

    assert response.status_code == 200
    (row,) = audit(admin, project["id"])
    assert (row.action, row.metadata_json) == (
        "project.updated",
        '{"changedFields":["description","isDefault"]}',
    )


async def test_t062_a_forced_audit_failure_rolls_the_change_back(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_audit(connection: Any, event: Any) -> None:
        # An action outside `CK_audit_events_action` fails the insert like any audit failure.
        connection.execute(
            insert(audit_events).values(
                actor_type="user",
                actor_object_id=event.actor_object_id,
                action="project.forced_failure",
                target_type="project",
            )
        )

    monkeypatch.setattr(repo, "write_audit_event", failing_audit)
    before = count(admin, projects)

    response = await api.client.post(
        "/v1/admin/projects", json={"name": "Field notes"}, headers=carol.authorization()
    )

    assert response.status_code == 500
    assert count(admin, projects) == before


# --- T-062 and T-021: mappings --------------------------------------------------------------


async def test_t062_a_first_mapping_must_be_the_default(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    index = search_index_row(status="ready")
    put(admin, (projects, project), (search_indexes, index))
    version = version_of(admin, project["id"])
    path = f"/v1/admin/projects/{project['id']}/indexes/{index['id']}"

    refused = await api.client.put(
        path, json={"isDefault": False}, headers=if_match(carol, version)
    )
    mapped = await api.client.put(path, json={"isDefault": True}, headers=if_match(carol, version))

    assert (refused.status_code, refused.json()["code"]) == (409, "default_index_required")
    assert mapped.status_code == 201
    assert mapped.json()["mapping"]["isDefault"] is True
    assert mapped.json()["projectRowVersion"] == version_of(admin, project["id"])
    (row,) = audit(admin, project["id"])
    assert (row.action, row.metadata_json) == ("project_index.mapped", '{"isDefault":true}')


async def test_t021_a_new_default_replaces_the_old_one(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, (old,) = mapped_project(admin)
    new = search_index_row(status="reingesting")
    put(admin, (search_indexes, new))

    response = await api.client.put(
        f"/v1/admin/projects/{project['id']}/indexes/{new['id']}",
        json={"isDefault": True},
        headers=if_match(carol, version_of(admin, project["id"])),
    )

    assert response.status_code == 201
    with admin.connect() as connection:
        defaults = connection.execute(
            select(project_indexes.c.search_index_id).where(
                project_indexes.c.project_id == project["id"],
                project_indexes.c.is_default == BIT_TRUE,
            )
        ).all()
    assert [row.search_index_id for row in defaults] == [new["id"]]
    rows = audit(admin, project["id"])
    assert [row.action for row in rows] == ["project_index.mapped", "project_index.default_set"]
    assert rows[1].metadata_json == f'{{"previousDefaultIndexId":"{old["id"]}"}}'


@pytest.mark.parametrize(
    ("status", "answer", "code"),
    [
        ("failed", 409, "index_not_mappable"),
        ("deleting", 409, "index_not_mappable"),
        ("deleted", 404, "index_not_found"),
    ],
)
async def test_t062_only_an_index_that_can_serve_is_mappable(
    *, api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, status: str, answer: int, code: str
) -> None:
    project, _ = mapped_project(admin)
    index = search_index_row(status=status)
    put(admin, (search_indexes, index))

    response = await api.client.put(
        f"/v1/admin/projects/{project['id']}/indexes/{index['id']}",
        json={"isDefault": False},
        headers=if_match(carol, version_of(admin, project["id"])),
    )

    assert (response.status_code, response.json()["code"]) == (answer, code)


async def test_t062_the_default_stays_while_other_mappings_exist(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, (default, _) = mapped_project(admin, indexes=2)

    response = await api.client.delete(
        f"/v1/admin/projects/{project['id']}/indexes/{default['id']}",
        headers=if_match(carol, version_of(admin, project["id"])),
    )

    assert (response.status_code, response.json()["code"]) == (409, "default_index_in_use")


async def test_t062_removing_the_only_mapping_leaves_nothing_to_search(
    api: ApiUnderTest,
    admin: Engine,
    api_sql_engine: SqlEngine,
    alice: SharedPrincipal,
    carol: SharedPrincipal,
) -> None:
    project, (index,) = mapped_project(admin)
    put(
        admin,
        (users, user_row(object_id=alice.oid)),
        (project_members, project_member_row(alice.oid, project["id"])),
        (user_index_overrides, user_index_override_row(alice.oid, project["id"], index["id"])),
    )

    response = await api.client.delete(
        f"/v1/admin/projects/{project['id']}/indexes/{index['id']}",
        headers=if_match(carol, version_of(admin, project["id"])),
    )

    assert response.json() == {
        "removed": True,
        "projectRowVersion": version_of(admin, project["id"]),
    }
    (row,) = audit(admin, project["id"])
    assert (row.action, row.metadata_json) == (
        "project_index.unmapped",
        '{"wasDefault":true,"overridesRemoved":1}',
    )
    assert count(admin, user_index_overrides) == 0
    # The chat operation arrives in stage 3; its index resolution is what answers it.
    principal = Principal(
        oid=alice.oid,
        tid=alice.tenant_id,
        azp=SESSION_AUTH.web_client_id,
        client="web",
        roles=frozenset({"User"}),
    )
    checks = AccessChecks(api_sql_engine)
    accessible = await checks.authorize_project_for_chat(principal, project["id"])
    with pytest.raises(ProjectIndexUnavailable):
        await checks.resolve_chat_index(principal, accessible)


# --- T-112: the mapping invariants through the mapping operations ---------------------------


def mappings_of(engine: Engine, project_id: UUID) -> dict[UUID, bool]:
    with engine.connect() as connection:
        return {
            row.search_index_id: bool(row.is_default)
            for row in connection.execute(
                select(project_indexes.c.search_index_id, project_indexes.c.is_default).where(
                    project_indexes.c.project_id == project_id
                )
            )
        }


async def test_t112_the_first_mapping_is_the_default_and_one_default_remains_throughout(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    first, second = search_index_row(status="ready"), search_index_row(status="ready")
    put(admin, (projects, project), (search_indexes, first), (search_indexes, second))
    base = f"/v1/admin/projects/{project['id']}/indexes"

    async def call(method: str, index: dict[str, Any], body: Any = None) -> Any:
        return await api.client.request(
            method,
            f"{base}/{index['id']}",
            json=body,
            headers=if_match(carol, version_of(admin, project["id"])),
        )

    def one_default() -> UUID | None:
        mapped = mappings_of(admin, project["id"])
        defaults = [index for index, is_default in mapped.items() if is_default]
        # Every state a project passes through holds no mapping or exactly one default.
        assert len(defaults) == (1 if mapped else 0), mapped
        return defaults[0] if defaults else None

    refused_first = await call("PUT", first, {"isDefault": False})
    assert (refused_first.status_code, refused_first.json()["code"]) == (
        409,
        "default_index_required",
    )
    assert one_default() is None
    assert (await call("PUT", first, {"isDefault": True})).status_code == 201
    assert one_default() == first["id"]
    assert (await call("PUT", second, {"isDefault": False})).status_code == 201
    assert one_default() == first["id"]
    assert (await call("PUT", second, {"isDefault": True})).status_code == 200
    assert one_default() == second["id"]
    in_use = await call("DELETE", second)
    assert (in_use.status_code, in_use.json()["code"]) == (409, "default_index_in_use")
    assert one_default() == second["id"]
    assert (await call("PUT", first, {"isDefault": True})).status_code == 200
    assert one_default() == first["id"]
    assert (await call("DELETE", second)).json()["removed"] is True
    assert one_default() == first["id"]
    assert (await call("DELETE", first)).json()["removed"] is True
    assert one_default() is None


# --- T-049: the order of checks -------------------------------------------------------------


async def test_t049_a_user_token_is_refused_before_body_and_ids(
    api: ApiUnderTest, alice: SharedPrincipal
) -> None:
    for method, path in (
        ("POST", "/v1/admin/projects"),
        ("PATCH", f"/v1/admin/projects/{uuid4()}"),
        ("PUT", f"/v1/admin/projects/{uuid4()}/members/{uuid4()}"),
        ("DELETE", f"/v1/admin/projects/{uuid4()}/members/{uuid4()}"),
        ("PUT", f"/v1/admin/projects/{uuid4()}/indexes/{uuid4()}"),
        ("DELETE", f"/v1/admin/projects/{uuid4()}/indexes/{uuid4()}"),
        ("GET", "/v1/admin/users?limit=0"),
    ):
        response = await api.client.request(
            method,
            path,
            content=b"{not json",
            headers=alice.authorization() | {"Content-Type": "application/json"},
        )
        assert (response.status_code, response.json()["code"]) == (403, "admin_required")


async def test_t049_the_answers_come_404_428_412_409(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    put(admin, (projects, project))
    archive(admin, project["id"], carol.oid)
    current = version_of(admin, project["id"])
    stale = encode_row_version(bytes(8))
    member = f"/v1/admin/projects/{project['id']}/members/{uuid4()}"

    missing = await api.client.delete(
        f"/v1/admin/projects/{uuid4()}/members/{uuid4()}", headers=carol.authorization()
    )
    required = await api.client.delete(member, headers=carol.authorization())
    failed = await api.client.delete(member, headers=if_match(carol, stale))
    archived = await api.client.delete(member, headers=if_match(carol, current))

    assert [
        (response.status_code, response.json()["code"])
        for response in (missing, required, failed, archived)
    ] == [
        (404, "project_not_found"),
        (428, "precondition_required"),
        (412, "precondition_failed"),
        (409, "project_archived"),
    ]


# --- T-056: optimistic concurrency ----------------------------------------------------------


async def test_t056_a_stale_version_changes_nothing_on_each_operation(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, (index,) = mapped_project(admin)
    person = user_row()
    other = search_index_row(status="ready")
    put(admin, (users, person), (search_indexes, other))
    stale = if_match(carol, encode_row_version(bytes(8)))
    base = f"/v1/admin/projects/{project['id']}"
    before = version_of(admin, project["id"])

    answers = [
        await api.client.patch(base, json={"description": "x"}, headers=stale),
        await api.client.put(f"{base}/members/{person['object_id']}", headers=stale),
        await api.client.delete(f"{base}/members/{person['object_id']}", headers=stale),
        await api.client.put(
            f"{base}/indexes/{other['id']}", json={"isDefault": True}, headers=stale
        ),
        await api.client.delete(f"{base}/indexes/{index['id']}", headers=stale),
    ]

    assert [response.status_code for response in answers] == [412] * 5
    assert version_of(admin, project["id"]) == before
    assert audit(admin, project["id"]) == []


async def test_t056_a_call_that_changes_nothing_keeps_the_version(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, (index,) = mapped_project(admin)
    person = user_row()
    put(
        admin,
        (users, person),
        (project_members, project_member_row(person["object_id"], project["id"])),
    )
    version = version_of(admin, project["id"])
    headers = if_match(carol, version)
    base = f"/v1/admin/projects/{project['id']}"

    unchanged = await api.client.patch(
        base, json={"name": project["name"], "status": "active"}, headers=headers
    )
    member = await api.client.put(f"{base}/members/{person['object_id']}", headers=headers)
    absent = await api.client.delete(f"{base}/members/{uuid4()}", headers=headers)
    mapping = await api.client.put(
        f"{base}/indexes/{index['id']}", json={"isDefault": True}, headers=headers
    )
    unmapped = await api.client.delete(f"{base}/indexes/{uuid4()}", headers=headers)

    assert unchanged.json()["rowVersion"] == version
    assert (member.status_code, member.json()["projectRowVersion"]) == (200, version)
    assert absent.json() == {"removed": False, "projectRowVersion": version}
    assert (mapping.status_code, mapping.json()["projectRowVersion"]) == (200, version)
    assert unmapped.json() == {"removed": False, "projectRowVersion": version}
    assert version_of(admin, project["id"]) == version
    assert audit(admin, project["id"]) == []


async def test_t056_two_concurrent_default_changes_leave_one_default(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, (_, first, second) = mapped_project(admin, indexes=3)
    headers = if_match(carol, version_of(admin, project["id"]))
    base = f"/v1/admin/projects/{project['id']}/indexes"

    answers = await asyncio.gather(
        api.client.put(f"{base}/{first['id']}", json={"isDefault": True}, headers=headers),
        api.client.put(f"{base}/{second['id']}", json={"isDefault": True}, headers=headers),
    )

    assert sorted(response.status_code for response in answers) == [200, 412]
    defaults = count(
        admin,
        project_indexes,
        project_indexes.c.project_id == project["id"],
        project_indexes.c.is_default == BIT_TRUE,
    )
    assert defaults == 1


async def test_t056_t010_an_added_member_reaches_the_project_at_the_returned_version(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, dave: SharedPrincipal
) -> None:
    project = project_row()
    put(admin, (users, user_row(object_id=dave.oid)), (projects, project))
    base = f"/v1/admin/projects/{project['id']}"

    added = await api.client.put(
        f"{base}/members/{dave.oid}", headers=if_match(carol, version_of(admin, project["id"]))
    )
    read = await api.client.get(base, headers=carol.authorization())
    listed = await api.client.get("/v1/projects", headers=dave.authorization())
    removed = await api.client.delete(
        f"{base}/members/{dave.oid}", headers=if_match(carol, added.json()["projectRowVersion"])
    )
    after = await api.client.get("/v1/projects", headers=dave.authorization())

    assert added.status_code == 201
    assert added.json()["member"]["grantedBy"]["userId"] == str(carol.oid)
    assert added.json()["projectRowVersion"] == read.json()["rowVersion"]
    assert [item["projectId"] for item in listed.json()["items"]] == [str(project["id"])]
    assert removed.json()["removed"] is True
    assert after.json()["items"] == []
    rows = audit(admin, project["id"])
    assert [(row.action, row.subject_object_id) for row in rows] == [
        ("project_member.added", dave.oid),
        ("project_member.removed", dave.oid),
    ]
    assert rows[1].metadata_json == '{"grantSource":"admin"}'


# --- T-054: keyset pages under inserts ------------------------------------------------------


async def pages(api: ApiUnderTest, caller: SharedPrincipal, path: str, between: Any) -> list[str]:
    """Every item ID across the pages of `path` (`limit` 2), calling `between` after each page."""
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({} if cursor is None else {"cursor": cursor})
        page = (await api.client.get(path, params=params, headers=caller.authorization())).json()
        seen.extend(
            item.get("projectId") or item.get("userId") or item["indexId"] for item in page["items"]
        )
        cursor = page["nextCursor"]
        if cursor is None:
            return seen
        between()


async def test_t054_member_pages_stay_complete_while_members_join(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project = project_row()
    people = [user_row(display_name=f"Person {n}") for n in range(5)]
    put(
        admin,
        (projects, project),
        *((users, person) for person in people),
        *((project_members, project_member_row(p["object_id"], project["id"])) for p in people),
    )

    def join() -> None:
        late = user_row()
        put(
            admin,
            (users, late),
            (project_members, project_member_row(late["object_id"], project["id"])),
        )

    seen = await pages(api, carol, f"/v1/admin/projects/{project['id']}/members", join)

    assert len(seen) == len(set(seen))
    assert {str(person["object_id"]) for person in people} <= set(seen)


async def test_t054_mapping_pages_put_the_default_first_and_stay_complete(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    project, rows = mapped_project(admin, indexes=5)

    def map_another() -> None:
        late = search_index_row(status="ready")
        put(
            admin,
            (search_indexes, late),
            (project_indexes, project_index_row(project["id"], late["id"])),
        )

    seen = await pages(api, carol, f"/v1/admin/projects/{project['id']}/indexes", map_another)

    assert seen[0] == str(rows[0]["id"])
    assert len(seen) == len(set(seen))
    assert {str(row["id"]) for row in rows} <= set(seen)


async def test_t054_project_pages_stay_complete_while_projects_are_created(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    created = [project_row() for _ in range(5)]
    put(admin, *((projects, row) for row in created))

    seen = await pages(
        api, carol, "/v1/admin/projects", lambda: put(admin, (projects, project_row()))
    )

    assert len(seen) == len(set(seen))
    assert {str(row["id"]) for row in created} <= set(seen)


async def test_t054_user_pages_stay_complete_while_people_sign_in(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    people = [user_row() for _ in range(5)]
    put(admin, *((users, person) for person in people))

    seen = await pages(api, carol, "/v1/admin/users", lambda: put(admin, (users, user_row())))

    assert len(seen) == len(set(seen))
    assert {str(person["object_id"]) for person in people} <= set(seen)
