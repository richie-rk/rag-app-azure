"""Unit tests of the access functions (T-009; `11-api.md` sections 1.4.2 to 1.4.5 and acceptance
criterion 15), on a data-access layer without SQL: each SQL row the queries can return is
queued, and the functions' decisions are checked."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest

from docchat_core.access.cache import IndexRecordCache
from docchat_core.access.checks import AccessChecks, require_admin
from docchat_core.access.exceptions import AdminRequired, ProjectIndexUnavailable, ProjectNotFound
from docchat_core.access.models import AccessibleProject, ResolvedIndex
from docchat_core.auth.principal import Principal
from docchat_core.sql.engine import SqlUnavailable
from docchat_testing.clock import FakeClock

PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
OTHER_INDEX = UUID("9c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e63")
PHYSICAL = UUID("0d5e6f7a-8b9c-4dad-9e1f-2a3b4c5d6e64")
EXPIRES = datetime(2026, 1, 3, 9, tzinfo=UTC)

type Caller = Literal["administrator", "member", "non-member"]
type ProjectState = Literal["active", "archived", "missing"]


def principal(*, admin: bool = False) -> Principal:
    return Principal(
        oid=uuid4(),
        tid=uuid4(),
        azp=uuid4(),
        client="web",
        roles=frozenset({"Admin"} if admin else {"User"}),
    )


def project_row(*, status: str = "active", is_member: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=PROJECT,
        name="Field notes",
        description="Notes",
        instructions="Be brief",
        example_questions_json='["What changed?"]',
        status=status,
        is_member=1 if is_member else 0,
    )


def choice_row(*, override: UUID | None = None, default: UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        override_index_id=override,
        override_expires_at=None if override is None else datetime(2026, 1, 3, 9),  # noqa: DTZ001
        default_index_id=default,
    )


def record_row(search_index_id: UUID = INDEX, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=search_index_id,
        status="ready" if active else "provisioning",
        physical_index_id=PHYSICAL if active else None,
        name="docchat-0123456789abcdef0123456789abcdef-g1" if active else None,
        schema_version=1 if active else None,
        embedding_model="text-embedding-3-large" if active else None,
        embedding_dimensions=1024 if active else None,
        query_embedding_deployment="embed-query-te3-large-v1" if active else None,
    )


def accessible(access: Literal["admin", "member"] = "member") -> AccessibleProject:
    return AccessibleProject(
        id=PROJECT,
        name="Field notes",
        description="Notes",
        instructions="Be brief",
        example_questions=("What changed?",),
        access=access,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def checks(fake_sql: Any, clock: FakeClock) -> AccessChecks:
    return AccessChecks(fake_sql, clock=clock)


# --- require_admin ------------------------------------------------------------------------------


def test_require_admin_passes_an_administrator() -> None:
    require_admin(principal(admin=True))


def test_require_admin_refuses_a_user() -> None:
    with pytest.raises(AdminRequired):
        require_admin(principal())


# --- authorize_project_for_chat -----------------------------------------------------------------


_MATRIX: list[tuple[Caller, ProjectState]] = [
    (caller, state)
    for caller in ("administrator", "member", "non-member")
    for state in ("active", "archived", "missing")
]


@pytest.mark.parametrize(("caller", "state"), _MATRIX)
async def test_t009_only_an_active_project_of_an_administrator_or_member_is_reachable(
    checks: AccessChecks, fake_sql: Any, caller: Caller, state: ProjectState
) -> None:
    row = None if state == "missing" else project_row(status=state, is_member=caller == "member")
    fake_sql.queue("authorize_project", row)
    who = principal(admin=caller == "administrator")

    if state == "active" and caller != "non-member":
        project = await checks.authorize_project_for_chat(who, PROJECT)
        assert project == accessible("admin" if caller == "administrator" else "member")
    else:
        with pytest.raises(ProjectNotFound) as caught:
            await checks.authorize_project_for_chat(who, PROJECT)
        # Every refusal is the same exception with the same body.
        assert type(caught.value) is ProjectNotFound
        assert (caught.value.code, caught.value.detail) == (
            ProjectNotFound().code,
            ProjectNotFound().detail,
        )
    (call,) = fake_sql.calls
    assert call.parameters == {"oid": who.oid, "project_id": PROJECT}


async def test_an_administrator_member_reaches_as_administrator(
    checks: AccessChecks, fake_sql: Any
) -> None:
    fake_sql.queue("authorize_project", project_row(is_member=True))
    project = await checks.authorize_project_for_chat(principal(admin=True), PROJECT)
    assert project.access == "admin"


async def test_every_check_reads_sql_afresh(checks: AccessChecks, fake_sql: Any) -> None:
    who = principal()
    fake_sql.queue("authorize_project", project_row(is_member=True), project_row(is_member=False))

    await checks.authorize_project_for_chat(who, PROJECT)
    with pytest.raises(ProjectNotFound):
        await checks.authorize_project_for_chat(who, PROJECT)
    assert fake_sql.names() == ["authorize_project", "authorize_project"]


async def test_an_unreachable_database_fails_closed(checks: AccessChecks, fake_sql: Any) -> None:
    fake_sql.queue("authorize_project", SqlUnavailable("exhausted", 40613))
    with pytest.raises(SqlUnavailable):
        await checks.authorize_project_for_chat(principal(admin=True), PROJECT)


async def test_the_check_is_traced(checks: AccessChecks, fake_sql: Any, captured: Any) -> None:
    fake_sql.queue("authorize_project", project_row(is_member=True), None)

    await checks.authorize_project_for_chat(principal(), PROJECT)
    with pytest.raises(ProjectNotFound):
        await checks.authorize_project_for_chat(principal(), PROJECT)

    assert captured.span_attributes("docchat.access.authorize_project") == [
        {
            "docchat.project_id": str(PROJECT),
            "docchat.access.outcome": "allowed",
            "docchat.access.via": "member",
        },
        {"docchat.project_id": str(PROJECT), "docchat.access.outcome": "not_found"},
    ]
    assert all(
        span.events == () for span in captured.finished_spans("docchat.access.authorize_project")
    )


# --- resolve_chat_index -------------------------------------------------------------------------


async def test_t009_an_unexpired_override_wins_over_the_default(
    checks: AccessChecks, fake_sql: Any, captured: Any
) -> None:
    who = principal()
    fake_sql.queue("resolve_index", choice_row(override=OTHER_INDEX, default=INDEX))
    fake_sql.queue("read_index_record", record_row(OTHER_INDEX))

    resolved = await checks.resolve_chat_index(who, accessible())

    assert (resolved.search_index_id, resolved.source) == (OTHER_INDEX, "user_override")
    assert resolved.override_expires_at == EXPIRES
    assert resolved.physical_index.id == PHYSICAL
    assert fake_sql.calls[0].parameters == {"oid": who.oid, "project_id": PROJECT}
    assert fake_sql.calls[1].parameters == {"search_index_id": OTHER_INDEX}
    assert captured.points("docchat.access.index_overrides_used") == {(): 1}
    assert captured.events("index_override_used") == [
        {
            "oid": str(who.oid),
            "projectId": str(PROJECT),
            "indexId": str(OTHER_INDEX),
            "expiresAt": "2026-01-03T09:00:00.000Z",
        }
    ]
    (attributes,) = captured.span_attributes("docchat.access.resolve_index")
    assert attributes["docchat.access.index_source"] == "user_override"


async def test_t009_without_an_unexpired_override_the_default_serves(
    checks: AccessChecks, fake_sql: Any, captured: Any
) -> None:
    # Q2 leaves out an expired override, so its row shows only the default.
    fake_sql.queue("resolve_index", choice_row(default=INDEX))
    fake_sql.queue("read_index_record", record_row(INDEX))

    resolved = await checks.resolve_chat_index(principal(), accessible())

    assert resolved == ResolvedIndex(
        search_index_id=INDEX,
        source="project_default",
        override_expires_at=None,
        physical_index=resolved.physical_index,
    )
    assert resolved.physical_index.name == "docchat-0123456789abcdef0123456789abcdef-g1"
    assert captured.points("docchat.access.index_overrides_used") == {}
    assert captured.span_attributes("docchat.access.resolve_index") == [
        {
            "docchat.project_id": str(PROJECT),
            "docchat.index_id": str(INDEX),
            "docchat.access.index_source": "project_default",
            "docchat.access.outcome": "ok",
            "docchat.access.index_cache": "miss",
        }
    ]


async def test_t009_neither_override_nor_default_is_unavailable(
    checks: AccessChecks, fake_sql: Any, captured: Any
) -> None:
    fake_sql.queue("resolve_index", choice_row())

    with pytest.raises(ProjectIndexUnavailable):
        await checks.resolve_chat_index(principal(), accessible())

    assert fake_sql.names() == ["resolve_index"]
    assert captured.span_attributes("docchat.access.resolve_index") == [
        {"docchat.project_id": str(PROJECT), "docchat.access.outcome": "unavailable"}
    ]


@pytest.mark.parametrize("record", [record_row(active=False), None])
async def test_t009_no_active_physical_index_is_unavailable(
    checks: AccessChecks, fake_sql: Any, record: SimpleNamespace | None
) -> None:
    fake_sql.queue("resolve_index", choice_row(default=INDEX))
    fake_sql.queue("read_index_record", record)

    with pytest.raises(ProjectIndexUnavailable):
        await checks.resolve_chat_index(principal(), accessible())


async def test_t009_an_override_without_an_active_index_never_falls_back(
    checks: AccessChecks, fake_sql: Any, captured: Any
) -> None:
    fake_sql.queue("resolve_index", choice_row(override=OTHER_INDEX, default=INDEX))
    fake_sql.queue("read_index_record", record_row(OTHER_INDEX, active=False))

    with pytest.raises(ProjectIndexUnavailable):
        await checks.resolve_chat_index(principal(), accessible())
    assert captured.events("index_override_used") == []


@pytest.mark.parametrize("status", ["reingest_required", "reingesting", "rebuilding"])
async def test_a_logical_index_being_rebuilt_still_serves(
    checks: AccessChecks, fake_sql: Any, status: str
) -> None:
    row = record_row()
    row.status = status
    fake_sql.queue("resolve_index", choice_row(default=INDEX))
    fake_sql.queue("read_index_record", row)

    assert (await checks.resolve_chat_index(principal(), accessible())).search_index_id == INDEX


async def test_t009_an_active_record_is_served_from_the_cache_for_60_seconds(
    checks: AccessChecks, fake_sql: Any, clock: FakeClock, captured: Any
) -> None:
    fake_sql.queue("resolve_index", *(choice_row(default=INDEX) for _ in range(3)))
    fake_sql.queue("read_index_record", record_row(), record_row())

    await checks.resolve_chat_index(principal(), accessible())
    clock.advance(59)
    await checks.resolve_chat_index(principal(), accessible())
    clock.advance(1)
    await checks.resolve_chat_index(principal(), accessible())

    assert fake_sql.names() == [
        "resolve_index",
        "read_index_record",
        "resolve_index",
        "resolve_index",
        "read_index_record",
    ]
    caches = [
        attributes["docchat.access.index_cache"]
        for attributes in captured.span_attributes("docchat.access.resolve_index")
    ]
    assert caches == ["miss", "hit", "miss"]


async def test_t009_a_record_without_an_active_index_is_not_cached(
    checks: AccessChecks, fake_sql: Any
) -> None:
    fake_sql.queue("resolve_index", choice_row(default=INDEX), choice_row(default=INDEX))
    fake_sql.queue("read_index_record", record_row(active=False), record_row())

    with pytest.raises(ProjectIndexUnavailable):
        await checks.resolve_chat_index(principal(), accessible())
    resolved = await checks.resolve_chat_index(principal(), accessible())

    assert resolved.physical_index.id == PHYSICAL
    assert fake_sql.names().count("read_index_record") == 2


async def test_the_cache_may_be_shared(fake_sql: Any, clock: FakeClock) -> None:
    cache = IndexRecordCache(clock)
    first, second = AccessChecks(fake_sql, cache=cache), AccessChecks(fake_sql, cache=cache)
    fake_sql.queue("resolve_index", choice_row(default=INDEX), choice_row(default=INDEX))
    fake_sql.queue("read_index_record", record_row())

    await first.resolve_chat_index(principal(), accessible())
    await second.resolve_chat_index(principal(), accessible())

    assert fake_sql.names().count("read_index_record") == 1
    assert len(cache) == 1


# --- list_reachable_project_ids -----------------------------------------------------------------


async def test_an_administrator_reaches_every_active_project(
    checks: AccessChecks, fake_sql: Any, captured: Any
) -> None:
    ids = [uuid4(), uuid4()]
    fake_sql.queue("reachable_projects", [SimpleNamespace(id=value) for value in ids])

    assert await checks.list_reachable_project_ids(principal(admin=True)) == frozenset(ids)
    assert fake_sql.calls[0].parameters is None
    assert captured.span_attributes("docchat.access.reachable_projects") == [
        {"docchat.access.project_count": 2}
    ]


async def test_a_user_reaches_the_projects_of_their_memberships(
    checks: AccessChecks, fake_sql: Any
) -> None:
    who = principal()
    fake_sql.queue("reachable_projects", [])

    assert await checks.list_reachable_project_ids(who) == frozenset()
    assert fake_sql.calls[0].parameters == {"oid": who.oid}


# --- list_my_projects ---------------------------------------------------------------------------


def summary_row(name: str, *, has_default: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        description="",
        example_questions_json="[]",
        has_default_index=1 if has_default else 0,
    )


async def test_a_page_reads_one_extra_row_for_the_next_cursor(
    checks: AccessChecks, fake_sql: Any
) -> None:
    fake_sql.queue("list_my_projects", [summary_row("A"), summary_row("B"), summary_row("C")])

    page = await checks.list_my_projects(principal(admin=True), None, 2)

    assert [item.name for item in page.items] == ["A", "B"]
    assert page.next_after == "B"
    assert fake_sql.calls[0].parameters == {"limit_plus_one": 3}


async def test_the_last_page_has_no_cursor(checks: AccessChecks, fake_sql: Any) -> None:
    who = principal()
    fake_sql.queue("list_my_projects", [summary_row("C", has_default=False)])

    page = await checks.list_my_projects(who, "B", 2)

    assert [(item.name, item.has_default_index) for item in page.items] == [("C", False)]
    assert page.next_after is None
    assert fake_sql.calls[0].parameters == {"oid": who.oid, "limit_plus_one": 3, "after_name": "B"}


async def test_an_empty_first_page(checks: AccessChecks, fake_sql: Any) -> None:
    fake_sql.queue("list_my_projects", [])
    page = await checks.list_my_projects(principal(), None, 50)
    assert (page.items, page.next_after) == ((), None)


@pytest.mark.parametrize("limit", [0, 101])
async def test_the_limit_lies_within_1_and_100(checks: AccessChecks, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await checks.list_my_projects(principal(), None, limit)
