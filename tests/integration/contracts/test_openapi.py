"""T-041, the OpenAPI contract tests (`11-api.md` section 1.14 and acceptance criterion 2).

The document is the one the app exports (`app.openapi()`), and the answers come from the app
served in process with its whole middleware stack. The app runs over a data-access double that
serves only the seeded ops-config rows, because no route under test reads anything else, so this
contract half needs no compose service. The committed `apps/api/openapi.json` is compared byte for
byte by the API's unit tests and the CI job `contract`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, cast

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from docchat_core.errors import CODES
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_testing.fixtures.api import BuiltApp, build_app, ports_under_test, serving
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
DATA_MODEL: Final = REPOSITORY_ROOT / "docs" / "spec" / "80-data-model.md"
PROBLEM: Final = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
STREAM_EVENTS: Final = {
    "meta": "StreamMetaEvent",
    "status": "StreamStatusEvent",
    "delta": "StreamDeltaEvent",
    "citations": "StreamCitationsEvent",
    "followups": "StreamFollowupsEvent",
    "heartbeat": "StreamHeartbeatEvent",
    "done": "StreamDoneEvent",
    "error": "StreamErrorEvent",
}
"""The eight event models of `11-api.md` section 3.3.3, by their `type`."""
DOCUMENT_REFERENCE: Final = re.compile(r"document|version|path|blob|url", re.IGNORECASE)
"""Parameter names that would let a user operation name document content (SEC-17)."""
HTTP_METHODS: Final = frozenset({"get", "put", "post", "delete", "patch"})


class _OpsRows:
    """The data-access double: the seeded ops-config rows for the snapshot, and a provisioned
    caller."""

    def __init__(self) -> None:
        text = DATA_MODEL.read_text(encoding="utf-8")
        self.rows = dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))

    async def fetch_all(self, statement: object, parameters: object = None, **kwargs: Any) -> Any:
        return list(self.rows.items())

    async def run(self, work: Callable[[Any], Any], **kwargs: Any) -> Any:
        return ProvisionResult(provisioned=False, default_project_count=0)


@pytest.fixture
def built(signing_keys: SigningKeys) -> BuiltApp:
    return build_app(
        ports=ports_under_test(sql_engine=cast("SqlEngine", _OpsRows())), signing_keys=signing_keys
    )


@pytest.fixture
def document(built: BuiltApp) -> dict[str, Any]:
    exported: dict[str, Any] = built.app.openapi()
    return exported


def _operations(document: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    return {
        operation["operationId"]: (method, path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def test_t041_every_mounted_route_is_in_the_document(
    built: BuiltApp, document: dict[str, Any]
) -> None:
    mounted = {
        (method.lower(), str(context.path))
        for context in iter_route_contexts(built.app.routes)
        if isinstance(context.original_route, APIRoute)
        for method in context.original_route.methods or ()
        if method != "HEAD"
    }
    documented = {(method, path) for method, path, _ in _operations(document).values()}

    assert mounted == documented
    assert len(documented) == 45


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_t041_the_document_and_its_viewers_are_not_served(
    built: BuiltApp, path: str, alice: SharedPrincipal
) -> None:
    async with serving(built) as api:
        anonymous = await api.client.get(path)
        signed_in = await api.client.get(path, headers=alice.authorization())

    for response in (anonymous, signed_in):
        assert response.status_code == 404
        assert response.json()["code"] == "route_not_found"


def test_t041_get_health_is_the_only_operation_without_security(document: dict[str, Any]) -> None:
    without = [
        name
        for name, (_, _, operation) in _operations(document).items()
        if operation.get("security") == []
    ]

    assert without == ["getHealth"]


def test_t041_every_documented_error_status_references_problem(document: dict[str, Any]) -> None:
    for name, (_, _, operation) in _operations(document).items():
        errors = {
            status: response
            for status, response in operation["responses"].items()
            if int(status) >= 400
        }
        assert set(errors) == {
            str(CODES[code].status) for code in operation["x-docchat-error-codes"]
        }, name
        for status, response in errors.items():
            assert response["content"] == PROBLEM, (name, status)


def test_t041_the_chat_stream_is_ndjson_of_the_eight_events(document: dict[str, Any]) -> None:
    _, _, operation = _operations(document)["createChatTurn"]
    created = operation["responses"]["200"]
    union = document["components"]["schemas"]["ChatStreamEvent"]
    references = {name: f"#/components/schemas/{model}" for name, model in STREAM_EVENTS.items()}

    assert created["content"] == {
        "application/x-ndjson": {"schema": {"$ref": "#/components/schemas/ChatStreamEvent"}}
    }
    assert created["x-docchat-stream"] == "ndjson-one-event-per-line"
    assert sorted(entry["$ref"] for entry in union["oneOf"]) == sorted(references.values())
    assert union["discriminator"] == {"propertyName": "type", "mapping": references}
    for name, model in STREAM_EVENTS.items():
        assert document["components"]["schemas"][model]["properties"]["type"]["const"] == name


def test_t041_no_user_operation_names_a_document_version_or_path(
    document: dict[str, Any],
) -> None:
    offending = {
        name: parameter["name"]
        for name, (_, _, operation) in _operations(document).items()
        if operation["x-docchat-access"] == "user"
        for parameter in operation.get("parameters", [])
        if DOCUMENT_REFERENCE.search(parameter["name"])
    }

    assert offending == {}
