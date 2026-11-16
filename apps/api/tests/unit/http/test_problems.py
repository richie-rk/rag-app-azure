from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Protocol
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from pydantic import ValidationError

import docchat_api
import docchat_core
from docchat_api.http.problems import (
    PROBLEM_MEDIA_TYPE,
    FieldError,
    Problem,
    RouteTable,
    ValidationFailed,
    field_issues,
    path_for,
    problem_body,
    reason_for,
)
from docchat_core.errors import CODES, REASONS, RETRY_AFTER_MAX, DocChatProblem, FieldIssue
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal


class Captured(Protocol):
    """The `captured` fixture of `tests/unit/conftest.py`."""

    collector: Any

    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def server_spans(self) -> list[ReadableSpan]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


CORRELATION = "0af7651916cd43dd8448eb211c80319c"
TURN_CREATION_CONFLICTS = {
    "turn_in_progress",
    "turn_already_ended",
    "client_turn_id_conflict",
    "session_turn_limit",
}


def _problem(code: str) -> DocChatProblem:
    """A problem of `code` with exactly the members its entry declares."""
    entry = CODES[code]
    cls: type[DocChatProblem] = type(f"Probe_{code}", (DocChatProblem,), {"code": code})
    kwargs: dict[str, Any] = {}
    if entry.retry_after is not None and entry.fixed_retry_after is None:
        kwargs["retry_after_seconds"] = entry.retry_after[1]
    if "errors" in entry.extensions:
        kwargs["errors"] = [FieldIssue("body.name", "too_long")]
    if "sessionId" in entry.extensions:
        kwargs["session_id"] = uuid4()
    if "turnId" in entry.extensions:
        kwargs["turn_id"] = uuid4()
    if entry.detail_fields:
        kwargs["detail_values"] = {"maxFilesPerBatch": 100}
    return cls(**kwargs)


PROBLEM_CODES = sorted(code for code, entry in CODES.items() if entry.status is not None)


@pytest.mark.parametrize("code", PROBLEM_CODES)
def test_t044_every_code_renders_exactly_its_members(code: str) -> None:
    problem = _problem(code)
    entry = CODES[code]

    body = problem_body(problem, CORRELATION).model_dump(mode="json", exclude_none=True)

    expected = {"type", "title", "status", "detail", "code", "correlationId"} | entry.extensions
    assert set(body) == expected
    assert body["type"] == f"urn:docchat:problem:{code}"
    assert body["title"] == entry.title
    assert body["status"] == entry.status
    assert body["code"] == code
    assert body["correlationId"] == CORRELATION
    if code != "batch_too_large":
        assert body["detail"] == entry.detail


def test_t044_session_and_turn_only_on_the_turn_creation_conflicts() -> None:
    with_session = {code for code in PROBLEM_CODES if "sessionId" in CODES[code].extensions}
    with_turn = {code for code in PROBLEM_CODES if "turnId" in CODES[code].extensions}
    conflicts_with_wait = {
        code
        for code in PROBLEM_CODES
        if CODES[code].status == 409 and "retryAfterSeconds" in CODES[code].extensions
    }

    assert with_session == TURN_CREATION_CONFLICTS
    assert with_turn == TURN_CREATION_CONFLICTS - {"session_turn_limit"}
    assert conflicts_with_wait == {"turn_in_progress"}


@pytest.mark.parametrize("seconds", [1, RETRY_AFTER_MAX])
def test_t044_retry_after_bounds_validate(seconds: int) -> None:
    problem = Problem.model_validate(
        {
            "type": "urn:docchat:problem:rate_limited",
            "title": "Request limit reached",
            "status": 429,
            "detail": "Wait.",
            "code": "rate_limited",
            "correlationId": CORRELATION,
            "retryAfterSeconds": seconds,
        }
    )

    assert problem.retry_after_seconds == seconds


@pytest.mark.parametrize("seconds", [0, RETRY_AFTER_MAX + 1])
def test_t044_retry_after_outside_its_bounds_is_refused(seconds: int) -> None:
    with pytest.raises(ValidationError):
        Problem.model_validate(
            {
                "type": "urn:docchat:problem:rate_limited",
                "title": "Request limit reached",
                "status": 429,
                "detail": "Wait.",
                "code": "rate_limited",
                "correlationId": CORRELATION,
                "retryAfterSeconds": seconds,
            }
        )


def test_t044_the_schema_states_the_optional_members_without_null() -> None:
    schema = Problem.model_json_schema(mode="serialization")
    properties = schema["properties"]

    assert properties["retryAfterSeconds"] == {
        "maximum": RETRY_AFTER_MAX,
        "minimum": 1,
        "title": "Retryafterseconds",
        "type": "integer",
    }
    assert properties["sessionId"]["type"] == "string"
    assert set(schema["required"]) == {"type", "title", "status", "detail", "code", "correlationId"}


def test_field_error_carries_no_value() -> None:
    assert set(FieldError.model_fields) == {"path", "reason"}


def _modules(package: Any) -> list[str]:
    walked = pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}.")
    return [package.__name__, *(info.name for info in walked)]


def _subclasses(cls: type[DocChatProblem]) -> set[type[DocChatProblem]]:
    found: set[type[DocChatProblem]] = set()
    for sub in cls.__subclasses__():
        found |= {sub} | _subclasses(sub)
    return found


def test_t045_every_raised_code_is_a_registered_problem_code() -> None:
    for package in (docchat_core, docchat_api):
        for name in _modules(package):
            # The ingestion parsing child and other optional extras import what this
            # environment has; every module that imports registers its problem classes.
            try:
                importlib.import_module(name)
            except ImportError:
                continue
    raised = {
        cls.__dict__["code"]
        for cls in _subclasses(DocChatProblem)
        if "code" in cls.__dict__ and cls.__module__.startswith(("docchat_api", "docchat_core"))
    }

    assert raised
    for code in raised:
        assert CODES[code].status is not None, code


def test_t045_a_rate_limited_body_is_titled_request_limit_reached() -> None:
    assert CODES["rate_limited"].title == "Request limit reached"


def test_t045_a_422_needs_an_errors_entry() -> None:
    with pytest.raises(ValueError, match="at least one field error"):
        ValidationFailed()


def test_t045_a_stream_only_code_is_never_a_problem() -> None:
    with pytest.raises(TypeError, match="never a problem"):
        type("Stream", (DocChatProblem,), {"code": "turn_timeout"})


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ({"type": "missing"}, "required"),
        ({"type": "extra_forbidden"}, "unknown_field"),
        ({"type": "json_invalid"}, "invalid_json"),
        ({"type": "json_type"}, "invalid_json"),
        ({"type": "uuid_parsing"}, "invalid_uuid"),
        ({"type": "uuid_type"}, "invalid_uuid"),
        (
            {
                "type": "string_pattern_mismatch",
                "ctx": {
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                },
            },
            "invalid_uuid",
        ),
        ({"type": "string_pattern_mismatch", "ctx": {"pattern": "^[a-z]+$"}}, "invalid_format"),
        ({"type": "string_too_short"}, "too_short"),
        ({"type": "string_too_long"}, "too_long"),
        ({"type": "too_short"}, "too_short"),
        ({"type": "too_long"}, "too_many_items"),
        ({"type": "greater_than"}, "out_of_range"),
        ({"type": "greater_than_equal"}, "out_of_range"),
        ({"type": "less_than"}, "out_of_range"),
        ({"type": "less_than_equal"}, "out_of_range"),
        ({"type": "literal_error"}, "not_allowed"),
        ({"type": "enum"}, "not_allowed"),
        ({"type": "int_type"}, "invalid_type"),
        ({"type": "model_attributes_type"}, "invalid_type"),
        ({"type": "invalid_characters"}, "invalid_characters"),
        ({"type": "duplicate"}, "duplicate"),
        ({"type": "invalid_cursor"}, "invalid_cursor"),
        ({"type": "empty"}, "empty"),
        ({"type": "value_error"}, "invalid_format"),
        ({"type": "int_parsing"}, "invalid_format"),
    ],
)
def test_t050_each_error_type_maps_to_its_reason(error: dict[str, Any], reason: str) -> None:
    assert reason_for(error) == reason
    assert reason in REASONS


@pytest.mark.parametrize(
    ("location", "reason", "path"),
    [
        (("body", "exampleQuestions", 2), "too_long", "body.exampleQuestions.2"),
        (("query", "limit"), "out_of_range", "query.limit"),
        (("path", "projectId"), "invalid_uuid", "path.projectId"),
        (("body", 17), "invalid_json", "body"),
        (("body",), "required", "body"),
        ((), "invalid_format", "body"),
        (("name",), "too_long", "body.name"),
    ],
)
def test_field_paths(location: tuple[object, ...], reason: str, path: str) -> None:
    assert path_for(location, reason) == path


def test_a_path_is_cut_to_256_characters() -> None:
    assert len(path_for(("body", "x" * 300), "too_long")) == 256


def test_field_issues_keep_the_validators_order() -> None:
    issues = field_issues(
        [
            {"type": "missing", "loc": ("body", "name"), "input": "secret"},
            {"type": "extra_forbidden", "loc": ("query", "x"), "input": "secret"},
        ]
    )

    assert issues == [FieldIssue("body.name", "required"), FieldIssue("query.x", "unknown_field")]


def test_the_route_table_names_every_method_of_a_path() -> None:
    table = RouteTable(
        [("/v1/items", {"GET"}), ("/v1/items", {"POST"}), ("/v1/items/{id}", {"GET"})]
    )

    assert table.allowed("/v1/items") == ["GET", "POST"]
    assert table.allowed("/v1/items/7") == ["GET"]


# --- Through the app -----------------------------------------------------------------------


async def test_a_problem_answers_with_its_media_type(started: ApiUnderTest) -> None:
    response = await started.client.get("/v1/me")

    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == 'Bearer realm="docchat"'


async def test_invalid_token_carries_its_www_authenticate(started: ApiUnderTest) -> None:
    response = await started.client.get("/v1/me", headers={"Authorization": "Bearer a.b.c"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="docchat", error="invalid_token"'


async def test_unknown_path_and_method(started: ApiUnderTest) -> None:
    missing = await started.client.get("/v1/nothing")
    trailing = await started.client.get("/v1/me/")
    wrong = await started.client.delete("/v1/projects")
    head = await started.client.head("/v1/me")

    assert (missing.status_code, missing.json()["code"]) == (404, "route_not_found")
    assert (trailing.status_code, trailing.json()["code"]) == (404, "route_not_found")
    assert (wrong.status_code, wrong.json()["code"]) == (405, "method_not_allowed")
    assert wrong.headers["allow"] == "GET"
    assert (head.status_code, head.headers["allow"]) == (405, "GET")


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_no_document_or_documentation_route(started: ApiUnderTest, path: str) -> None:
    response = await started.client.get(path)

    assert (response.status_code, response.json()["code"]) == (404, "route_not_found")


async def test_the_problem_counter_and_the_span_carry_the_code(
    started: ApiUnderTest, captured: Captured
) -> None:
    await started.client.get("/v1/me")

    assert captured.points("docchat.api.problems") == {
        (("code", "authentication_required"), ("operation", "getCurrentUser"), ("status", 401)): 1
    }
    (server,) = captured.server_spans()
    assert server.attributes is not None
    assert server.attributes["docchat.error_code"] == "authentication_required"


async def test_a_404_of_an_operation_is_recorded_as_a_denial(
    started: ApiUnderTest, captured: Captured, carol: SharedPrincipal
) -> None:
    response = await started.client.post(
        "/v1/admin/probe",
        json={"name": "n", "projectId": str(uuid4())},
        headers=carol.authorization(),
    )

    assert response.json()["code"] == "project_not_found"
    assert captured.points("docchat.access.denials") == {
        (("kind", "not_found"), ("operation", "probeAdmin")): 1
    }


async def test_sql_unavailable_answers_dependency_unavailable(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.get("/v1/probe/sql", headers=alice.authorization())

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"
    assert response.json()["retryAfterSeconds"] == 5
    assert response.headers["retry-after"] == "5"


async def test_t047_an_unmapped_exception_is_one_record_and_a_500(
    started: ApiUnderTest, captured: Captured, alice: SharedPrincipal, sentinel: str
) -> None:
    response = await started.client.get("/v1/probe/boom", headers=alice.authorization())

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert sentinel not in response.text
    (record,) = captured.records("request_failed")
    assert record.__dict__["error.type"] == "builtins.RuntimeError"
    assert record.__dict__["docchat.error_code"] == "internal_error"
    for span in captured.finished_spans():
        texts = [str(value) for value in (span.attributes or {}).values()]
        texts += [
            str(value) for event in span.events for value in (event.attributes or {}).values()
        ]
        assert not any(sentinel in text for text in texts), span.name
    assert not any(
        sentinel in str(value)
        for record in captured.collector.records
        for value in [record.getMessage(), *record.__dict__.values()]
    )


async def test_t047_a_sentinel_in_every_field_never_reaches_a_problem_body(
    started: ApiUnderTest, alice: SharedPrincipal, sentinel: str
) -> None:
    responses = [
        await started.client.get(
            "/v1/projects", params={"limit": sentinel, "cursor": sentinel, "unknown": sentinel}
        ),
        await started.client.get(
            "/v1/projects",
            params={"limit": sentinel, "cursor": sentinel, "unknown": sentinel},
            headers=alice.authorization(),
        ),
        await started.client.get("/v1/me", headers={"Authorization": f"Bearer {sentinel}"}),
        await started.client.post(
            f"/v1/probe/items/{sentinel}",
            params={"projectId": sentinel},
            json={"name": sentinel * 3, "projectId": sentinel, "unknown": sentinel},
            headers=alice.authorization(),
        ),
        await started.client.post(
            "/v1/probe/items/{sentinel}",
            content=sentinel.encode(),
            headers=alice.authorization() | {"Content-Type": f"text/{sentinel}"},
        ),
    ]

    assert [response.status_code for response in responses] == [401, 422, 401, 422, 415]
    for response in responses:
        assert sentinel not in response.text


async def test_an_http_exception_other_than_404_and_405_is_a_defect(
    started: ApiUnderTest, captured: Captured, alice: SharedPrincipal
) -> None:
    response = await started.client.get("/v1/probe/teapot", headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    (record,) = captured.records("request_failed")
    assert record.__dict__["error.type"] == "starlette.exceptions.HTTPException"
