"""`DocChatRoute`, the route class of every `/v1` route, its route options and `current_principal`
(`11-api.md` sections 1.1, 1.3, 1.4, 1.7, 1.11 and 1.13; `82-security.md` SEC-03, SEC-12 and
SEC-13).

FastAPI reads and parses a request's body before it resolves the endpoint's dependencies, so
checks written as dependencies would let `413`, `415` or `422` answer before `401` and would parse
the bodies of refused requests. The route class therefore runs the pre-body checks of section 1.3
in its route handler, before FastAPI's own handler reads anything:

1. token validation and the role check (`TokenValidator.validate` on every `Authorization` value),
   which build the `Principal`; the server span gets `enduser.id` and `docchat.auth.client`;
2. the per-user burst limit, unless the route declares `burst_limited=False`;
3. `require_admin` on every `admin` route;
4. the per-user write limit when the route is write-limited;
5. first-sign-in provisioning (`ensure_user`).

The `Principal` is kept in `request.state.principal`; endpoints take it through
`current_principal`, which only reads it. The route class also places its options, its operationId
and the server span in the scope for the request-size guard, the problem handlers and the audit
helper; maps the data layer's `SqlUnavailable` and `SqlTimeout` to `503 dependency_unavailable`;
and builds the operation's OpenAPI access extensions from the endpoint's `access` declaration.

A route declares its body rules with `route_options` and its access metadata with `access`, both
decorators placed below the router decorator:

    @router.post("/admin/projects", operation_id="adminCreateProject", tags=["admin-projects"])
    @access("admin", scoped_params=["body:/initialMemberIds"], error_codes=["project_name_taken"])
    async def admin_create_project(body: ..., principal: CurrentPrincipal) -> AdminProject: ...
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Any, Final, override

from fastapi import Depends, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from docchat_api.http.access import (
    BODY_POINTER_PREFIX,
    AccessSpec,
    access_of,
    error_codes,
    extensions,
)
from docchat_api.http.context import OPERATION_KEY, ROUTE_OPTIONS_KEY, SERVER_SPAN_KEY
from docchat_api.http.problems import DependencyUnavailable, ValidationFailed
from docchat_api.http.state import current_process, request_ops
from docchat_api.http.telemetry import current_server_span, record_audit_action, record_caller
from docchat_core.access import AdminRequired, record_denial, require_admin
from docchat_core.auth import Principal, RoleNotAssigned
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.sql.engine import SqlTimeout, SqlUnavailable
from docchat_core.users import ensure_user

JSON_MEDIA_TYPE: Final = "application/json"
DEFAULT_BODY_LIMIT: Final = 65_536
WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OPTIONS_ATTRIBUTE: Final = "__docchat_route_options__"
PRINCIPAL_STATE: Final = "principal"
PROVISIONED_ACTION: Final = "user.provisioned"

IF_MATCH_PARAMETER: Final[Mapping[str, Any]] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 1},
}


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteOptions:
    """A route's body rules (section 1.7). `body_limit` is the JSON body limit in bytes (`None`
    for a route that enforces its own limit while it streams); `content_types` the media types a
    body may have; `write_limited` `None` takes the method's default (`True` on `POST`, `PUT`,
    `PATCH` and `DELETE`)."""

    body_limit: int | None = DEFAULT_BODY_LIMIT
    content_types: tuple[str, ...] = (JSON_MEDIA_TYPE,)
    burst_limited: bool = True
    write_limited: bool | None = None

    def __post_init__(self) -> None:
        if self.body_limit is not None and self.body_limit < 1:
            raise ValueError("a body limit is a positive number of bytes")
        if not self.content_types or any(
            value != value.strip().lower() or ";" in value for value in self.content_types
        ):
            raise ValueError("content types are bare lower-case media types")

    def for_methods(self, methods: Sequence[str]) -> RouteOptions:
        """The options with `write_limited` decided for a route answering `methods`."""
        if self.write_limited is not None:
            return self
        return replace(self, write_limited=any(method in WRITE_METHODS for method in methods))


DEFAULT_ROUTE_OPTIONS: Final = RouteOptions()
"""The options of every route that declares none: 65,536 bytes of `application/json`, burst
limited, write-limited by method."""


def route_options(
    *,
    body_limit: int | None = DEFAULT_BODY_LIMIT,
    content_types: Sequence[str] = (JSON_MEDIA_TYPE,),
    burst_limited: bool = True,
    write_limited: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a route's body rules; only the routes of section 1.7's table that deviate from
    the defaults use it. Put it below the router decorator."""
    options = RouteOptions(
        body_limit=body_limit,
        content_types=tuple(content_types),
        burst_limited=burst_limited,
        write_limited=write_limited,
    )

    def mark[F: Callable[..., Any]](endpoint: F) -> F:
        setattr(endpoint, OPTIONS_ATTRIBUTE, options)
        return endpoint

    return mark


def options_of(endpoint: Callable[..., Any]) -> RouteOptions:
    options = getattr(endpoint, OPTIONS_ATTRIBUTE, None)
    return options if isinstance(options, RouteOptions) else DEFAULT_ROUTE_OPTIONS


class PrincipalMissingError(RuntimeError):
    """`current_principal` used outside a route the route class serves (a defect)."""


def current_principal(request: Request) -> Principal:
    """The caller, as token validation built it; never read from anything the request sends."""
    principal = getattr(request.state, PRINCIPAL_STATE, None)
    if not isinstance(principal, Principal):
        raise PrincipalMissingError("no validated principal on this request")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
"""The endpoint parameter type of the caller."""


def _merged(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merged(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value]
        else:
            merged[key] = value
    return merged


def _parameter_names(dependant: Dependant) -> tuple[set[str], set[str]]:
    """The path and query parameter names of an endpoint and its dependencies, with a query
    model's fields as the parameters they become."""
    path: set[str] = set()
    query: set[str] = set()
    pending = [dependant]
    while pending:
        current = pending.pop()
        path.update(field.alias for field in current.path_params)
        for field in current.query_params:
            annotation = field.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                query.update(info.alias or name for name, info in annotation.model_fields.items())
            else:
                query.add(field.alias)
        pending.extend(current.dependencies)
    return path, query


def _unwrapped(exc: StarletteHTTPException) -> BaseException:
    """FastAPI turns any exception raised while it reads the body into a `400`; the request-size
    guard's `413` and `415` and invalid UTF-8 come back out of it."""
    cause = exc.__cause__
    if isinstance(cause, DocChatProblem | ClientDisconnect):
        return cause
    if isinstance(cause, UnicodeDecodeError):
        return ValidationFailed(errors=[FieldIssue("body", "invalid_json")])
    return exc


class DocChatRoute(APIRoute):
    """The route class of the `/v1` router; every endpoint it serves declares `access` with the
    level `user` or `admin` and an operationId."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        spec = access_of(endpoint)
        if spec is None or spec.level == "public":
            raise TypeError(f"{path}: a /v1 endpoint declares access 'user' or 'admin'")
        openapi_extra: Mapping[str, Any] = kwargs.pop("openapi_extra", None) or {}
        super().__init__(path, endpoint, **kwargs)
        if not self.operation_id:
            raise TypeError(f"{path}: every operation has an operationId")
        self.access_spec: AccessSpec = spec
        methods = sorted(self.methods or ())
        self.route_options: RouteOptions = options_of(endpoint).for_methods(methods)
        path_names, query_names = _parameter_names(self.dependant)
        for name in spec.scoped_params:
            if not name.startswith(BODY_POINTER_PREFIX) and name not in path_names | query_names:
                raise TypeError(f"{self.operation_id}: scoped parameter {name!r} isn't a parameter")
        self.error_codes: list[str] = error_codes(
            spec,
            burst_limited=self.route_options.burst_limited,
            write_limited=bool(self.route_options.write_limited),
            json_body=self.body_field is not None,
            parameters=bool(path_names or query_names),
        )
        extra = extensions(spec, self.error_codes)
        if spec.if_match:
            extra["parameters"] = [dict(IF_MATCH_PARAMETER)]
        self.openapi_extra = _merged(openapi_extra, extra)

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def handler(request: Request) -> Response:
            scope = request.scope
            scope[ROUTE_OPTIONS_KEY] = self.route_options
            scope[OPERATION_KEY] = self.operation_id
            scope[SERVER_SPAN_KEY] = current_server_span()
            try:
                await self._check_before_body(request)
                return await handle(request)
            except StarletteHTTPException as exc:
                raise _unwrapped(exc) from None
            except (SqlUnavailable, SqlTimeout):
                raise DependencyUnavailable() from None

        return handler

    async def _check_before_body(self, request: Request) -> None:
        """Steps 2 to 5 of section 1.3 (step 1 is the platform's)."""
        process = current_process(request)
        operation = str(self.operation_id)
        span = request.scope[SERVER_SPAN_KEY]
        try:
            principal = await process.validator.validate(request.headers.getlist("authorization"))
        except RoleNotAssigned:
            record_denial("role_not_assigned", operation)
            raise
        record_caller(span, principal)
        setattr(request.state, PRINCIPAL_STATE, principal)
        if self.route_options.burst_limited:
            process.burst_limiter.check(principal.oid)
        if self.access_spec.level == "admin":
            try:
                require_admin(principal)
            except AdminRequired:
                record_denial("admin_required", operation)
                raise
        if self.route_options.write_limited:
            ops = await request_ops(request)
            await process.write_limiter.check(principal, ops.api.write_requests_per_minute)
        if await ensure_user(process.sql, principal, process.throttle) == "provisioned":
            record_audit_action(span, PROVISIONED_ACTION)
