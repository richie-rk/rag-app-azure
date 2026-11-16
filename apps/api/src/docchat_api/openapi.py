"""The OpenAPI document: its builder, `document_streams()` and the exporter
(`11-api.md` section 1.13; `01-repository.md` section 7; ADR 0013).

`create_app` installs `build_document` as `app.openapi`, so the document the authorisation matrix
reads and the one the exporter writes are the same. FastAPI generates the operations; the builder
then adds what FastAPI can't know: the `Problem` and `FieldError` schemas and, for every error
status an operation's `x-docchat-error-codes` can return, a response with
`application/problem+json` referencing `Problem` (in place of FastAPI's own `422` schema); the
security requirement `bearerAuth` (HTTP bearer, format `JWT`) on every operation but `getHealth`,
which carries `security: []`; and the streams.

FastAPI's own JSON Lines path emits an OpenAPI 3.2 `itemSchema`, which the generators type as
`unknown`, so a streaming route returns a `StreamingResponse` and declares its event union with
`ndjson_stream`; `document_streams` puts every member into `components.schemas`, adds a schema with
`oneOf` over them discriminated by `type`, and makes the operation's `200` response
`application/x-ndjson` with `x-docchat-stream: "ndjson-one-event-per-line"`.

`uv run --package docchat-api python -m docchat_api.openapi --output apps/api/openapi.json`
writes the document with sorted keys, two-space indentation, `ensure_ascii=False` and a trailing
newline, so an unchanged API regenerates identical bytes.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import TypeAdapter

from docchat_api.http.access import ACCESS_EXTENSION, ERROR_CODES_EXTENSION
from docchat_api.http.problems import PROBLEM_MEDIA_TYPE, Problem
from docchat_core.errors import CODES

OPENAPI_VERSION: Final = "3.1.0"
REF_TEMPLATE: Final = "#/components/schemas/{model}"
PROBLEM_REF: Final = "#/components/schemas/Problem"
SECURITY_SCHEME: Final = "bearerAuth"
NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"
STREAM_EXTENSION: Final = "x-docchat-stream"
STREAM_FORMAT: Final = "ndjson-one-event-per-line"
STREAM_ATTRIBUTE: Final = "__docchat_stream__"
STREAMS_STATE: Final = "docchat_streams"
# FastAPI's generic validation schemas, replaced by `Problem`.
_FASTAPI_VALIDATION_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")
_HTTP_METHODS: Final = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """A streaming operation's event union and the schema name it is published under."""

    name: str
    events: Any


def ndjson_stream(name: str, events: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare that an endpoint streams `events` (a discriminated union of event models) as
    NDJSON; the union is published as the schema `name`. Put it below the router decorator."""
    spec = StreamSpec(name, events)

    def mark[F: Callable[..., Any]](endpoint: F) -> F:
        setattr(endpoint, STREAM_ATTRIBUTE, spec)
        return endpoint

    return mark


def stream_of(endpoint: Callable[..., Any]) -> StreamSpec | None:
    spec = getattr(endpoint, STREAM_ATTRIBUTE, None)
    return spec if isinstance(spec, StreamSpec) else None


def _operations(document: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for path_item in document.get("paths", {}).values():
        for method, operation in path_item.items():
            if method in _HTTP_METHODS:
                yield operation


def _schemas(document: dict[str, Any]) -> dict[str, Any]:
    components: dict[str, Any] = document.setdefault("components", {})
    schemas: dict[str, Any] = components.setdefault("schemas", {})
    return schemas


def _add_schema(document: dict[str, Any], schema: dict[str, Any], name: str) -> None:
    schemas = _schemas(document)
    for definition_name, definition in schema.pop("$defs", {}).items():
        schemas[definition_name] = definition
    schemas[name] = schema


def document_problems(document: dict[str, Any]) -> dict[str, Any]:
    """`Problem` and `FieldError` in `components.schemas`, and every error status of each
    operation's codes as an `application/problem+json` response referencing `Problem`."""
    _add_schema(
        document,
        Problem.model_json_schema(mode="serialization", ref_template=REF_TEMPLATE),
        "Problem",
    )
    for operation in _operations(document):
        codes: Sequence[str] = operation.get(ERROR_CODES_EXTENSION, ())
        statuses = sorted({status for code in codes if (status := CODES[code].status) is not None})
        responses: dict[str, Any] = operation.setdefault("responses", {})
        responses.pop("422", None)
        for status in statuses:
            responses[str(status)] = {
                "description": HTTPStatus(status).phrase,
                "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_REF}}},
            }
    schemas = _schemas(document)
    for name in _FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(name, None)
    return document


def document_security(document: dict[str, Any]) -> dict[str, Any]:
    """The `bearerAuth` scheme, required by every operation that isn't public."""
    components: dict[str, Any] = document.setdefault("components", {})
    components["securitySchemes"] = {
        SECURITY_SCHEME: {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    for operation in _operations(document):
        if operation.get(ACCESS_EXTENSION) == "public":
            operation["security"] = []
        else:
            operation["security"] = [{SECURITY_SCHEME: []}]
    return document


def document_streams(document: dict[str, Any], streams: Mapping[str, StreamSpec]) -> dict[str, Any]:
    """For each operationId in `streams`: every member of its event union in
    `components.schemas` (from `TypeAdapter(...).json_schema(mode="serialization")`), the union
    as a `oneOf` schema discriminated by `type`, and the `200` response as NDJSON of it."""
    by_id = {operation.get("operationId"): operation for operation in _operations(document)}
    for operation_id, spec in streams.items():
        operation = by_id.get(operation_id)
        if operation is None:
            raise KeyError(f"no operation {operation_id!r} to stream")
        schema = TypeAdapter(spec.events).json_schema(
            mode="serialization", ref_template=REF_TEMPLATE
        )
        schemas = _schemas(document)
        schemas.update(schema.get("$defs", {}))
        members = schema.get("oneOf") or schema.get("anyOf") or []
        union: dict[str, Any] = {
            "oneOf": [{"$ref": member["$ref"]} for member in members],
            "discriminator": {"propertyName": "type"},
        }
        mapping = schema.get("discriminator", {}).get("mapping")
        if mapping:
            union["discriminator"]["mapping"] = mapping
        schemas[spec.name] = union
        responses: dict[str, Any] = operation.setdefault("responses", {})
        description = responses.get("200", {}).get("description", HTTPStatus.OK.phrase)
        responses["200"] = {
            "description": description,
            "content": {
                NDJSON_MEDIA_TYPE: {"schema": {"$ref": f"#/components/schemas/{spec.name}"}}
            },
            STREAM_EXTENSION: STREAM_FORMAT,
        }
    return document


def build_document(app: FastAPI) -> dict[str, Any]:
    """The app's OpenAPI 3.1 document, built once and cached on the app."""
    if app.openapi_schema is not None:
        return app.openapi_schema
    document = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=OPENAPI_VERSION,
        routes=app.routes,
        separate_input_output_schemas=False,
    )
    document_problems(document)
    document_security(document)
    document_streams(document, getattr(app.state, STREAMS_STATE, {}))
    app.openapi_schema = document
    return document


def render(document: Mapping[str, Any]) -> str:
    """The committed bytes: sorted keys, two-space indentation, non-ASCII kept, a final newline."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m docchat_api.openapi", description="Write the API's OpenAPI document."
    )
    parser.add_argument("--output", type=Path, required=True, help="the file to write")
    arguments = parser.parse_args(argv)
    # Imported here: the app module installs this module's builder, so a top-level import would
    # be circular.
    from docchat_api.app import create_app  # noqa: PLC0415

    output: Path = arguments.output
    output.write_text(render(create_app().openapi()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
