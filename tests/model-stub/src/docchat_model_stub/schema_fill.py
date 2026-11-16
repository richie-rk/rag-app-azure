"""One JSON value that satisfies a structured-output schema (84-testing.md section 2.3).

Rules: every required property present; strings `"stub-<property>"` cut or padded to `maxLength`
and `minLength`; numbers at `minimum` (else 0); booleans `false`; arrays with `max(minItems, 1)`
items where `maxItems` allows; an enum takes its first value; `const` is honoured.
"""

from __future__ import annotations

import math
from typing import Any

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type Schema = dict[str, Any]

_PAD = "x"
_ROOT_NAME = "value"


class SchemaError(ValueError):
    """A schema the filler can't follow, such as a `$ref` that names no definition."""


def _resolve(schema: Schema, root: Schema) -> Schema:
    seen: set[str] = set()
    while isinstance(ref := schema.get("$ref"), str):
        if ref in seen or not ref.startswith("#/"):
            raise SchemaError(f"unresolvable $ref {ref}")
        seen.add(ref)
        target: Any = root
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or key not in target:
                raise SchemaError(f"unresolvable $ref {ref}")
            target = target[key]
        if not isinstance(target, dict):
            raise SchemaError(f"unresolvable $ref {ref}")
        schema = target
    return schema


def _first_option(schema: Schema) -> Schema | None:
    for keyword in ("anyOf", "oneOf", "allOf"):
        options = schema.get(keyword)
        if isinstance(options, list) and options and isinstance(options[0], dict):
            first: Schema = options[0]
            return first
    return None


def _schema_type(schema: Schema) -> str | None:
    declared = schema.get("type")
    if isinstance(declared, list):
        # A nullable field lists its types; the first one listed is the one filled.
        return str(declared[0]) if declared else None
    if isinstance(declared, str):
        return declared
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _string(schema: Schema, name: str) -> str:
    text = f"stub-{name}"
    max_length = schema.get("maxLength")
    if isinstance(max_length, int):
        text = text[:max_length]
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(text) < min_length:
        text += _PAD * (min_length - len(text))
    return text


def _number(schema: Schema, *, integer: bool) -> int | float:
    value: float
    if isinstance(schema.get("minimum"), int | float):
        value = schema["minimum"]
    elif isinstance(schema.get("exclusiveMinimum"), int | float):
        value = schema["exclusiveMinimum"] + 1
    else:
        value = 0
        if isinstance(schema.get("maximum"), int | float):
            value = min(value, schema["maximum"])
    return math.ceil(value) if integer else value


def _array(schema: Schema, root: Schema, name: str) -> list[JsonValue]:
    min_items = schema.get("minItems")
    count = max(min_items, 1) if isinstance(min_items, int) else 1
    max_items = schema.get("maxItems")
    if isinstance(max_items, int):
        count = min(count, max_items)
    items = schema.get("items")
    item_schema: Schema = items if isinstance(items, dict) else {}
    return [_fill(item_schema, root, name) for _ in range(count)]


def _object(schema: Schema, root: Schema) -> dict[str, JsonValue]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = schema.get("required", [])
    return {
        key: _fill(sub_schema if isinstance(sub_schema, dict) else {}, root, key)
        for key, sub_schema in properties.items()
        if key in required
    }


def _by_type(schema: Schema, root: Schema, name: str) -> JsonValue:
    value: JsonValue
    match _schema_type(schema):
        case "object":
            value = _object(schema, root)
        case "array":
            value = _array(schema, root, name)
        case "string":
            value = _string(schema, name)
        case "integer":
            value = _number(schema, integer=True)
        case "number":
            value = _number(schema, integer=False)
        case "boolean":
            value = False
        case _:
            value = None
    return value


def _fill(schema: Schema, root: Schema, name: str) -> JsonValue:
    schema = _resolve(schema, root)
    if "const" in schema:
        const: JsonValue = schema["const"]
        return const
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        first: JsonValue = enum[0]
        return first
    option = _first_option(schema)
    if option is not None and "type" not in schema:
        return _fill(option, root, name)
    return _by_type(schema, root, name)


def fill(schema: Schema) -> JsonValue:
    """Build the value for `schema`, resolving local `$ref`s against the schema itself."""
    return _fill(schema, schema, _ROOT_NAME)
