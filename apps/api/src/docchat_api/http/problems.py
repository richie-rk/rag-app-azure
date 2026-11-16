"""Problem details: the `Problem` and `FieldError` models, the exception handlers and the failure
boundary of every request (`11-api.md` sections 1.3 step 6, 1.5, 1.7 and 2; `82-security.md`
SEC-31; ADR 0012).

Every non-`2xx` answer the app produces is an RFC 9457 problem with `application/problem+json`,
built only from a `DocChatProblem` and its `CODES` entry: one title and one `detail` per code, the
optional members `retryAfterSeconds`, `errors`, `sessionId` and `turnId` omitted when they don't
apply, `Retry-After` beside `retryAfterSeconds`. A body never holds exception text, input values
or names.

- `handle_problem` answers every `DocChatProblem` (and the authentication refusals with their
  `WWW-Authenticate`); `handle_validation_error` turns `RequestValidationError` into
  `422 validation_failed` with one `FieldError` per failed field; `handle_http_exception` answers
  the router's unknown path (`404 route_not_found`) and unknown method (`405 method_not_allowed`
  with `Allow` naming every method of the path).
- `UnhandledErrorMiddleware` is the boundary for everything else: `500 internal_error`, logged
  once as `request_failed` with the exception's qualified type name and never its message.
  Starlette's own server-error middleware would answer too, but it re-raises, so the server would
  log the exception's text a second time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from docchat_api.http.context import correlation_id, operation_of, server_span_of
from docchat_api.http.telemetry import NO_OPERATION, record_problem, report_request_failed
from docchat_core.access import record_denial
from docchat_core.auth import AuthenticationFailure
from docchat_core.errors import (
    CODE_MAX_LENGTH,
    CODE_PATTERN,
    DETAIL_MAX_LENGTH,
    FIELD_PATH_MAX_LENGTH,
    MAX_FIELD_ERRORS,
    PROBLEM_TYPE_PREFIX,
    REASONS,
    RETRY_AFTER_MAX,
    TITLE_MAX_LENGTH,
    DocChatProblem,
    FieldIssue,
)
from docchat_core.models.identifiers import (
    CORRELATION_ID_PATTERN,
    LOWER_CASE_UUID_PATTERN,
    UUID_PATTERN,
)

PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
ROUTE_TABLE_STATE: Final = "docchat_route_table"
"""The `app.state` attribute caching every route's path and methods, for the `Allow` header."""

HTTP_NOT_FOUND: Final = 404
HTTP_METHOD_NOT_ALLOWED: Final = 405


# --- The models -------------------------------------------------------------------------------


class FieldError(BaseModel):
    """One failed field of a `422`: where, and why; never the input value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Annotated[str, Field(min_length=1, max_length=FIELD_PATH_MAX_LENGTH)]
    reason: Annotated[str, Field(min_length=1, max_length=CODE_MAX_LENGTH, pattern=CODE_PATTERN)]


def _absent_when_empty(schema: dict[str, Any]) -> None:
    # An optional member is left out rather than sent as null, so the schema states no default.
    schema.pop("default", None)


class Problem(BaseModel):
    """The one body of every non-`2xx` answer. The optional members are absent, never `null`,
    when they don't apply, which the schema states by leaving `null` out of their types."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, serialize_by_alias=True, validate_by_name=True
    )

    type: Annotated[str, Field(pattern=rf"^{PROBLEM_TYPE_PREFIX}[a-z][a-z0-9_]*$")]
    title: Annotated[str, Field(min_length=1, max_length=TITLE_MAX_LENGTH)]
    status: Annotated[int, Field(ge=400, le=599)]
    detail: Annotated[str, Field(min_length=1, max_length=DETAIL_MAX_LENGTH)]
    code: Annotated[str, Field(min_length=1, max_length=CODE_MAX_LENGTH, pattern=CODE_PATTERN)]
    correlation_id: Annotated[str, Field(alias="correlationId", pattern=CORRELATION_ID_PATTERN)]
    retry_after_seconds: Annotated[int, Field(ge=1, le=RETRY_AFTER_MAX)] | SkipJsonSchema[None] = (
        Field(default=None, alias="retryAfterSeconds", json_schema_extra=_absent_when_empty)
    )
    errors: (
        Annotated[list[FieldError], Field(min_length=1, max_length=MAX_FIELD_ERRORS)]
        | SkipJsonSchema[None]
    ) = Field(default=None, json_schema_extra=_absent_when_empty)
    session_id: UUID | SkipJsonSchema[None] = Field(
        default=None, alias="sessionId", json_schema_extra=_absent_when_empty
    )
    turn_id: UUID | SkipJsonSchema[None] = Field(
        default=None, alias="turnId", json_schema_extra=_absent_when_empty
    )


# --- The problem classes the platform raises -------------------------------------------------


class ValidationFailed(DocChatProblem):
    """`422 validation_failed`, with at least one `FieldError`."""

    code = "validation_failed"


class DependencyUnavailable(DocChatProblem):
    """`503 dependency_unavailable` (`retryAfterSeconds` 5): SQL or Cosmos DB unreachable,
    timed out or throttled after the data layer's or SDK's retries."""

    code = "dependency_unavailable"


class RouteNotFound(DocChatProblem):
    """`404 route_not_found`: no route has this path (a trailing slash included)."""

    code = "route_not_found"


class MethodNotAllowed(DocChatProblem):
    """`405 method_not_allowed`: the path exists with other methods."""

    code = "method_not_allowed"


class InternalError(DocChatProblem):
    """`500 internal_error`: an exception no handler maps."""

    code = "internal_error"


# --- Validation errors to field errors ------------------------------------------------------

_TYPE_REASONS: Final[Mapping[str, str]] = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "json_invalid": "invalid_json",
    "json_type": "invalid_json",
    "uuid_parsing": "invalid_uuid",
    "uuid_type": "invalid_uuid",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "too_short": "too_short",
    "too_long": "too_many_items",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "literal_error": "not_allowed",
    "enum": "not_allowed",
}
"""Pydantic error types and their reasons (`11-api.md` section 1.7)."""

_IDENTIFIER_PATTERNS: Final = frozenset({UUID_PATTERN, LOWER_CASE_UUID_PATTERN})
_LOCATIONS: Final = frozenset({"body", "query", "path", "header"})


def reason_for(error: Mapping[str, Any]) -> str:
    """The `errors[].reason` of one Pydantic error: the table of section 1.7; a pattern failure
    on an identifier is `invalid_uuid`; the text types and validators raise errors whose type is
    the reason itself; every other `*_type` is `invalid_type`; anything else `invalid_format`."""
    kind = str(error.get("type", ""))
    reason = _TYPE_REASONS.get(kind)
    if reason is not None:
        return reason
    if kind == "string_pattern_mismatch":
        context = error.get("ctx")
        if isinstance(context, Mapping) and context.get("pattern") in _IDENTIFIER_PATTERNS:
            return "invalid_uuid"
    if kind in REASONS:
        return kind
    if kind.endswith("_type"):
        return "invalid_type"
    return "invalid_format"


def path_for(location: Sequence[object], reason: str) -> str:
    """The location followed by the dotted path in the API's names, list indices zero-based
    (`body.exampleQuestions.2`); invalid JSON is always at `body`."""
    parts = [str(part) for part in location]
    if reason == "invalid_json" or not parts:
        return "body"
    if parts[0] not in _LOCATIONS:
        parts.insert(0, "body")
    return ".".join(parts)[:FIELD_PATH_MAX_LENGTH]


def field_issues(errors: Iterable[Mapping[str, Any]]) -> list[FieldIssue]:
    """One `FieldIssue` per error, in the validator's order, never carrying the input."""
    issues: list[FieldIssue] = []
    for error in errors:
        reason = reason_for(error)
        location = error.get("loc", ())
        issues.append(
            FieldIssue(path_for(location if isinstance(location, Sequence) else (), reason), reason)
        )
    return issues


# --- Rendering ------------------------------------------------------------------------------


def problem_body(problem: DocChatProblem, correlation: str) -> Problem:
    entry = problem.entry
    return Problem(
        type=entry.problem_type,
        title=problem.title,
        status=problem.status,
        detail=problem.detail,
        code=entry.code,
        correlation_id=correlation,
        retry_after_seconds=problem.retry_after_seconds,
        errors=[FieldError(path=issue.path, reason=issue.reason) for issue in problem.errors]
        or None,
        session_id=problem.session_id,
        turn_id=problem.turn_id,
    )


def problem_response(
    problem: DocChatProblem, *, headers: Mapping[str, str] | None = None
) -> Response:
    """The problem's response in the current request's trace: its body, `Retry-After` when it
    carries `retryAfterSeconds`, and `WWW-Authenticate` for an in-app `401`."""
    body = problem_body(problem, correlation_id())
    sent = dict(headers or {})
    if problem.retry_after_seconds is not None:
        sent["Retry-After"] = str(problem.retry_after_seconds)
    if isinstance(problem, AuthenticationFailure) and problem.www_authenticate is not None:
        sent["WWW-Authenticate"] = problem.www_authenticate
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=problem.status,
        headers=sent,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _answer(
    scope: Scope, problem: DocChatProblem, headers: Mapping[str, str] | None = None
) -> Response:
    operation = operation_of(scope)
    record_problem(
        server_span_of(scope), problem.entry.code, problem.status, operation or NO_OPERATION
    )
    # A 404 an operation answers is always a scoped identifier it couldn't reach; the role and
    # administrator refusals are recorded by the route class where they happen.
    if operation is not None and problem.status == HTTP_NOT_FOUND:
        record_denial("not_found", operation)
    return problem_response(problem, headers=headers)


async def handle_problem(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, DocChatProblem):  # pragma: no cover - registered for DocChatProblem
        raise exc
    return _answer(request.scope, exc)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered for it
        raise exc
    issues = field_issues(exc.errors())
    # A validation error without a location still has to name one: the body as a whole.
    return _answer(
        request.scope, ValidationFailed(errors=issues or [FieldIssue("body", "invalid_format")])
    )


class RouteTable:
    """Every route's path and methods, so a `405` names all the methods of its path, where
    Starlette would name only those of the first route with that path."""

    def __init__(self, routes: Iterable[tuple[str, Iterable[str]]]) -> None:
        self._routes = [(compile_path(path)[0], frozenset(methods)) for path, methods in routes]

    @classmethod
    def of(cls, app: FastAPI) -> RouteTable:
        """The app's table, read from its routes once and kept on the app."""
        table = getattr(app.state, ROUTE_TABLE_STATE, None)
        if not isinstance(table, RouteTable):
            table = cls(
                (context.path, context.methods or ())
                for context in iter_route_contexts(app.routes)
                if context.path is not None
            )
            setattr(app.state, ROUTE_TABLE_STATE, table)
        return table

    def allowed(self, path: str) -> list[str]:
        methods: set[str] = set()
        for regex, route_methods in self._routes:
            if regex.match(path):
                methods |= route_methods
        return sorted(methods)


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registered for it
        raise exc
    if exc.status_code == HTTP_NOT_FOUND:
        return _answer(request.scope, RouteNotFound())
    if exc.status_code == HTTP_METHOD_NOT_ALLOWED:
        allowed = RouteTable.of(request.app).allowed(request.url.path)
        return _answer(request.scope, MethodNotAllowed(), {"Allow": ", ".join(allowed)})
    # No route raises another HTTP status; one that does is a defect, answered and logged as one.
    report_request_failed(server_span_of(request.scope), exc)
    return _answer(request.scope, InternalError())


def install_problem_handlers(app: FastAPI) -> None:
    handlers: dict[type[Exception], Callable[[Request, Exception], Any]] = {
        DocChatProblem: handle_problem,
        RequestValidationError: handle_validation_error,
        StarletteHTTPException: handle_http_exception,
    }
    for exc_class, handler in handlers.items():
        app.add_exception_handler(exc_class, handler)


# --- The failure boundary --------------------------------------------------------------------


class UnhandledErrorMiddleware:
    """Answers an exception no handler maps with `500 internal_error`, after one `request_failed`
    record and the failure on the server span. Once a response has started (a stream) no problem
    can be sent; the stream's own writer is that response's boundary, and the connection simply
    ends."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:
            # The client went away while its body was read; there is no one to answer.
            return
        # The boundary of section 1.3 step 6: every exception becomes one record and one answer.
        except Exception as exc:  # noqa: BLE001
            report_request_failed(server_span_of(scope), exc)
            if started:
                return
            response = _answer(scope, InternalError())
            await response(scope, receive, send)
