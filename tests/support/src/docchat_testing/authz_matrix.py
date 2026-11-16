"""The generator of the authorisation matrix test (`84-testing.md` section 3; `11-api.md`
sections 1.4, 1.13 and acceptance criterion 1; `82-security.md` SEC-11 to SEC-14; T-001, T-565).

`generate(document)` reads the OpenAPI document the app under test exports (never the committed
file, so an unexported route can't hide) and yields one `MatrixCase` per operation and check:

- a missing `x-docchat-access`, `x-docchat-scoped-params` or `x-docchat-error-codes` is the
  failing case `missing_access_metadata`;
- `public`: anonymous answers `200` with an empty body, and the operation must be `getHealth`;
- every other operation: `401` with no `Authorization`, with `Bearer not-a-token`, and with no
  header and a 33 KiB body (never `413`); `erin` (no role) answers `403 role_not_assigned`;
- `admin`: `alice` answers `403 admin_required` with real IDs, with random IDs and with an
  invalid body (never `404`, `412`, `422` or `428`);
- each scoped parameter: `carol` (admin operations) or `alice` (user operations) with an
  identifier of the same kind she can't reach and with a random one both get the operation's
  not-found answer, the two bodies byte-identical once `correlationId` is removed;
- the unmodified request with the right caller answers `2xx`;
- an operation with fewer than three cases is the failing case `too_few_cases` (the public
  operation, which has exactly one check, excepted).

An administrator reaches every project and index, and a person or a logical index belongs to no
project, so for an `admin` operation a scoped parameter of kind `user` or `index` gets only the
random identifier, even under `projectId` (the member and mapping operations; adding another
project's member or mapping another project's index is a valid request). At an array pointer
whose example array is empty, the identifier becomes its one element.

An operation that requires `If-Match` sends the current version of the resource section 1.9
names for it (`IF_MATCH_KINDS`), from `World.versions`, so its positive control can pass the
precondition; a refusal case never reaches the precondition, which comes after every lookup.

Identifiers are placeholders (`IdRef`) resolved when a case runs, through `World`, the seeded
data's name-to-kind map: `projectId` is a project, `indexId` an index, and so on; a scoped name
outside the map, or an example identifier that can't be resolved, fails the case
`unresolved_example`. A request comes from the operation's OpenAPI request example when it has
one, else from `fill_schema` over its request schema.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

import httpx

ACCESS: Final = "x-docchat-access"
SCOPED: Final = "x-docchat-scoped-params"
CODES: Final = "x-docchat-error-codes"
EXTENSIONS: Final = (ACCESS, SCOPED, CODES)
PUBLIC_OPERATION: Final = "getHealth"
OVERSIZED_BODY_BYTES: Final = 33 * 1024
MIN_CASES: Final = 3
BODY_POINTER: Final = "body:/"

ID_KINDS: Final[Mapping[str, str]] = {
    "projectId": "project",
    "indexId": "index",
    "documentId": "document",
    "versionId": "version",
    "runId": "run",
    "sessionId": "session",
    "turnId": "turn",
    "citationNumber": "citation",
    "factId": "fact",
    "userId": "user",
    "initialMemberIds": "user",
    "versionIds": "version",
}
"""The constant name-to-kind map of section 3.2."""

BODY_POINTER_ANSWERS: Final[Mapping[tuple[str, str], tuple[int, str | None]]] = {
    ("adminCreateProject", "body:/initialMemberIds"): (422, "validation_failed"),
    ("adminReserveUpload", "body:/projectId"): (422, "project_not_mapped"),
    ("adminSubmitUploads", "body:/versionIds"): (200, None),
}
"""The three body pointers whose not-found answer isn't a `404` (section 3.4)."""

IF_MATCH_KINDS: Final[Mapping[str, str]] = {
    "adminUpdateProject": "project",
    "adminAddProjectMember": "project",
    "adminRemoveProjectMember": "project",
    "adminMapProjectIndex": "project",
    "adminUnmapProjectIndex": "project",
    "adminUpdateIndex": "index",
    "adminStartReingest": "index",
    "adminDeleteIndex": "index",
    "adminDeleteDocument": "document",
    "adminReingestDocument": "document",
    "updateMemorySettings": "memory",
    "updateMemoryFact": "memory",
    "deleteMemoryFact": "memory",
    "clearMemory": "memory",
}
"""The 14 `If-Match` operations and the resource whose version each must name (`11-api.md`
section 1.9): members and mappings are parts of their project."""

SHARED_KINDS: Final = frozenset({"user", "index"})
"""Kinds no project owns: an administrator reaches every one under any project."""

IF_MATCH_HEADER: Final = "If-Match"

ECHOED_IDENTIFIERS: Final = frozenset({("adminSubmitUploads", "body:/versionIds")})
"""The scoped parameters whose not-found answer names each identifier it was sent
(`adminSubmitUploads` reports `{versionId, outcome: not_found}` per version, `11-api.md`
section 3.11.9), so its two bodies are compared with those identifiers masked."""

_NOT_FOUND_CODES: Final[Mapping[str, str]] = {
    "project": "project_not_found",
    "index": "index_not_found",
    "document": "document_not_found",
    "version": "version_not_found",
    "run": "run_not_found",
    "session": "session_not_found",
    "turn": "turn_not_found",
    "citation": "citation_not_found",
    "fact": "memory_fact_not_found",
    "user": "user_not_found",
}
_PATH_PARAMETER: Final = re.compile(r"{([^}]+)}")
_HTTP_METHODS: Final = ("get", "put", "post", "delete", "patch")

type Caller = Literal["anonymous", "alice", "bob", "carol", "dave", "erin"]
type Which = Literal["reachable", "unreachable", "random"]


@dataclass(frozen=True, slots=True)
class IdRef:
    """An identifier placeholder: the `which` identifier of `kind`, resolved when a case runs."""

    kind: str
    which: Which = "reachable"


@dataclass(frozen=True, slots=True)
class World:
    """The seeded identifiers: for each kind, the one the right caller reaches and one of the
    same kind in another project, of another user or under another index."""

    reachable: Mapping[str, str]
    unreachable: Mapping[str, str]
    versions: Mapping[str, str] = field(default_factory=dict)
    """The `If-Match` value naming the current version of each kind's reachable resource."""
    absent: Mapping[str, str] = field(default_factory=dict)
    """For a kind whose identifiers aren't UUIDs (a citation number, 1 to 16), the identifier
    that stands for a random one: a valid value that no seeded resource holds."""

    def version(self, kind: str) -> str:
        value = self.versions.get(kind)
        if value is None:
            raise UnresolvedExampleError(f"version of {kind}")
        return value

    def resolve(self, ref: IdRef) -> str:
        if ref.which == "random":
            return self.absent.get(ref.kind) or str(uuid.uuid4())
        known = self.reachable if ref.which == "reachable" else self.unreachable
        value = known.get(ref.kind)
        if value is None:
            raise UnresolvedExampleError(ref.kind)
        return value


class UnresolvedExampleError(LookupError):
    """An identifier of a kind the seeded world doesn't hold."""


@dataclass(frozen=True, slots=True)
class MatrixRequest:
    """One request with identifier placeholders anywhere in its path, query or body."""

    method: str
    path: str
    path_params: Mapping[str, Any] = field(default_factory=dict)
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None
    content: bytes | None = None
    if_match: str | None = None
    """The kind whose current version `If-Match` names, for the `If-Match` operations."""

    def with_ids(self, which: Which) -> MatrixRequest:
        """Every placeholder switched to `which` identifiers."""
        return replace(
            self,
            path_params=_switched(self.path_params, which),
            query=_switched(self.query, which),
            body=_switched(self.body, which),
        )


@dataclass(frozen=True, slots=True)
class Expectation:
    status: int | None = None
    """An exact status; `None` with `success` for any `2xx`."""
    code: str | None = None
    success: bool = False
    empty_body: bool = False
    never: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class MatrixCase:
    """One generated check of one operation."""

    operation_id: str
    check: str
    caller: Caller = "anonymous"
    authorization: str | None = None
    request: MatrixRequest | None = None
    expect: Expectation = field(default_factory=Expectation)
    compare_with: MatrixRequest | None = None
    """For a scoped parameter: the request whose body must be identical to `request`'s."""
    mask_identifiers: bool = False
    """Compare the two bodies with each request's own identifiers masked (`ECHOED_IDENTIFIERS`)."""
    failure: str | None = None
    """A generator failure (`missing_access_metadata`, `unresolved_example`, `too_few_cases`);
    the case fails when it runs."""

    @property
    def id(self) -> str:
        return f"{self.operation_id}-{self.check}"


def _switched(value: Any, which: Which) -> Any:
    if isinstance(value, IdRef):
        return replace(value, which=which)
    if isinstance(value, Mapping):
        return {key: _switched(item, which) for key, item in value.items()}
    if isinstance(value, list):
        return [_switched(item, which) for item in value]
    return value


def _resolved(value: Any, world: World, found: list[str] | None = None) -> Any:
    """`value` with every placeholder resolved; each identifier used is added to `found`."""
    if isinstance(value, IdRef):
        identifier = world.resolve(value)
        if found is not None:
            found.append(identifier)
        return identifier
    if isinstance(value, Mapping):
        return {key: _resolved(item, world, found) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolved(item, world, found) for item in value]
    return value


# --- Bodies from schemas ---------------------------------------------------------------------


def _deref(schema: Mapping[str, Any], document: Mapping[str, Any]) -> Mapping[str, Any]:
    seen: set[str] = set()
    while isinstance(ref := schema.get("$ref"), str):
        if ref in seen or not ref.startswith("#/"):
            raise UnresolvedExampleError(ref)
        seen.add(ref)
        target: Any = document
        for part in ref[2:].split("/"):
            target = target[part]
        schema = target
    return schema


def _scalar(schema: Mapping[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "string":
        return "x" * max(int(schema.get("minLength", 0)), 1)
    if kind in {"integer", "number"}:
        return schema.get("minimum", 0)
    return False if kind == "boolean" else None


def _identifier(schema: Mapping[str, Any], name: str | None) -> Any:
    kind = None if name is None else ID_KINDS.get(name)
    if kind is None or schema.get("type") not in {"string", "integer", "array"}:
        return None
    ref = IdRef(kind)
    return [ref] if schema.get("type") == "array" else ref


def fill_schema(
    schema: Mapping[str, Any], document: Mapping[str, Any], name: str | None = None
) -> Any:
    """A value that satisfies `schema`: every required property; an identifier field (by the
    name-to-kind map) as a placeholder; strings at `minLength` (`"x"` repeated, at least one);
    numbers at `minimum` (else 0); booleans `false`; arrays of `max(minItems, 1)` items; an enum's
    first value; `const` honoured; the first member of a `oneOf` or `anyOf`."""
    schema = _deref(schema, document)
    identifier = _identifier(schema, name)
    members = [
        member
        for member in (*schema.get("oneOf", ()), *schema.get("anyOf", ()))
        if member.get("type") != "null"
    ]
    if identifier is not None:
        value: Any = identifier
    elif "const" in schema:
        value = schema["const"]
    elif "enum" in schema:
        value = schema["enum"][0]
    elif members:
        value = fill_schema(members[0], document, name)
    elif schema.get("type") == "object" or "properties" in schema:
        properties: Mapping[str, Any] = schema.get("properties", {})
        value = {
            key: fill_schema(properties[key], document, key)
            for key in schema.get("required", ())
            if key in properties
        }
    elif schema.get("type") == "array":
        count = max(int(schema.get("minItems", 0)), 1)
        value = [fill_schema(schema.get("items", {}), document, name) for _ in range(count)]
    else:
        value = _scalar(schema)
    return value


def _example(operation: Mapping[str, Any]) -> Any:
    content = operation.get("requestBody", {}).get("content", {})
    for media in content.values():
        if "example" in media:
            return media["example"]
    return None


def _with_placeholders(value: Any, name: str | None = None) -> Any:
    """An example's identifier fields become placeholders, so they resolve against the world."""
    if isinstance(value, Mapping):
        return {key: _with_placeholders(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_placeholders(item, name) for item in value]
    kind = None if name is None else ID_KINDS.get(name)
    if kind is not None and isinstance(value, str | int):
        return IdRef(kind)
    return value


def build_request(
    method: str, path: str, operation: Mapping[str, Any], document: Mapping[str, Any]
) -> MatrixRequest:
    """The operation's unmodified request: identifiers from the world, its example body or a
    body filled from its schema, its required query parameters filled from their schemas."""
    path_params: dict[str, Any] = {}
    query: dict[str, Any] = {}
    for name in _PATH_PARAMETER.findall(path):
        if name not in ID_KINDS:
            raise UnresolvedExampleError(name)
        path_params[name] = IdRef(ID_KINDS[name])
    for parameter in operation.get("parameters", ()):
        if parameter.get("in") == "query" and parameter.get("required"):
            query[parameter["name"]] = fill_schema(
                parameter.get("schema", {}), document, parameter["name"]
            )
    if_match = None
    for parameter in operation.get("parameters", ()):
        if parameter.get("in") == "header" and parameter.get("name") == IF_MATCH_HEADER:
            operation_id = str(operation.get("operationId"))
            if operation_id not in IF_MATCH_KINDS:
                raise UnresolvedExampleError(IF_MATCH_HEADER)
            if_match = IF_MATCH_KINDS[operation_id]
    body: Any = None
    request_body = operation.get("requestBody")
    if request_body is not None:
        example = _example(operation)
        if example is not None:
            body = _with_placeholders(example)
        else:
            schema = request_body.get("content", {}).get("application/json", {}).get("schema")
            body = None if schema is None else fill_schema(schema, document)
    return MatrixRequest(
        method=method.upper(),
        path=path,
        path_params=path_params,
        query=query,
        body=body,
        if_match=if_match,
    )


# --- Generation ------------------------------------------------------------------------------


def _not_found(
    operation_id: str, operation: Mapping[str, Any], name: str
) -> tuple[Expectation, str | None]:
    """The not-found answer of one scoped parameter (section 3.4); `None` as the second item
    when the parameter can't be mapped to a kind."""
    parameter = name.removeprefix(BODY_POINTER).split("/")[0]
    kind = ID_KINDS.get(parameter)
    special = BODY_POINTER_ANSWERS.get((operation_id, name))
    if special is not None:
        status, code = special
        return Expectation(status=status, code=code), kind or "body"
    if kind is None:
        return Expectation(), None
    codes = operation.get(CODES, ())
    code = _NOT_FOUND_CODES[kind]
    if code not in codes:
        # An operation that answers every scoped parameter with its one `404` code (the citation
        # operations: `citation_not_found` for the session, the turn and the number alike).
        not_found = [listed for listed in codes if listed in _NOT_FOUND_CODES.values()]
        if len(not_found) != 1:
            return Expectation(), None
        code = not_found[0]
    return Expectation(status=404, code=code), kind


def _scoped_request(request: MatrixRequest, name: str, which: Which) -> MatrixRequest:
    """The request with only the scoped parameter `name` set to a `which` identifier."""

    def switch(value: Any) -> Any:
        return _switched(value, which)

    if name.startswith(BODY_POINTER):
        key = name.removeprefix(BODY_POINTER).split("/")[0]
        body = dict(request.body or {})
        if body.get(key) == []:
            # An empty example array has no element to substitute into; it gets one.
            body[key] = [IdRef(ID_KINDS.get(key, key), which)]
        elif body.get(key) is not None:
            body[key] = switch(body[key])
        elif key in ID_KINDS:
            # An optional identifier the example leaves out or null (`createChatTurn`'s
            # `sessionId`): the scoped case sends one.
            body[key] = IdRef(ID_KINDS[key], which)
        return replace(request, body=body)
    if name in request.path_params:
        return replace(
            request, path_params={**request.path_params, name: switch(request.path_params[name])}
        )
    return replace(request, query={**request.query, name: IdRef(ID_KINDS.get(name, name), which)})


def _nested(path: str, name: str) -> bool:
    """A scoped parameter that sits under another identifier: a path parameter after the first,
    or a body field (resolved under the operation's path parameters)."""
    if name.startswith(BODY_POINTER):
        return True
    names = _PATH_PARAMETER.findall(path)
    return name in names[1:]


def _operation_cases(
    method: str, path: str, operation: Mapping[str, Any], document: Mapping[str, Any]
) -> Iterator[MatrixCase]:
    operation_id = str(operation.get("operationId", f"{method.upper()} {path}"))
    missing = [extension for extension in EXTENSIONS if extension not in operation]
    if missing:
        yield MatrixCase(operation_id, "missing_access_metadata", failure="missing_access_metadata")
        return
    access = operation[ACCESS]
    try:
        request = build_request(method, path, operation, document)
    except UnresolvedExampleError:
        yield MatrixCase(operation_id, "unresolved_example", failure="unresolved_example")
        return
    if access == "public":
        if operation_id != PUBLIC_OPERATION:
            yield MatrixCase(
                operation_id, "public_is_only_get_health", failure="unexpected_public_operation"
            )
            return
        yield MatrixCase(
            operation_id,
            "anonymous_200",
            request=request,
            expect=Expectation(status=200, empty_body=True),
        )
        return
    if access not in {"user", "admin"}:
        return
    unauthenticated = Expectation(status=401, never=frozenset({413}))
    yield MatrixCase(
        operation_id, "401_without_authorization", request=request, expect=unauthenticated
    )
    yield MatrixCase(
        operation_id,
        "401_with_bearer_not_a_token",
        authorization="Bearer not-a-token",
        request=request,
        expect=unauthenticated,
    )
    yield MatrixCase(
        operation_id,
        "401_with_33_kib_body",
        request=replace(request, body=None, content=b"x" * OVERSIZED_BODY_BYTES),
        expect=unauthenticated,
    )
    yield MatrixCase(
        operation_id,
        "403_without_role",
        caller="erin",
        request=request,
        expect=Expectation(status=403, code="role_not_assigned"),
    )
    if access == "admin":
        refused = Expectation(
            status=403, code="admin_required", never=frozenset({404, 412, 422, 428})
        )
        yield MatrixCase(
            operation_id, "403_user_real_ids", caller="alice", request=request, expect=refused
        )
        yield MatrixCase(
            operation_id,
            "403_user_random_ids",
            caller="alice",
            request=request.with_ids("random"),
            expect=refused,
        )
        yield MatrixCase(
            operation_id,
            "403_user_invalid_body",
            caller="alice",
            request=replace(request, body=None, content=b"{not json"),
            expect=refused,
        )
    caller: Caller = "carol" if access == "admin" else "alice"
    for name in operation[SCOPED]:
        expectation, kind = _not_found(operation_id, operation, name)
        if kind is None:
            yield MatrixCase(operation_id, f"scoped_{name}", failure="unresolved_example")
            continue
        if access == "admin" and (not _nested(path, name) or kind in SHARED_KINDS):
            # An administrator reaches every project and index, so a parameter that doesn't
            # sit under another identifier, or that names a person or a logical index (which no
            # project owns), has only the random identifier to refuse.
            yield MatrixCase(
                operation_id,
                f"not_found_{name}",
                caller=caller,
                request=_scoped_request(request, name, "random"),
                expect=expectation,
            )
            continue
        yield MatrixCase(
            operation_id,
            f"not_found_{name}",
            caller=caller,
            request=_scoped_request(request, name, "unreachable"),
            compare_with=_scoped_request(request, name, "random"),
            expect=expectation,
            mask_identifiers=(operation_id, name) in ECHOED_IDENTIFIERS,
        )
    yield MatrixCase(
        operation_id,
        "2xx_right_caller",
        caller=caller,
        request=request,
        expect=Expectation(success=True),
    )


def operations(document: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    for path, item in document.get("paths", {}).items():
        for method in _HTTP_METHODS:
            if method in item:
                yield method, path, item[method]


def generate(document: Mapping[str, Any]) -> list[MatrixCase]:
    """Every case of every operation of `document`."""
    cases: list[MatrixCase] = []
    for method, path, operation in operations(document):
        produced = list(_operation_cases(method, path, operation, document))
        public = operation.get(ACCESS) == "public" and not any(case.failure for case in produced)
        if len(produced) < MIN_CASES and not public:
            operation_id = str(operation.get("operationId", f"{method.upper()} {path}"))
            produced.append(MatrixCase(operation_id, "too_few_cases", failure="too_few_cases"))
        cases.extend(produced)
    return cases


# --- Running ---------------------------------------------------------------------------------


type TokenFor = Callable[[Caller], str]


def _url(request: MatrixRequest, world: World, found: list[str] | None = None) -> str:
    path = request.path
    for name, value in _resolved(dict(request.path_params), world, found).items():
        path = path.replace("{" + name + "}", str(value))
    return path


async def _send(
    client: httpx.AsyncClient,
    request: MatrixRequest,
    world: World,
    *,
    authorization: str | None = None,
) -> tuple[httpx.Response, list[str]]:
    """The response and every identifier the request carried."""
    found: list[str] = []
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    if request.if_match is not None:
        headers[IF_MATCH_HEADER] = world.version(request.if_match)
    kwargs: dict[str, Any] = {"params": _resolved(dict(request.query), world, found)}
    if request.content is not None:
        headers["Content-Type"] = "application/json"
        kwargs["content"] = request.content
    elif request.body is not None:
        kwargs["json"] = _resolved(request.body, world, found)
    url = _url(request, world, found)
    response = await client.request(request.method, url, headers=headers, **kwargs)
    return response, found


async def send(
    client: httpx.AsyncClient,
    request: MatrixRequest,
    world: World,
    *,
    authorization: str | None = None,
) -> httpx.Response:
    response, _ = await _send(client, request, world, authorization=authorization)
    return response


IDENTIFIER_MASK: Final = "<identifier>"


def _masked(value: Any, identifiers: frozenset[str]) -> Any:
    if isinstance(value, str):
        return IDENTIFIER_MASK if value.lower() in identifiers else value
    if isinstance(value, dict):
        return {key: _masked(item, identifiers) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item, identifiers) for item in value]
    return value


def without_correlation(response: httpx.Response, identifiers: Iterable[str] = ()) -> bytes:
    """The body with `correlationId` removed, for the byte comparison of two not-found answers.

    A string equal to one of `identifiers` is masked: the cases of `ECHOED_IDENTIFIERS` pass the
    request's own identifiers, because that answer names each one it was sent."""
    try:
        body = response.json()
    except ValueError:
        return response.content
    if isinstance(body, dict):
        body.pop("correlationId", None)
    masked = _masked(body, frozenset(identifier.lower() for identifier in identifiers))
    return json.dumps(masked, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _check(case: MatrixCase, response: httpx.Response) -> None:
    expect = case.expect
    status = response.status_code
    assert status not in expect.never, f"{case.id}: answered {status}"
    if expect.success:
        assert 200 <= status < 300, f"{case.id}: expected 2xx, got {status}"
    elif expect.status is not None:
        assert status == expect.status, f"{case.id}: expected {expect.status}, got {status}"
    if expect.code is not None:
        assert response.json().get("code") == expect.code, f"{case.id}: expected {expect.code}"
    if expect.empty_body:
        assert response.content == b"", f"{case.id}: expected an empty body"


async def check_case(
    case: MatrixCase,
    client: httpx.AsyncClient,
    world: World,
    token_for: TokenFor,
) -> None:
    """Run one case against the app behind `client`; an `AssertionError` names the case."""
    assert case.failure is None, f"{case.id}: {case.failure}"
    assert case.request is not None
    authorization = case.authorization
    if authorization is None and case.caller != "anonymous":
        authorization = f"Bearer {token_for(case.caller)}"
    response, identifiers = await _send(client, case.request, world, authorization=authorization)
    _check(case, response)
    if case.compare_with is not None:
        other, other_identifiers = await _send(
            client, case.compare_with, world, authorization=authorization
        )
        _check(case, other)
        if not case.mask_identifiers:
            identifiers, other_identifiers = [], []
        assert without_correlation(response, identifiers) == without_correlation(
            other, other_identifiers
        ), f"{case.id}: the unreachable and the random identifier got different bodies"


def case_ids(cases: Sequence[MatrixCase]) -> list[str]:
    return [case.id for case in cases]
