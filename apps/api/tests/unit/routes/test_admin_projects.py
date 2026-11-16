"""Unit tests of the administrator project, member and mapping operations (`11-api.md`
sections 3.7 and 3.9) through the in-process app.

Most tests replace the repository functions with recorders, so they prove what each handler
validates, what it passes on and how it answers each result. The `If-Match` tests run the real
units of work on a scripted connection, so the order `404`, `428`, `412`, `409` is the
repository's own.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest

from docchat_api.app import create_app
from docchat_api.http.models import RequestModel
from docchat_api.http.paging import encode_cursor
from docchat_api.http.preconditions import IfMatchHeader
from docchat_api.routes.admin_projects import (
    CREATE_EXAMPLE,
    MAPPING_EXAMPLE,
    UPDATE_EXAMPLE,
    AdminProjectsCursor,
    MappingsCursor,
    MembersCursor,
    ProjectCreate,
    ProjectIndexMappingUpdate,
    ProjectUpdate,
    refusal_problem,
)
from docchat_core.audit.models import AuditEvent, ProjectCreatedMetadata
from docchat_core.models import encode_row_version
from docchat_core.sql.repositories import projects as repo
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

PROJECT = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
USER = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
INDEX = UUID("3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f")
RV = bytes(range(1, 9))
NEW_RV = bytes(range(11, 19))
STAMP = datetime(2026, 10, 30, 9, 15, 2, 123000, tzinfo=UTC)
STAMP_TEXT = "2026-10-30T09:15:02.123Z"
IF_MATCH = {"If-Match": f'"{encode_row_version(RV)}"'}


class FakeSql(Protocol):
    calls: list[Any]

    def names(self) -> list[str]: ...


@dataclass
class Recorder:
    """A stand-in repository function: records its keyword arguments, answers `result`."""

    result: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, engine: object, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Any], Recorder]:
    def install(name: str, result: Any) -> Recorder:
        recorder = Recorder(result)
        monkeypatch.setattr(repo, name, recorder)
        return recorder

    return install


def admin_project(**overrides: Any) -> repo.AdminProjectRow:
    values: dict[str, Any] = {
        "id": PROJECT,
        "name": "Field notes",
        "description": "Notes",
        "instructions": "Line one\nLine two",
        "example_questions": ("What changed?",),
        "is_default": False,
        "status": "active",
        "member_count": 2,
        "index_count": 1,
        "default_index": repo.IndexRefRow(index_id=INDEX, display_name="Handbook"),
        "created_at": STAMP,
        "created_by": repo.UserRefRow(user_id=USER, display_name="Ada"),
        "updated_at": STAMP,
        "updated_by": repo.UserRefRow(user_id=USER, display_name=""),
        "archived_at": None,
        "archived_by": None,
        "row_version": NEW_RV,
    } | overrides
    return repo.AdminProjectRow(**values)


def admin_project_json(**overrides: Any) -> dict[str, Any]:
    return {
        "projectId": str(PROJECT),
        "name": "Field notes",
        "description": "Notes",
        "instructions": "Line one\nLine two",
        "exampleQuestions": ["What changed?"],
        "isDefault": False,
        "status": "active",
        "memberCount": 2,
        "indexCount": 1,
        "defaultIndex": {"indexId": str(INDEX), "displayName": "Handbook"},
        "createdAt": STAMP_TEXT,
        "createdBy": {"userId": str(USER), "displayName": "Ada"},
        "updatedAt": STAMP_TEXT,
        "updatedBy": {"userId": str(USER), "displayName": ""},
        "archivedAt": None,
        "archivedBy": None,
        "rowVersion": encode_row_version(NEW_RV),
    } | overrides


def member(**overrides: Any) -> repo.ProjectMemberRow:
    values: dict[str, Any] = {
        "user_id": USER,
        "display_name": "Ada",
        "email": "ada@example.test",
        "grant_source": "admin",
        "granted_at": STAMP,
        "granted_by": repo.UserRefRow(user_id=PROJECT, display_name="Carol"),
        "last_seen_at": STAMP,
    } | overrides
    return repo.ProjectMemberRow(**values)


def mapping(**overrides: Any) -> repo.ProjectIndexMappingRow:
    values: dict[str, Any] = {
        "index_id": INDEX,
        "display_name": "Handbook",
        "index_status": "ready",
        "is_default": True,
        "mapped_at": STAMP,
        "mapped_by": repo.UserRefRow(user_id=USER, display_name="Ada"),
        "updated_at": STAMP,
    } | overrides
    return repo.ProjectIndexMappingRow(**values)


def created_event(caller: UUID) -> AuditEvent:
    return AuditEvent(
        action="project.created",
        actor_type="user",
        actor_object_id=caller,
        target_type="project",
        target_id=str(PROJECT),
        project_id=PROJECT,
        metadata=ProjectCreatedMetadata(is_default=False),
    )


# --- The platform around the operations -------------------------------------------------------


async def test_a_user_gets_admin_required_before_any_lookup(
    started: ApiUnderTest, fake_sql: FakeSql, alice: SharedPrincipal
) -> None:
    response = await started.client.patch(
        f"/v1/admin/projects/{PROJECT}", content=b"{not json", headers=alice.authorization()
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"
    assert "admin_read_project" not in fake_sql.names()


@pytest.mark.parametrize(
    ("refusal", "status", "code"),
    [
        ("project_not_found", 404, "project_not_found"),
        ("user_not_found", 404, "user_not_found"),
        ("index_not_found", 404, "index_not_found"),
        ("precondition_required", 428, "precondition_required"),
        ("precondition_failed", 412, "precondition_failed"),
        ("project_archived", 409, "project_archived"),
        ("project_name_taken", 409, "project_name_taken"),
        ("index_not_mappable", 409, "index_not_mappable"),
        ("default_index_required", 409, "default_index_required"),
        ("default_index_in_use", 409, "default_index_in_use"),
        ("status_change_with_fields", 422, "validation_failed"),
    ],
)
def test_each_refusal_has_its_problem(refusal: Any, status: int, code: str) -> None:
    problem = refusal_problem(repo.Refused(refusal))

    assert (problem.status, problem.entry.code) == (status, code)
    if refusal == "status_change_with_fields":
        assert [(issue.path, issue.reason) for issue in problem.errors] == [
            ("body.status", "not_allowed")
        ]


# --- adminListProjects -----------------------------------------------------------------------


def summary(name: str) -> repo.AdminProjectSummaryRow:
    return repo.AdminProjectSummaryRow(
        id=uuid4(),
        name=name,
        description="",
        is_default=False,
        status="archived",
        member_count=0,
        index_count=0,
        default_index=None,
        updated_at=STAMP,
        row_version=RV,
    )


async def test_the_project_list_passes_its_filters(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("read_admin_projects", [summary("A"), summary("B"), summary("C")])

    response = await started.client.get(
        "/v1/admin/projects",
        params={"status": "archived", "q": "  Fie\tld ", "limit": 2},
        headers=carol.authorization(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["items"]] == ["A", "B"]
    assert body["items"][0] | {"projectId": None} == {
        "projectId": None,
        "name": "A",
        "description": "",
        "isDefault": False,
        "status": "archived",
        "memberCount": 0,
        "indexCount": 0,
        "defaultIndex": None,
        "updatedAt": STAMP_TEXT,
        "rowVersion": encode_row_version(RV),
    }
    assert body["nextCursor"] == encode_cursor(AdminProjectsCursor(n="B", s="archived", q="Fie ld"))
    assert recorder.calls == [
        {"status": "archived", "q": "Fie ld", "after_name": None, "limit_plus_one": 3}
    ]


async def test_the_project_list_follows_its_cursor(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("read_admin_projects", [summary("C")])
    cursor = encode_cursor(AdminProjectsCursor(n="B", s="all", q=None))

    response = await started.client.get(
        "/v1/admin/projects", params={"cursor": cursor}, headers=carol.authorization()
    )

    assert response.json()["nextCursor"] is None
    assert recorder.calls == [{"status": None, "q": None, "after_name": "B", "limit_plus_one": 51}]


async def test_a_cursor_of_other_filters_is_invalid(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("read_admin_projects", [])
    cursor = encode_cursor(AdminProjectsCursor(n="B", s="active", q=None))

    response = await started.client.get(
        "/v1/admin/projects",
        params={"cursor": cursor, "status": "archived"},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]
    assert recorder.calls == []


async def test_the_project_list_validates_its_parameters(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    response = await started.client.get(
        "/v1/admin/projects",
        params={"status": "deleted", "q": "x" * 81, "limit": 0, "projectId": "x"},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"path": "query.status", "reason": "not_allowed"},
        {"path": "query.q", "reason": "too_long"},
        {"path": "query.limit", "reason": "out_of_range"},
        {"path": "query.projectId", "reason": "unknown_field"},
    ]


# --- adminCreateProject ----------------------------------------------------------------------


async def test_a_project_is_created(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    captured: Any,
) -> None:
    recorder = stub(
        "create_project",
        repo.ProjectWrite(project=admin_project(), events=(created_event(carol.oid),)),
    )
    first, second = uuid4(), uuid4()

    response = await started.client.post(
        "/v1/admin/projects",
        json={
            "name": " Fie\u0301ld   notes ",
            "description": "Notes",
            "instructions": "Line one\r\nLine two\r",
            "exampleQuestions": ["What  changed?"],
            "initialMemberIds": [str(first).upper(), str(second)],
        },
        headers=carol.authorization(),
    )

    assert response.status_code == 201
    assert response.json() == admin_project_json()
    (call,) = recorder.calls
    project = call["project"]
    assert call["caller"] == carol.oid
    assert project.name == "Fiéld notes"
    assert project.instructions == "Line one\nLine two"
    assert project.example_questions == ("What changed?",)
    assert project.is_default is False
    assert project.initial_member_ids == (first, second)
    assert project.id.version == 4
    assert captured.events("admin_change") == [
        {"action": "project.created", "targetType": "project", "projectId": str(PROJECT)}
    ]


async def test_the_create_defaults_fill_absent_fields(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("create_project", repo.ProjectWrite(project=admin_project(), events=()))

    await started.client.post(
        "/v1/admin/projects", json={"name": "Field notes"}, headers=carol.authorization()
    )

    project = recorder.calls[0]["project"]
    assert (project.description, project.instructions, project.example_questions) == ("", "", ())
    assert (project.is_default, project.initial_member_ids) == (False, ())


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        (
            {"name": "A\u202eB"},
            [{"path": "body.name", "reason": "invalid_characters"}],
        ),
        (
            {"name": "A\u200bB"},
            [{"path": "body.name", "reason": "invalid_characters"}],
        ),
        (
            {"name": "A", "exampleQuestions": ["One?", "Two?", "one?", "TWO?"]},
            [
                {"path": "body.exampleQuestions.2", "reason": "duplicate"},
                {"path": "body.exampleQuestions.3", "reason": "duplicate"},
            ],
        ),
        (
            {"name": "A", "initialMemberIds": [str(USER), str(USER).upper()]},
            [{"path": "body.initialMemberIds.1", "reason": "duplicate"}],
        ),
        (
            {"name": "A", "initialMemberIds": [str(uuid4()) for _ in range(51)]},
            [{"path": "body.initialMemberIds", "reason": "too_many_items"}],
        ),
        (
            {"name": "A", "initialMemberIds": ["{" + str(USER) + "}"]},
            [{"path": "body.initialMemberIds.0", "reason": "invalid_uuid"}],
        ),
        (
            {"name": "A", "isDefault": "true"},
            [{"path": "body.isDefault", "reason": "invalid_type"}],
        ),
        (
            {"name": "A", "status": "active"},
            [{"path": "body.status", "reason": "unknown_field"}],
        ),
        ({}, [{"path": "body.name", "reason": "required"}]),
    ],
)
async def test_t051_t050_a_create_body_is_validated_before_any_write(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    body: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    recorder = stub("create_project", None)

    response = await started.client.post(
        "/v1/admin/projects", json=body, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == errors
    assert recorder.calls == []


async def test_unknown_initial_members_are_not_found_at_their_index(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("create_project", repo.UnknownMembers(positions=(0, 2)))

    response = await started.client.post(
        "/v1/admin/projects",
        json={"name": "A", "initialMemberIds": [str(uuid4()) for _ in range(3)]},
        headers=carol.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"] == [
        {"path": "body.initialMemberIds.0", "reason": "not_found"},
        {"path": "body.initialMemberIds.2", "reason": "not_found"},
    ]


async def test_a_taken_name_is_a_conflict(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("create_project", repo.Refused("project_name_taken"))

    response = await started.client.post(
        "/v1/admin/projects", json={"name": "A"}, headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (409, "project_name_taken")


# --- adminGetProject -------------------------------------------------------------------------


async def test_a_project_is_read_archived_or_not(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    archived = admin_project(
        status="archived",
        archived_at=STAMP,
        archived_by=repo.UserRefRow(user_id=USER, display_name="Ada"),
    )
    recorder = stub("read_admin_project", archived)

    response = await started.client.get(
        f"/v1/admin/projects/{str(PROJECT).upper()}", headers=carol.authorization()
    )

    assert response.status_code == 200
    assert response.json() == admin_project_json(
        status="archived",
        archivedAt=STAMP_TEXT,
        archivedBy={"userId": str(USER), "displayName": "Ada"},
    )
    assert recorder.calls == [{"project_id": PROJECT}]


async def test_a_missing_project_is_not_found(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("read_admin_project", None)

    response = await started.client.get(
        f"/v1/admin/projects/{uuid4()}", headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


async def test_a_malformed_project_id_is_invalid_uuid(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    response = await started.client.get(
        "/v1/admin/projects/" + PROJECT.hex, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "path.projectId", "reason": "invalid_uuid"}]


# --- adminUpdateProject ----------------------------------------------------------------------


async def test_an_update_passes_only_the_fields_sent(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("update_project", repo.ProjectWrite(project=admin_project(), events=()))

    response = await started.client.patch(
        f"/v1/admin/projects/{PROJECT}",
        json={"description": " New\nnotes ", "isDefault": True},
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.status_code == 200
    assert response.json()["rowVersion"] == encode_row_version(NEW_RV)
    (call,) = recorder.calls
    assert call["changes"] == repo.ProjectChanges(description="New notes", is_default=True)
    assert call["version"] == IfMatchHeader(IF_MATCH["If-Match"])
    assert call["caller"] == carol.oid


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        ({}, [{"path": "body", "reason": "required"}]),
        ({"name": None}, [{"path": "body.name", "reason": "invalid_type"}]),
        ({"status": "deleted"}, [{"path": "body.status", "reason": "not_allowed"}]),
        ({"initialMemberIds": []}, [{"path": "body.initialMemberIds", "reason": "unknown_field"}]),
        ({"exampleQuestions": [""]}, [{"path": "body.exampleQuestions.0", "reason": "too_short"}]),
    ],
)
async def test_an_update_body_is_validated(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    body: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    recorder = stub("update_project", None)

    response = await started.client.patch(
        f"/v1/admin/projects/{PROJECT}", json=body, headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 422
    assert response.json()["errors"] == errors
    assert recorder.calls == []


async def test_a_status_change_with_fields_is_refused_at_body_status(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("update_project", repo.Refused("status_change_with_fields"))

    response = await started.client.patch(
        f"/v1/admin/projects/{PROJECT}",
        json={"status": "archived", "name": "X"},
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body.status", "reason": "not_allowed"}]


# --- The If-Match order, through the real unit of work --------------------------------------


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)
    rowcount: int = 1

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        return self.rows[0]

    def all(self) -> list[Any]:
        return list(self.rows)


class ScriptedConnection:
    def __init__(self) -> None:
        self._answers: defaultdict[int, deque[Result]] = defaultdict(deque)
        self.executed: list[Any] = []

    def answer(self, statement: object, *results: Result) -> None:
        self._answers[id(statement)].extend(results)

    def execute(self, statement: Any, parameters: Mapping[str, Any] | None = None) -> Result:
        self.executed.append(statement)
        queued = self._answers.get(id(statement))
        return queued.popleft() if queued else Result()


@pytest.fixture
def connection(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> ScriptedConnection:
    """The units of work of `repositories.projects` run on a scripted connection."""
    scripted = ScriptedConnection()
    plain_run = fake_sql.run

    async def run(work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        if name.startswith("admin_"):
            return work(scripted)
        return await plain_run(work, name=name, retry=retry)

    monkeypatch.setattr(fake_sql, "run", run)
    return scripted


def locked(status: str = "active") -> Result:
    return Result(
        [
            SimpleNamespace(
                status=status,
                is_default=0,
                name="Field notes",
                description="Notes",
                instructions="",
                example_questions_json="[]",
                row_version=RV,
            )
        ]
    )


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 428, "precondition_required"),
        ({"If-Match": "*"}, 428, "precondition_required"),
        ({"If-Match": f'W/"{encode_row_version(RV)}"'}, 428, "precondition_required"),
        ({"If-Match": encode_row_version(RV)}, 428, "precondition_required"),
        ({"If-Match": '"not-a-version"'}, 412, "precondition_failed"),
        ({"If-Match": f'"{encode_row_version(NEW_RV)}"'}, 412, "precondition_failed"),
        (IF_MATCH, 409, "project_archived"),
    ],
)
async def test_t049_member_removal_answers_428_then_412_then_409(
    *,
    started: ApiUnderTest,
    connection: ScriptedConnection,
    carol: SharedPrincipal,
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    connection.answer(repo.LOCK_PROJECT, locked("archived"))

    response = await started.client.delete(
        f"/v1/admin/projects/{PROJECT}/members/{USER}", headers=carol.authorization() | headers
    )

    assert (response.status_code, response.json()["code"]) == (status, code)
    assert repo.REMOVE_MEMBERSHIP not in connection.executed


async def test_t049_a_missing_project_is_404_before_the_precondition(
    started: ApiUnderTest, connection: ScriptedConnection, carol: SharedPrincipal
) -> None:
    connection.answer(repo.LOCK_PROJECT, Result([]))

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/indexes/{INDEX}",
        json={"isDefault": True},
        headers=carol.authorization(),
    )

    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


async def test_t049_an_unknown_user_is_404_before_the_precondition(
    started: ApiUnderTest, connection: ScriptedConnection, carol: SharedPrincipal
) -> None:
    connection.answer(repo.LOCK_PROJECT, locked())
    connection.answer(repo.LOCK_USER, Result([]))

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/members/{USER}", headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (404, "user_not_found")


async def test_the_current_version_removes_and_returns_the_new_one(
    started: ApiUnderTest, connection: ScriptedConnection, carol: SharedPrincipal
) -> None:
    connection.answer(repo.LOCK_PROJECT, locked())
    connection.answer(repo.REMOVE_MEMBERSHIP, Result([SimpleNamespace(grant_source="admin")]))
    connection.answer(repo.MOVE_VERSION, Result([SimpleNamespace(row_version=NEW_RV)]))

    response = await started.client.delete(
        f"/v1/admin/projects/{PROJECT}/members/{USER}", headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 200
    assert response.json() == {
        "removed": True,
        "projectRowVersion": encode_row_version(NEW_RV),
    }


# --- Members ---------------------------------------------------------------------------------


async def test_the_member_list_pages_by_display_name(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    rows = [member(display_name=name, user_id=uuid4()) for name in ("A", "B", "C")]
    rows[1] = member(display_name="B", grant_source="default", granted_by=None, user_id=USER)
    recorder = stub("read_project_members", rows)

    response = await started.client.get(
        f"/v1/admin/projects/{PROJECT}/members",
        params={"limit": 2},
        headers=carol.authorization(),
    )

    body = response.json()
    assert [item["displayName"] for item in body["items"]] == ["A", "B"]
    assert body["items"][1] == {
        "userId": str(USER),
        "displayName": "B",
        "email": "ada@example.test",
        "grantSource": "default",
        "grantedAt": STAMP_TEXT,
        "grantedBy": None,
        "lastSeenAt": STAMP_TEXT,
    }
    assert body["nextCursor"] == encode_cursor(MembersCursor(d="B", u=USER))
    assert recorder.calls == [{"project_id": PROJECT, "after": None, "limit_plus_one": 3}]


async def test_the_member_list_follows_its_cursor_and_needs_its_project(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("read_project_members", None)
    cursor = encode_cursor(MembersCursor(d="", u=USER))

    response = await started.client.get(
        f"/v1/admin/projects/{PROJECT}/members",
        params={"cursor": cursor},
        headers=carol.authorization(),
    )

    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert recorder.calls[0]["after"] == repo.MemberPosition(display_name="", user_id=USER)


@pytest.mark.parametrize(("created", "status"), [(True, 201), (False, 200)])
async def test_adding_a_member_answers_201_or_200(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    created: bool,
    status: int,
) -> None:
    recorder = stub(
        "add_member",
        repo.MemberChange(
            member=member(),
            project_row_version=NEW_RV if created else RV,
            created=created,
            events=(),
        ),
    )

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/members/{USER}", headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == status
    assert response.json() == {
        "member": {
            "userId": str(USER),
            "displayName": "Ada",
            "email": "ada@example.test",
            "grantSource": "admin",
            "grantedAt": STAMP_TEXT,
            "grantedBy": {"userId": str(PROJECT), "displayName": "Carol"},
            "lastSeenAt": STAMP_TEXT,
        },
        "projectRowVersion": encode_row_version(NEW_RV if created else RV),
    }
    assert recorder.calls[0]["user_id"] == USER


async def test_adding_a_member_refuses_a_body(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("add_member", None)

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/members/{USER}",
        json={"role": "member"},
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body", "reason": "unknown_field"}]
    assert recorder.calls == []


@pytest.mark.parametrize("removed", [True, False])
async def test_removing_a_member_reports_whether_a_row_went(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    removed: bool,
) -> None:
    stub("remove_member", repo.PartRemoval(removed=removed, project_row_version=RV, events=()))

    response = await started.client.delete(
        f"/v1/admin/projects/{PROJECT}/members/{USER}", headers=carol.authorization() | IF_MATCH
    )

    assert response.json() == {"removed": removed, "projectRowVersion": encode_row_version(RV)}


# --- Mappings --------------------------------------------------------------------------------


async def test_the_mapping_list_puts_the_default_first(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    other = uuid4()
    recorder = stub(
        "read_project_mappings",
        [mapping(), mapping(index_id=other, display_name="Archive", is_default=False)],
    )

    response = await started.client.get(
        f"/v1/admin/projects/{PROJECT}/indexes", params={"limit": 1}, headers=carol.authorization()
    )

    body = response.json()
    assert body["items"] == [
        {
            "indexId": str(INDEX),
            "displayName": "Handbook",
            "indexStatus": "ready",
            "isDefault": True,
            "mappedAt": STAMP_TEXT,
            "mappedBy": {"userId": str(USER), "displayName": "Ada"},
            "updatedAt": STAMP_TEXT,
        }
    ]
    assert body["nextCursor"] == encode_cursor(MappingsCursor(f=1, d="Handbook", i=INDEX))
    assert recorder.calls[0]["limit_plus_one"] == 2


async def test_the_mapping_list_follows_its_cursor(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub("read_project_mappings", [])
    cursor = encode_cursor(MappingsCursor(f=0, d="Archive", i=INDEX))

    response = await started.client.get(
        f"/v1/admin/projects/{PROJECT}/indexes",
        params={"cursor": cursor},
        headers=carol.authorization(),
    )

    assert response.json() == {"items": [], "nextCursor": None}
    assert recorder.calls[0]["after"] == repo.MappingPosition(
        is_default=False, display_name="Archive", index_id=INDEX
    )


async def test_a_missing_projects_mappings_are_not_found(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("read_project_mappings", None)

    response = await started.client.get(
        f"/v1/admin/projects/{PROJECT}/indexes", headers=carol.authorization()
    )

    assert response.status_code == 404


@pytest.mark.parametrize(("created", "status"), [(True, 201), (False, 200)])
async def test_mapping_an_index_answers_201_or_200(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    created: bool,
    status: int,
) -> None:
    recorder = stub(
        "map_index",
        repo.MappingChange(
            mapping=mapping(), project_row_version=NEW_RV, created=created, events=()
        ),
    )

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/indexes/{INDEX}",
        json={"isDefault": True},
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.status_code == status
    assert response.json()["projectRowVersion"] == encode_row_version(NEW_RV)
    assert response.json()["mapping"]["indexId"] == str(INDEX)
    (call,) = recorder.calls
    assert (call["project_id"], call["index_id"], call["is_default"]) == (PROJECT, INDEX, True)


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        ({}, [{"path": "body.isDefault", "reason": "required"}]),
        ({"isDefault": 1}, [{"path": "body.isDefault", "reason": "invalid_type"}]),
        (
            {"isDefault": True, "default": True},
            [{"path": "body.default", "reason": "unknown_field"}],
        ),
    ],
)
async def test_a_mapping_body_is_validated(
    started: ApiUnderTest,
    stub: Callable[[str, Any], Recorder],
    carol: SharedPrincipal,
    body: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    recorder = stub("map_index", None)

    response = await started.client.put(
        f"/v1/admin/projects/{PROJECT}/indexes/{INDEX}",
        json=body,
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.json()["errors"] == errors
    assert recorder.calls == []


async def test_unmapping_reports_the_removal(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    recorder = stub(
        "unmap_index", repo.PartRemoval(removed=True, project_row_version=NEW_RV, events=())
    )

    response = await started.client.delete(
        f"/v1/admin/projects/{PROJECT}/indexes/{INDEX}", headers=carol.authorization() | IF_MATCH
    )

    assert response.json() == {"removed": True, "projectRowVersion": encode_row_version(NEW_RV)}
    assert recorder.calls[0]["index_id"] == INDEX


async def test_the_default_in_use_is_a_conflict(
    started: ApiUnderTest, stub: Callable[[str, Any], Recorder], carol: SharedPrincipal
) -> None:
    stub("unmap_index", repo.Refused("default_index_in_use"))

    response = await started.client.delete(
        f"/v1/admin/projects/{PROJECT}/indexes/{INDEX}", headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (409, "default_index_in_use")


@pytest.mark.parametrize(
    ("operation", "model", "example"),
    [
        ("adminCreateProject", ProjectCreate, CREATE_EXAMPLE),
        ("adminUpdateProject", ProjectUpdate, UPDATE_EXAMPLE),
        ("adminMapProjectIndex", ProjectIndexMappingUpdate, MAPPING_EXAMPLE),
    ],
)
def test_t043_each_request_example_validates_and_is_published(
    operation: str, model: type[RequestModel], example: Mapping[str, object]
) -> None:
    model.model_validate(example)
    published = {
        op["operationId"]: op
        for item in create_app().openapi()["paths"].values()
        for op in item.values()
    }[operation]
    assert published["requestBody"]["content"]["application/json"]["example"] == example
