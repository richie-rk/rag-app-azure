from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from fastapi import APIRouter
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from docchat_api.app import AREA_ROUTERS, create_app
from docchat_api.chat.models import ChatTurnRequest
from docchat_api.conversations.feedback_routes import FeedbackRequest
from docchat_api.http.access import access
from docchat_api.http.routing import DocChatRoute
from docchat_api.openapi import (
    NDJSON_MEDIA_TYPE,
    PROBLEM_REF,
    StreamSpec,
    document_problems,
    document_security,
    document_streams,
    main,
    ndjson_stream,
    render,
    stream_of,
)
from docchat_api.settings import ApiSettings
from docchat_core.auth import AuthSettings
from docchat_core.cosmos import DOCUMENT_MODELS
from docchat_core.errors import CODES
from docchat_core.ingestion.models import IngestionQueueMessage
from docchat_core.ingestion.settings import EmbeddingModel, IngestionAppSettings
from docchat_core.settings import DocChatSettings

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "apps").is_dir())
COMMITTED = ROOT / "apps" / "api" / "openapi.json"
DOCUMENT = create_app().openapi()
OPERATIONS = {
    operation["operationId"]: (method, path, operation)
    for path, item in DOCUMENT["paths"].items()
    for method, operation in item.items()
}
BASE = [
    "authentication_required",
    "authentication_unavailable",
    "dependency_unavailable",
    "internal_error",
    "invalid_token",
    "role_not_assigned",
]
BURST = ["too_many_requests"]
ADMIN = ["admin_required"]
WRITE = ["write_rate_limited"]
BODY = ["request_too_large", "unsupported_media_type", "validation_failed"]
PARAMS = ["validation_failed"]
PRECONDITION = ["precondition_failed", "precondition_required"]
CATALOGUE = [
    "method_not_found",
    "method_inactive",
    "invalid_method_parameters",
    "unsupported_embedding_model",
    "unsupported_dimensions",
    "unsupported_analyzer",
]
INDEX = "/v1/admin/indexes/{indexId}"
DOCUMENT_PATH = INDEX + "/documents/{documentId}"


def admin_codes(*sets: list[str]) -> list[str]:
    """An administrator operation's codes: Base, Burst, Admin and the given sets and codes."""
    return sorted({*BASE, *BURST, *ADMIN, *(code for codes in sets for code in codes)})


ADMIN_JSON = [
    *BASE,
    "too_many_requests",
    "admin_required",
    "request_too_large",
    "unsupported_media_type",
    "validation_failed",
]
"""An administrator operation with a JSON body that isn't write-limited (Base, Burst, Admin,
Body and Params)."""
UPLOAD_GATE = ["index_not_found", "malware_scanning_not_ready", "index_not_accepting_uploads"]
CITATION = "/v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}"
SESSION = "/v1/sessions/{sessionId}"
CITATION_REFUSALS = ["citation_not_found", "document_changed"]
DOWNLOADS = ["rate_limited", "too_many_downloads", "citation_content_missing"]
"""The download limits and the missing blob of the two citation file operations."""


def test_regenerating_the_document_reproduces_the_committed_bytes() -> None:
    assert render(DOCUMENT) == COMMITTED.read_text(encoding="utf-8")


def test_the_document_is_openapi_3_1() -> None:
    assert DOCUMENT["openapi"] == "3.1.0"


def test_the_stage_operations_with_their_tags() -> None:
    assert {
        name: (method, path, op["tags"]) for name, (method, path, op) in OPERATIONS.items()
    } == {
        "getHealth": ("get", "/healthz", ["health"]),
        "getCurrentUser": ("get", "/v1/me", ["me"]),
        "listMyProjects": ("get", "/v1/projects", ["projects"]),
        "adminListProjects": ("get", "/v1/admin/projects", ["admin-projects"]),
        "adminCreateProject": ("post", "/v1/admin/projects", ["admin-projects"]),
        "adminGetProject": ("get", "/v1/admin/projects/{projectId}", ["admin-projects"]),
        "adminUpdateProject": ("patch", "/v1/admin/projects/{projectId}", ["admin-projects"]),
        "adminListProjectMembers": (
            "get",
            "/v1/admin/projects/{projectId}/members",
            ["admin-projects"],
        ),
        "adminAddProjectMember": (
            "put",
            "/v1/admin/projects/{projectId}/members/{userId}",
            ["admin-projects"],
        ),
        "adminRemoveProjectMember": (
            "delete",
            "/v1/admin/projects/{projectId}/members/{userId}",
            ["admin-projects"],
        ),
        "adminSearchUsers": ("get", "/v1/admin/users", ["admin-users"]),
        "adminListProjectIndexes": (
            "get",
            "/v1/admin/projects/{projectId}/indexes",
            ["admin-projects"],
        ),
        "adminMapProjectIndex": (
            "put",
            "/v1/admin/projects/{projectId}/indexes/{indexId}",
            ["admin-projects"],
        ),
        "adminUnmapProjectIndex": (
            "delete",
            "/v1/admin/projects/{projectId}/indexes/{indexId}",
            ["admin-projects"],
        ),
        "adminGetIndexOptions": ("get", "/v1/admin/index-options", ["admin-indexes"]),
        "adminListIndexes": ("get", "/v1/admin/indexes", ["admin-indexes"]),
        "adminCreateIndex": ("post", "/v1/admin/indexes", ["admin-indexes"]),
        "adminGetIndex": ("get", INDEX, ["admin-indexes"]),
        "adminUpdateIndex": ("patch", INDEX, ["admin-indexes"]),
        "adminDeleteIndex": ("delete", INDEX, ["admin-indexes"]),
        "adminStartReingest": ("post", INDEX + "/reingest", ["admin-indexes"]),
        "adminListDocuments": ("get", INDEX + "/documents", ["admin-documents"]),
        "adminGetDocument": ("get", DOCUMENT_PATH, ["admin-documents"]),
        "adminDeleteDocument": ("delete", DOCUMENT_PATH, ["admin-documents"]),
        "adminReingestDocument": ("post", DOCUMENT_PATH + "/reingest", ["admin-documents"]),
        "adminListIngestionRuns": ("get", "/v1/admin/ingestion-runs", ["admin-runs"]),
        "adminGetIngestionRun": ("get", "/v1/admin/ingestion-runs/{runId}", ["admin-runs"]),
        "adminPreflightUploads": (
            "post",
            "/v1/admin/indexes/{indexId}/uploads/preflight",
            ["admin-uploads"],
        ),
        "adminReserveUpload": ("post", "/v1/admin/indexes/{indexId}/uploads", ["admin-uploads"]),
        "adminUploadContent": (
            "put",
            "/v1/admin/indexes/{indexId}/uploads/{versionId}/content",
            ["admin-uploads"],
        ),
        "adminSubmitUploads": (
            "post",
            "/v1/admin/indexes/{indexId}/uploads/submit",
            ["admin-uploads"],
        ),
        "createChatTurn": ("post", "/v1/chat/turns", ["chat"]),
        "getCitation": ("get", CITATION, ["chat"]),
        "getCitationContent": ("get", CITATION + "/content", ["chat"]),
        "getCitationDocument": ("get", CITATION + "/document", ["chat"]),
        "adminGetDocumentPdf": ("get", DOCUMENT_PATH + "/pdf", ["admin-documents"]),
        "listSessions": ("get", "/v1/sessions", ["sessions"]),
        "getSession": ("get", SESSION, ["sessions"]),
        "listSessionTurns": ("get", SESSION + "/turns", ["sessions"]),
        "putTurnFeedback": ("put", SESSION + "/turns/{turnId}/feedback", ["sessions"]),
        "getMemory": ("get", "/v1/memory", ["memory"]),
        "updateMemorySettings": ("patch", "/v1/memory", ["memory"]),
        "updateMemoryFact": ("put", "/v1/memory/facts/{factId}", ["memory"]),
        "deleteMemoryFact": ("delete", "/v1/memory/facts/{factId}", ["memory"]),
        "clearMemory": ("delete", "/v1/memory/facts", ["memory"]),
    }


def test_t046_each_operation_lists_exactly_its_codes() -> None:
    codes = {name: op["x-docchat-error-codes"] for name, (_, _, op) in OPERATIONS.items()}

    assert codes == {
        "getHealth": [],
        "getCurrentUser": sorted([*BASE, "too_many_requests"]),
        "listMyProjects": sorted([*BASE, "too_many_requests", "validation_failed"]),
        "adminListProjects": admin_codes(PARAMS),
        "adminCreateProject": admin_codes(WRITE, BODY, ["project_name_taken"]),
        "adminGetProject": admin_codes(PARAMS, ["project_not_found"]),
        "adminUpdateProject": admin_codes(
            WRITE,
            BODY,
            PARAMS,
            PRECONDITION,
            ["project_not_found", "project_name_taken", "project_archived"],
        ),
        "adminListProjectMembers": admin_codes(PARAMS, ["project_not_found"]),
        "adminAddProjectMember": admin_codes(
            WRITE,
            PARAMS,
            PRECONDITION,
            ["project_not_found", "user_not_found", "project_archived"],
        ),
        "adminRemoveProjectMember": admin_codes(
            WRITE, PARAMS, PRECONDITION, ["project_not_found", "project_archived"]
        ),
        "adminSearchUsers": admin_codes(PARAMS, ["project_not_found"]),
        "adminListProjectIndexes": admin_codes(PARAMS, ["project_not_found"]),
        "adminMapProjectIndex": admin_codes(
            WRITE,
            BODY,
            PARAMS,
            PRECONDITION,
            [
                "project_not_found",
                "index_not_found",
                "project_archived",
                "index_not_mappable",
                "default_index_required",
            ],
        ),
        "adminUnmapProjectIndex": admin_codes(
            WRITE,
            PARAMS,
            PRECONDITION,
            ["project_not_found", "project_archived", "default_index_in_use"],
        ),
        "adminGetIndexOptions": admin_codes(),
        "adminListIndexes": admin_codes(PARAMS),
        "adminCreateIndex": admin_codes(
            WRITE, BODY, CATALOGUE, ["display_name_taken", "search_index_capacity"]
        ),
        "adminGetIndex": admin_codes(PARAMS, ["index_not_found"]),
        "adminUpdateIndex": admin_codes(
            WRITE,
            BODY,
            PRECONDITION,
            CATALOGUE,
            ["index_not_found", "display_name_taken", "index_busy", "empty_update"],
        ),
        "adminDeleteIndex": admin_codes(
            WRITE, PARAMS, PRECONDITION, ["index_not_found", "index_mapped", "index_busy"]
        ),
        "adminStartReingest": admin_codes(
            WRITE, BODY, PRECONDITION, ["index_not_found", "index_busy", "search_index_capacity"]
        ),
        "adminListDocuments": admin_codes(PARAMS, ["index_not_found"]),
        "adminGetDocument": admin_codes(PARAMS, ["index_not_found", "document_not_found"]),
        "adminDeleteDocument": admin_codes(
            WRITE, PARAMS, PRECONDITION, ["index_not_found", "document_not_found", "index_busy"]
        ),
        "adminReingestDocument": admin_codes(
            WRITE,
            BODY,
            PRECONDITION,
            ["index_not_found", "document_not_found", "no_current_version", "index_busy"],
        ),
        "adminListIngestionRuns": admin_codes(PARAMS),
        "adminGetIngestionRun": admin_codes(PARAMS, ["run_not_found"]),
        "adminPreflightUploads": sorted([*ADMIN_JSON, *UPLOAD_GATE, "batch_too_large"]),
        "adminReserveUpload": sorted(
            [
                *ADMIN_JSON,
                *UPLOAD_GATE,
                "content_unchanged",
                "duplicate_content",
                "update_not_confirmed",
                "invalid_file_name",
                "unsupported_type",
                "empty_file",
                "project_not_mapped",
            ]
        ),
        "adminUploadContent": sorted(
            [
                *BASE,
                *UPLOAD_GATE,
                "too_many_requests",
                "admin_required",
                "validation_failed",
                "version_not_found",
                "version_not_uploading",
                "content_unchanged",
                "duplicate_content",
                "content_length_required",
                "file_too_large",
                "unsupported_media_type",
                "size_mismatch",
                "file_signature_mismatch",
                "hash_mismatch",
                "upload_busy",
                "storage_unavailable",
            ]
        ),
        "adminSubmitUploads": sorted([*ADMIN_JSON, *UPLOAD_GATE, "batch_too_large"]),
        "createChatTurn": sorted(
            [
                *BASE,
                "request_too_large",
                "unsupported_media_type",
                "validation_failed",
                "project_not_found",
                "session_not_found",
                "project_index_unavailable",
                "turn_in_progress",
                "turn_already_ended",
                "client_turn_id_conflict",
                "session_turn_limit",
                "rate_limited",
                "too_many_streams",
                "token_quota_exceeded",
            ]
        ),
        "getCitation": sorted([*BASE, *BURST, *PARAMS, *CITATION_REFUSALS]),
        "getCitationContent": sorted([*BASE, *BURST, *PARAMS, *CITATION_REFUSALS, *DOWNLOADS]),
        "getCitationDocument": sorted(
            [*BASE, *BURST, *PARAMS, *CITATION_REFUSALS, *DOWNLOADS, "document_pdf_unavailable"]
        ),
        "adminGetDocumentPdf": admin_codes(
            PARAMS,
            [
                "index_not_found",
                "document_not_found",
                "document_pdf_unavailable",
                "rate_limited",
                "too_many_downloads",
                "document_pdf_missing",
            ],
        ),
        "listSessions": sorted([*BASE, *BURST, *PARAMS, "project_not_found"]),
        "getSession": sorted([*BASE, *BURST, *PARAMS, "session_not_found"]),
        "listSessionTurns": sorted([*BASE, *BURST, *PARAMS, "session_not_found"]),
        "putTurnFeedback": sorted(
            {*BASE, *BURST, *WRITE, *BODY, "turn_not_found", "feedback_not_allowed"}
        ),
        "getMemory": sorted([*BASE, *BURST]),
        "updateMemorySettings": sorted({*BASE, *BURST, *WRITE, *BODY, *PRECONDITION}),
        "updateMemoryFact": sorted(
            {*BASE, *BURST, *WRITE, *BODY, *PRECONDITION, "memory_fact_not_found"}
        ),
        "deleteMemoryFact": sorted(
            {*BASE, *BURST, *WRITE, *PARAMS, *PRECONDITION, "memory_fact_not_found"}
        ),
        "clearMemory": sorted({*BASE, *BURST, *WRITE, *PRECONDITION}),
    }


def test_every_operation_carries_the_three_extensions() -> None:
    for _, _, operation in OPERATIONS.values():
        assert {"x-docchat-access", "x-docchat-scoped-params", "x-docchat-error-codes"} <= set(
            operation
        )


def test_get_health_is_the_only_operation_without_security() -> None:
    assert OPERATIONS["getHealth"][2]["security"] == []
    for name, (_, _, operation) in OPERATIONS.items():
        if name != "getHealth":
            assert operation["security"] == [{"bearerAuth": []}]
    assert DOCUMENT["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }


def test_every_error_status_references_problem() -> None:
    for _, _, operation in OPERATIONS.values():
        statuses = {str(CODES[code].status) for code in operation["x-docchat-error-codes"]}
        documented = {status for status in operation["responses"] if status >= "400"}
        assert documented == statuses
        for status in statuses:
            content = operation["responses"][status]["content"]
            assert content == {"application/problem+json": {"schema": {"$ref": PROBLEM_REF}}}


def test_the_problem_schemas_replace_fastapis_validation_schemas() -> None:
    schemas = DOCUMENT["components"]["schemas"]

    assert {"Problem", "FieldError"} <= set(schemas)
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_the_health_answer_has_no_body() -> None:
    assert "content" not in OPERATIONS["getHealth"][2]["responses"]["200"]


def test_build_document_is_cached() -> None:
    app = create_app()

    assert app.openapi() is app.openapi()


class _Meta(BaseModel):
    type: Literal["meta"]
    session_id: str


class _Done(BaseModel):
    type: Literal["done"]


Event = Annotated[_Meta | _Done, Field(discriminator="type")]


def _streaming_document() -> dict[str, Any]:
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.post("/turns", operation_id="createThing")
    @ndjson_stream("ThingEvent", Event)
    @access("user")
    async def create() -> None:
        return

    from fastapi import FastAPI  # noqa: PLC0415 - a bare app, not the API's

    app = FastAPI()
    app.include_router(router)
    document = app.openapi()
    stream = stream_of(create)
    assert stream is not None
    return document_streams(document, {"createThing": stream})


def test_document_streams_publishes_the_union_and_the_ndjson_response() -> None:
    document = _streaming_document()
    schemas = document["components"]["schemas"]
    response = document["paths"]["/v1/turns"]["post"]["responses"]["200"]

    assert {"_Meta", "_Done"} <= set(schemas)
    assert schemas["ThingEvent"]["oneOf"] == [
        {"$ref": "#/components/schemas/_Meta"},
        {"$ref": "#/components/schemas/_Done"},
    ]
    assert schemas["ThingEvent"]["discriminator"]["propertyName"] == "type"
    assert response["content"] == {
        NDJSON_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ThingEvent"}}
    }
    assert response["x-docchat-stream"] == "ndjson-one-event-per-line"


def test_a_stream_needs_its_operation() -> None:
    with pytest.raises(KeyError, match="createChatTurn"):
        document_streams({"paths": {}}, {"createChatTurn": StreamSpec("ChatStreamEvent", Event)})


def test_problems_and_security_on_a_bare_document() -> None:
    document: dict[str, Any] = {
        "paths": {
            "/x": {
                "get": {
                    "x-docchat-access": "user",
                    "x-docchat-error-codes": ["too_many_requests"],
                    "responses": {"422": {"description": "FastAPI's"}},
                }
            }
        }
    }

    document_security(document_problems(document))
    operation = document["paths"]["/x"]["get"]

    assert set(operation["responses"]) == {"429"}
    assert operation["security"] == [{"bearerAuth": []}]


def test_render_is_deterministic() -> None:
    text = render({"b": "é", "a": [1, {"d": 2, "c": 3}]})

    assert (
        text
        == '{\n  "a": [\n    1,\n    {\n      "c": 3,\n      "d": 2\n    }\n  ],\n  "b": "é"\n}\n'
    )


def test_the_exporter_writes_the_document(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    assert main(["--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == DOCUMENT


def test_t531_two_exports_are_identical_and_the_chat_stream_is_ndjson(tmp_path: Path) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"

    assert main(["--output", str(first)]) == 0
    assert main(["--output", str(second)]) == 0

    assert first.read_bytes() == second.read_bytes()
    exported = json.loads(first.read_bytes())
    created = exported["paths"]["/v1/chat/turns"]["post"]["responses"]["200"]["content"]
    assert created == {
        NDJSON_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ChatStreamEvent"}}
    }
    assert NDJSON_MEDIA_TYPE == "application/x-ndjson"


# T-518: every model that takes input refuses unknown members.


def _models_below(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if model in seen:
        return
    seen.add(model)
    for field in model.model_fields.values():
        stack: list[object] = [field.annotation]
        while stack:
            annotation = stack.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _models_below(annotation, seen)
            stack.extend(getattr(annotation, "__args__", ()))
            stack.extend(getattr(annotation, "__metadata__", ()))


def _input_models() -> set[type[BaseModel]]:
    """The request bodies and query models of every route the document describes, with every
    model nested in them."""
    found: set[type[BaseModel]] = set()
    documented = set(OPERATIONS)
    for router in AREA_ROUTERS:
        for route in router.routes:
            if not isinstance(route, DocChatRoute) or route.operation_id not in documented:
                continue
            pending = [route.dependant]
            while pending:
                current = pending.pop()
                for param in (*current.query_params, *current.body_params):
                    annotation = param.field_info.annotation
                    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                        _models_below(annotation, found)
                pending.extend(current.dependencies)
    return found


def _settings_classes() -> set[type[BaseSettings]]:
    found: set[type[BaseSettings]] = set()
    pending: list[type[BaseSettings]] = [DocChatSettings]
    while pending:
        current = pending.pop()
        for subclass in current.__subclasses__():
            if subclass.__module__.startswith(("docchat_core.", "docchat_api.")):
                found.add(subclass)
            pending.append(subclass)
    return found


def test_t518_every_input_queue_document_and_settings_model_forbids_unknown_members() -> None:
    inputs = _input_models()
    stored: set[type[BaseModel]] = set()
    for document in DOCUMENT_MODELS:
        _models_below(document, stored)
    queued: set[type[BaseModel]] = set()
    _models_below(IngestionQueueMessage, queued)
    settings: set[type[BaseModel]] = set()
    for settings_class in _settings_classes():
        _models_below(settings_class, settings)

    # The API's settings classes, its request models and the ingestion app's are all present.
    assert {ApiSettings, AuthSettings, IngestionAppSettings, EmbeddingModel} <= settings
    assert {ChatTurnRequest, FeedbackRequest} <= inputs
    allowing = sorted(
        f"{model.__module__}.{model.__qualname__}"
        for model in inputs | stored | queued | settings
        if model.model_config.get("extra") != "forbid"
    )
    assert allowing == []
