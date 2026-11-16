from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from docchat_api.app import create_app
from docchat_api.routes.admin_uploads import UploadStorage, upload_storage
from docchat_core.cosmos.containers import CONTAINERS
from docchat_core.ingestion.models import IndexStatus, SubmitOutcome
from docchat_core.ingestion.runs import SubmitResult, UploadRunResult
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.uploads import Reservation, SubmittedUploads, UploadGate
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_testing.authz_matrix import (
    ID_KINDS,
    IF_MATCH_KINDS,
    Caller,
    IdRef,
    MatrixCase,
    MatrixRequest,
    UnresolvedExampleError,
    World,
    build_request,
    case_ids,
    check_case,
    fill_schema,
    generate,
    send,
    without_correlation,
)
from docchat_testing.fakes.chat import ChatFakes
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import BuiltApp, build_app, ports_under_test, serving
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal
from docchat_testing.tokens import SigningKeys

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy

PROJECT_A = UUID("0a0a0a0a-0000-4000-8000-00000000000a")
INDEX_X = UUID("0c0c0c0c-0000-4000-8000-00000000000c")
INDEX_Y = UUID("0d0d0d0d-0000-4000-8000-00000000000d")
VERSION_X = UUID("0e0e0e0e-0000-4000-8000-00000000000e")
VERSION_Y = UUID("0f0f0f0f-0000-4000-8000-00000000000f")
PHYSICAL_X = UUID("0c0c0c0c-0000-4000-8000-0000000000c1")
WORLD = World(
    reachable={
        "project": str(PROJECT_A),
        "user": "0a11ce00-0000-4000-8000-000000000001",
        "index": str(INDEX_X),
        "version": str(VERSION_X),
        "document": "0e0d0c00-0000-4000-8000-00000000000e",
        "run": "0e0e0000-0000-4000-8000-00000000000e",
        "session": "0e5e5500-0000-4000-8000-00000000000e",
        "turn": "0e7a7a00-0000-4000-8000-00000000000e",
        "citation": "1",
        "fact": "0efac700-0000-4000-8000-00000000000e",
    },
    unreachable={
        "project": "0b0b0b0b-0000-4000-8000-00000000000b",
        "user": "0b0b0000-0000-4000-8000-000000000002",
        "index": str(INDEX_Y),
        "version": str(VERSION_Y),
        "document": "0f0d0c00-0000-4000-8000-00000000000f",
        "run": "0f0e0000-0000-4000-8000-00000000000f",
        "session": "0f5e5500-0000-4000-8000-00000000000f",
        "turn": "0f7a7a00-0000-4000-8000-00000000000f",
        "citation": "2",
        "fact": "0ffac700-0000-4000-8000-00000000000f",
    },
    versions={
        "project": '"AAAAAAAAB9E="',
        "index": '"AAAAAAAAB9I="',
        "document": '"AAAAAAAAB9M="',
        "memory": '"00000000-0000-0000-0000-000000000000"',
    },
    absent={"citation": "16"},
)


def _operation(access: str | None, **extra: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {"operationId": "thing", "responses": {}}
    if access is not None:
        operation |= {
            "x-docchat-access": access,
            "x-docchat-scoped-params": [],
            "x-docchat-error-codes": [],
        }
    return operation | extra


def _document(path: str, method: str, operation: Mapping[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.1.0", "paths": {path: {method: dict(operation)}}, "components": {}}


# --- T-565: the generator's meta-test ----------------------------------------------------------


def test_t565_a_route_without_its_access_extension_fails() -> None:
    cases = generate(_document("/v1/things", "get", _operation(None)))

    assert [case.failure for case in cases] == ["missing_access_metadata", "too_few_cases"]


def test_t565_an_operation_with_fewer_than_three_cases_fails() -> None:
    cases = generate(_document("/v1/things", "get", _operation("superuser")))

    assert [case.failure for case in cases] == ["too_few_cases"]


def test_t565_a_scoped_name_outside_the_map_fails() -> None:
    operation = _operation("user")
    operation["x-docchat-scoped-params"] = ["widgetId"]
    cases = generate(_document("/v1/things", "get", operation))

    assert "unresolved_example" in [case.failure for case in cases]


def _differing_not_found_app() -> Starlette:
    async def thing(request: Request) -> Response:
        # A leak: the body tells an unreachable identifier from a random one.
        return JSONResponse(
            {"code": "project_not_found", "status": 404, "id": request.path_params["projectId"]},
            status_code=404,
        )

    return Starlette(routes=[Route("/v1/projects/{projectId}", thing)])


async def test_t565_differing_not_found_bodies_fail() -> None:
    operation = _operation("user")
    operation["x-docchat-scoped-params"] = ["projectId"]
    operation["x-docchat-error-codes"] = ["project_not_found"]
    cases = generate(_document("/v1/projects/{projectId}", "get", operation))
    (scoped,) = [case for case in cases if case.check == "not_found_projectId"]
    transport = httpx.ASGITransport(app=_differing_not_found_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(AssertionError, match="different bodies"):
            await check_case(scoped, client, WORLD, lambda caller: "token")


async def test_a_failing_case_fails_when_it_runs() -> None:
    case = MatrixCase("thing", "missing_access_metadata", failure="missing_access_metadata")

    async with httpx.AsyncClient(base_url="http://test") as client:
        with pytest.raises(AssertionError, match="missing_access_metadata"):
            await check_case(case, client, WORLD, lambda caller: "token")


# --- Generation -----------------------------------------------------------------------------


def test_the_public_operation_must_be_get_health() -> None:
    cases = generate(_document("/v1/open", "get", _operation("public")))

    assert [case.failure for case in cases] == ["unexpected_public_operation", "too_few_cases"]


def test_a_user_operation_gets_the_401_403_and_positive_checks() -> None:
    cases = generate(_document("/v1/things", "get", _operation("user")))

    assert [case.check for case in cases] == [
        "401_without_authorization",
        "401_with_bearer_not_a_token",
        "401_with_33_kib_body",
        "403_without_role",
        "2xx_right_caller",
    ]
    assert cases[3].caller == "erin"
    assert cases[-1].caller == "alice"


def test_an_admin_operation_gets_the_admin_required_checks_and_carol() -> None:
    operation = _operation("admin")
    operation["x-docchat-scoped-params"] = ["indexId", "documentId"]
    operation["x-docchat-error-codes"] = ["index_not_found", "document_not_found"]
    cases = {
        case.check: case
        for case in generate(
            _document("/v1/admin/indexes/{indexId}/documents/{documentId}", "get", operation)
        )
    }

    for check in ("403_user_real_ids", "403_user_random_ids", "403_user_invalid_body"):
        assert cases[check].caller == "alice"
        assert cases[check].expect.code == "admin_required"
        assert {404, 412, 422, 428} <= cases[check].expect.never
    random_ids = cases["403_user_random_ids"].request
    assert random_ids is not None
    assert random_ids.path_params == {
        "indexId": IdRef("index", "random"),
        "documentId": IdRef("document", "random"),
    }
    # The first path parameter has only the random identifier; a nested one compares both.
    assert cases["not_found_indexId"].compare_with is None
    assert cases["not_found_documentId"].compare_with is not None
    assert cases["2xx_right_caller"].caller == "carol"


def test_only_the_submit_version_ids_are_compared_with_masked_identifiers() -> None:
    masked = [case.id for case in CASES if case.mask_identifiers]

    assert masked == ["adminSubmitUploads-not_found_body:/versionIds"]


def test_the_three_body_pointers_answer_otherwise() -> None:
    operation = _operation("admin", operationId="adminCreateProject")
    operation["x-docchat-scoped-params"] = ["body:/initialMemberIds"]
    cases = {
        case.check: case for case in generate(_document("/v1/admin/projects", "post", operation))
    }

    expect = cases["not_found_body:/initialMemberIds"].expect
    assert (expect.status, expect.code) == (422, "validation_failed")


def test_build_request_resolves_path_identifiers_and_fills_bodies() -> None:
    document = {
        "components": {
            "schemas": {
                "Body": {
                    "type": "object",
                    "required": ["name", "count", "on", "projectId", "tags"],
                    "properties": {
                        "name": {"type": "string", "minLength": 3},
                        "count": {"type": "integer", "minimum": 2},
                        "on": {"type": "boolean"},
                        "projectId": {"type": "string", "format": "uuid"},
                        "tags": {"type": "array", "minItems": 2, "items": {"enum": ["a", "b"]}},
                        "optional": {"type": "string"},
                    },
                }
            }
        }
    }
    operation = {
        "requestBody": {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}
        }
    }

    request = build_request("post", "/v1/projects/{projectId}/things", operation, document)

    assert request.path_params == {"projectId": IdRef("project")}
    assert request.body == {
        "name": "xxx",
        "count": 2,
        "on": False,
        "projectId": IdRef("project"),
        "tags": ["a", "a"],
    }


def test_an_example_body_wins_and_its_identifiers_resolve() -> None:
    operation = {
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                    "example": {"projectId": "ignored", "initialMemberIds": ["a", "b"], "n": 1},
                }
            }
        }
    }

    request = build_request("post", "/v1/admin/projects", operation, {})

    assert request.body == {
        "projectId": IdRef("project"),
        "initialMemberIds": [IdRef("user"), IdRef("user")],
        "n": 1,
    }


def test_fill_schema_follows_combinators_and_consts() -> None:
    assert fill_schema({"anyOf": [{"type": "null"}, {"const": 7}]}, {}) == 7
    assert fill_schema({"type": "number"}, {}) == 0
    assert fill_schema({"type": "string"}, {}, "versionIds") == IdRef("version")


def test_an_unmapped_path_parameter_is_unresolved() -> None:
    cases = generate(_document("/v1/widgets/{widgetId}", "get", _operation("user")))

    assert cases[0].failure == "unresolved_example"


def test_a_null_optional_identifier_gets_one_in_its_scoped_case() -> None:
    operation = _operation("user")
    operation["x-docchat-scoped-params"] = ["body:/sessionId"]
    operation["x-docchat-error-codes"] = ["session_not_found"]
    operation["requestBody"] = {
        "content": {
            "application/json": {"example": {"sessionId": None, "projectId": PROJECT_A.hex}}
        }
    }
    cases = generate(_document("/v1/chat/turns", "post", operation))
    (scoped,) = [case for case in cases if case.check == "not_found_body:/sessionId"]
    positive = next(case for case in cases if case.check == "2xx_right_caller")

    assert scoped.request is not None
    assert scoped.request.body["sessionId"] == IdRef("session", "unreachable")
    assert scoped.compare_with is not None
    assert scoped.compare_with.body["sessionId"] == IdRef("session", "random")
    assert positive.request is not None
    assert positive.request.body["sessionId"] is None


def test_switching_and_resolving_identifiers() -> None:
    request = MatrixRequest("GET", "/x", path_params={"projectId": IdRef("project")})

    assert request.with_ids("unreachable").path_params == {
        "projectId": IdRef("project", "unreachable")
    }
    assert WORLD.resolve(IdRef("project", "unreachable")) == WORLD.unreachable["project"]
    assert WORLD.resolve(IdRef("project", "random")) not in WORLD.reachable.values()


def test_without_correlation_drops_only_the_correlation_id() -> None:
    first = httpx.Response(404, json={"code": "x", "correlationId": "a" * 32})
    second = httpx.Response(404, json={"correlationId": "b" * 32, "code": "x"})

    assert without_correlation(first) == without_correlation(second)


def test_without_correlation_masks_only_the_requests_own_identifiers() -> None:
    first_id, second_id = str(uuid4()), str(uuid4())
    first = httpx.Response(200, json={"results": [{"versionId": first_id, "outcome": "x"}]})
    second = httpx.Response(200, json={"results": [{"versionId": second_id, "outcome": "x"}]})

    assert without_correlation(first, [first_id.upper()]) == without_correlation(
        second, [second_id]
    )
    assert without_correlation(first, [first_id]) != without_correlation(second, [first_id])


def test_the_map_names_each_kind_of_section_3_2() -> None:
    assert set(ID_KINDS.values()) == {
        "project",
        "index",
        "document",
        "version",
        "run",
        "session",
        "turn",
        "citation",
        "fact",
        "user",
    }


# --- If-Match, shared kinds and empty arrays ---------------------------------------------------


def _if_match_operation(operation_id: str) -> dict[str, Any]:
    operation = _operation("admin", operationId=operation_id)
    operation["parameters"] = [{"name": "If-Match", "in": "header", "required": True}]
    return operation


def test_an_if_match_operation_names_the_version_of_its_resource() -> None:
    request = build_request(
        "delete",
        "/v1/admin/projects/{projectId}/members/{userId}",
        _if_match_operation("adminRemoveProjectMember"),
        {},
    )

    assert request.if_match == "project"
    assert set(IF_MATCH_KINDS.values()) == {"project", "index", "document", "memory"}
    assert len(IF_MATCH_KINDS) == 14


def test_an_if_match_operation_outside_the_table_is_unresolved() -> None:
    cases = generate(_document("/v1/admin/things", "patch", _if_match_operation("adminPatch")))

    assert cases[0].failure == "unresolved_example"


async def test_the_if_match_header_carries_the_worlds_version() -> None:
    seen: list[str | None] = []

    async def echo(request: Request) -> Response:
        seen.append(request.headers.get("if-match"))
        return JSONResponse({})

    transport = httpx.ASGITransport(app=Starlette(routes=[Route("/x", echo, methods=["PUT"])]))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await send(client, MatrixRequest("PUT", "/x", if_match="project"), WORLD)
        await send(client, MatrixRequest("PUT", "/x"), WORLD)

    assert seen == [WORLD.versions["project"], None]
    with pytest.raises(UnresolvedExampleError):
        WORLD.version("session")


def test_a_person_or_index_under_a_project_gets_only_the_random_identifier() -> None:
    operation = _operation("admin", operationId="adminAddProjectMember")
    operation["x-docchat-scoped-params"] = ["projectId", "userId"]
    operation["x-docchat-error-codes"] = ["project_not_found", "user_not_found"]
    path = "/v1/admin/projects/{projectId}/members/{userId}"
    cases = {case.check: case for case in generate(_document(path, "put", operation))}

    scoped = cases["not_found_userId"]
    assert scoped.compare_with is None
    assert scoped.request is not None
    assert scoped.request.path_params["userId"] == IdRef("user", "random")
    assert (scoped.expect.status, scoped.expect.code) == (404, "user_not_found")


def test_an_empty_example_array_gets_one_identifier() -> None:
    operation = _operation("admin", operationId="adminCreateProject")
    operation["x-docchat-scoped-params"] = ["body:/initialMemberIds"]
    operation["requestBody"] = {
        "content": {
            "application/json": {
                "schema": {"type": "object"},
                "example": {"name": "Field notes", "initialMemberIds": []},
            }
        }
    }
    cases = {
        case.check: case for case in generate(_document("/v1/admin/projects", "post", operation))
    }

    scoped = cases["not_found_body:/initialMemberIds"]
    assert scoped.request is not None
    assert scoped.request.body == {
        "name": "Field notes",
        "initialMemberIds": [IdRef("user", "random")],
    }
    assert scoped.compare_with is None
    positive = cases["2xx_right_caller"].request
    assert positive is not None
    assert positive.body["initialMemberIds"] == []


def test_an_operation_with_one_not_found_code_answers_every_scoped_parameter_with_it() -> None:
    operation = _operation("user")
    operation["x-docchat-scoped-params"] = ["sessionId", "turnId", "citationNumber"]
    operation["x-docchat-error-codes"] = ["citation_not_found", "document_changed"]
    path = "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}"
    cases = {case.check: case for case in generate(_document(path, "get", operation))}

    for name in ("sessionId", "turnId", "citationNumber"):
        scoped = cases[f"not_found_{name}"]
        assert scoped.failure is None
        assert (scoped.expect.status, scoped.expect.code) == (404, "citation_not_found")
        assert scoped.compare_with is not None
        assert scoped.compare_with.path_params[name] == IdRef(ID_KINDS[name], "random")


def test_two_not_found_codes_leave_a_foreign_kind_unresolved() -> None:
    operation = _operation("user")
    operation["x-docchat-scoped-params"] = ["sessionId"]
    operation["x-docchat-error-codes"] = ["citation_not_found", "project_not_found"]
    cases = generate(_document("/v1/sessions/{sessionId}", "get", operation))

    assert "unresolved_example" in [case.failure for case in cases]


def test_a_kind_without_uuid_identifiers_resolves_random_to_its_absent_value() -> None:
    assert WORLD.resolve(IdRef("citation", "random")) == "16"
    assert WORLD.resolve(IdRef("citation", "unreachable")) == "2"
    assert WORLD.resolve(IdRef("session", "random")) not in WORLD.reachable.values()


# --- The stage's matrix in process, over a data-access layer without SQL ------------------------


def _seeded_rows() -> dict[str, str]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    raise FileNotFoundError("80-data-model.md")


_VERSION_INDEX = {VERSION_X: INDEX_X, VERSION_Y: INDEX_Y}
"""Each world version and the index it belongs to; both are `pending`."""


def _pending_version(version_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=version_id,
        document_id=uuid4(),
        search_index_id=_VERSION_INDEX[version_id],
        version_number=1,
        state="pending",
        size_bytes=1000,
        sha256=bytes(32),
        content_type="application/pdf",
        scan_status="not_required",
        failure_code=None,
        uploaded_by=uuid4(),
        uploaded_by_name=None,
        uploaded_via_project_id=PROJECT_A,
        project_name="A",
        reserved_at=datetime(2026, 10, 31),  # noqa: DTZ001 - a DATETIME2 value as read
        committed_at=datetime(2026, 10, 31),  # noqa: DTZ001 - a DATETIME2 value as read
        ingested_at=None,
        document_pdf_status=None,
    )


def _submitted(index_id: UUID, version_ids: list[UUID]) -> SubmittedUploads:
    results = tuple(
        SubmitResult(
            version_id,
            SubmitOutcome.QUEUED
            if _VERSION_INDEX.get(version_id) == index_id
            else SubmitOutcome.NOT_FOUND,
        )
        for version_id in version_ids
    )
    queued = any(result.outcome is SubmitOutcome.QUEUED for result in results)
    return SubmittedUploads(UploadRunResult(uuid4() if queued else None, results))


class _Rows:
    """The statements the stage's routes make, with no project rows, and the upload statements
    answered from the world: indexes X and Y exist, project A maps X, and each index holds one
    `pending` version."""

    def __init__(self) -> None:
        self.rows = _seeded_rows()

    async def fetch_all(self, statement: object, parameters: object = None, **kwargs: Any) -> Any:
        return list(self.rows.items()) if kwargs["name"] == "read_ops_config" else []

    async def fetch_one(self, statement: object, parameters: Any = None, **kwargs: Any) -> Any:
        chat = _chat_row(kwargs["name"], parameters)
        if chat is not _NO_ROW:
            return chat
        if kwargs["name"] == "project_mapped":
            mapped = (parameters["project_id"], parameters["index_id"]) == (PROJECT_A, INDEX_X)
            return SimpleNamespace() if mapped else None
        if kwargs["name"] == "upload_version":
            version_id = parameters["version_id"]
            found = _VERSION_INDEX.get(version_id) == parameters["index_id"]
            return _pending_version(version_id) if found else None
        raise AssertionError(kwargs["name"])

    async def run(self, work: Callable[[Any], Any], **kwargs: Any) -> Any:
        keywords = getattr(work, "keywords", {})
        match kwargs["name"]:
            case "upload_gate":
                known = keywords["index_id"] in {INDEX_X, INDEX_Y}
                return UploadGate(IndexStatus.READY, scanner_ready=True) if known else None
            case "reserve_upload":
                return Reservation(uuid4(), uuid4(), 1, reused=False)
            case "submit_uploads":
                return _submitted(keywords["index_id"], list(keywords["version_ids"]))
            case _:
                return ProvisionResult(provisioned=False, default_project_count=0)


_NO_ROW: Any = object()


def _chat_row(name: str, parameters: Any) -> Any:
    """The chat path's access rows: project A, active, its default index X with an active
    physical index; any other project has no row."""
    match name:
        case "authorize_project":
            if parameters["project_id"] != PROJECT_A:
                return None
            return SimpleNamespace(
                id=PROJECT_A,
                name="A",
                description="",
                instructions="",
                example_questions_json="[]",
                status="active",
                is_member=1,
            )
        case "resolve_index":
            return SimpleNamespace(
                override_index_id=None, override_expires_at=None, default_index_id=INDEX_X
            )
        case "read_index_record":
            return SimpleNamespace(
                id=INDEX_X,
                status="ready",
                physical_index_id=PHYSICAL_X,
                name="docchat-0c0c0c0c00004000800000000000000c-g1",
                schema_version=1,
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1024,
                query_embedding_deployment="embed-query-te3-large-v1",
            )
        case _:
            return _NO_ROW


class _Queue:
    async def send_message(self, content: str, *, time_to_live: int) -> object:
        return {}


def _containers() -> Callable[[ContainerProxy], ContainerProxy]:
    """In-memory `usage`, `conversations` and `memories` containers in place of the lifespan's."""
    fakes = {definition.name: FakeContainer.of(definition) for definition in CONTAINERS}

    def replace(container: ContainerProxy) -> ContainerProxy:
        return cast("ContainerProxy", fakes[container.id])

    return replace


DOCUMENT = create_app().openapi()
CASES = generate(DOCUMENT)
FULL_OPERATIONS: Final = frozenset(
    {
        "getHealth",
        "getCurrentUser",
        "listMyProjects",
        "adminPreflightUploads",
        "adminReserveUpload",
        "adminUploadContent",
        "adminSubmitUploads",
        "createChatTurn",
    }
)
"""The operations this data-access layer answers in full; every other operation runs only its
refusals that come before any store (`401`, `403`), which need no seeded rows."""
IN_PROCESS: Final = [
    case
    for case in CASES
    if case.operation_id in FULL_OPERATIONS or case.check.startswith(("401", "403"))
]


@pytest.fixture
def built(signing_keys: SigningKeys) -> BuiltApp:
    """The app over the scripted data-access layer, in-memory Cosmos DB containers and the chat
    pipeline's doubles, built outside the test's event loop."""
    built = build_app(
        ports=ports_under_test(
            sql_engine=cast("SqlEngine", _Rows()),
            container_wrapper=_containers(),
            chat=ChatFakes().ports(),
        ),
        signing_keys=signing_keys,
    )
    built.app.dependency_overrides[upload_storage] = lambda: UploadStorage(
        documents=cast("Any", None), queue=_Queue()
    )
    return built


@pytest.fixture
async def client(built: BuiltApp) -> AsyncIterator[httpx.AsyncClient]:
    async with serving(built) as api:
        yield api.client


@pytest.mark.parametrize("case", IN_PROCESS, ids=case_ids(IN_PROCESS))
async def test_the_stage_matrix_passes_in_process(
    case: MatrixCase,
    client: httpx.AsyncClient,
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    def token_for(caller: Caller) -> str:
        return principals[cast("PrincipalName", caller)].token()

    await check_case(case, client, WORLD, token_for)


def test_the_stage_matrix_covers_every_operation() -> None:
    operations = {case.operation_id for case in IN_PROCESS}

    assert operations >= FULL_OPERATIONS
    assert operations == {case.operation_id for case in CASES}
    assert not [case.id for case in CASES if case.failure]
