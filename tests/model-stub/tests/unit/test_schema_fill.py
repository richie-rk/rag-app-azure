from __future__ import annotations

import pytest

from docchat_model_stub.schema_fill import SchemaError, fill


def _object(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def test_the_pipeline_schemas() -> None:
    standalone = _object({"query": {"type": "string"}})
    followups = _object({"questions": {"type": "array", "items": {"type": "string"}}})
    assert fill(standalone) == {"query": "stub-query"}
    assert fill(followups) == {"questions": ["stub-questions"]}


def test_the_memory_operations_schema() -> None:
    operation = _object(
        {
            "action": {"type": "string", "enum": ["add", "replace"]},
            "targetFactId": {"type": ["string", "null"]},
            "category": {"type": "string", "enum": ["role", "team"]},
            "text": {"type": "string"},
            "addressesAssistant": {"type": "boolean"},
        }
    )
    schema = _object({"operations": {"type": "array", "items": operation}})
    assert fill(schema) == {
        "operations": [
            {
                "action": "add",
                "targetFactId": "stub-targetFactId",
                "category": "role",
                "text": "stub-text",
                "addressesAssistant": False,
            }
        ]
    }


def test_only_required_properties_are_filled() -> None:
    schema = _object({"a": {"type": "string"}, "b": {"type": "string"}}, required=["b"])
    assert fill(schema) == {"b": "stub-b"}


def test_string_lengths() -> None:
    schema = _object(
        {
            "short": {"type": "string", "maxLength": 3},
            "long": {"type": "string", "minLength": 12},
        }
    )
    assert fill(schema) == {"short": "stu", "long": "stub-longxxx"}


def test_numbers() -> None:
    schema = _object(
        {
            "count": {"type": "integer", "minimum": 3},
            "ratio": {"type": "number", "minimum": 0.5},
            "free": {"type": "integer"},
            "above": {"type": "integer", "exclusiveMinimum": 4},
            "negative": {"type": "number", "maximum": -2},
        }
    )
    assert fill(schema) == {"count": 3, "ratio": 0.5, "free": 0, "above": 5, "negative": -2}


def test_arrays() -> None:
    schema = _object(
        {
            "many": {"type": "array", "items": {"type": "integer"}, "minItems": 3},
            "none": {"type": "array", "items": {"type": "integer"}, "maxItems": 0},
            "untyped": {"items": {"type": "boolean"}},
        }
    )
    assert fill(schema) == {"many": [0, 0, 0], "none": [], "untyped": [False]}


def test_const_enum_null_and_unknown_types() -> None:
    schema = _object(
        {
            "fixed": {"const": "v1"},
            "choice": {"enum": [2, 3]},
            "nothing": {"type": "null"},
            "anything": {},
        }
    )
    assert fill(schema) == {"fixed": "v1", "choice": 2, "nothing": None, "anything": None}


def test_unions_take_their_first_option() -> None:
    schema = _object(
        {
            "any": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "one": {"oneOf": [{"type": "string"}]},
            "all": {"allOf": [{"type": "boolean"}]},
        }
    )
    assert fill(schema) == {"any": 0, "one": "stub-one", "all": False}


def test_object_without_properties() -> None:
    assert fill({"type": "object"}) == {}
    assert fill(_object({"inner": {"properties": {}}})) == {"inner": {}}


def test_local_references_resolve() -> None:
    schema = {
        "$defs": {"Item": _object({"name": {"type": "string"}})},
        **_object({"item": {"$ref": "#/$defs/Item"}}),
    }
    assert fill(schema) == {"item": {"name": "stub-name"}}


@pytest.mark.parametrize(
    "ref", ["#/$defs/Missing", "other.json#/x", "#/$defs/Loop", "#/$defs/NotObject"]
)
def test_unresolvable_references_are_refused(ref: str) -> None:
    schema = {
        "$defs": {"Loop": {"$ref": "#/$defs/Loop"}, "NotObject": 3},
        **_object({"item": {"$ref": ref}}),
    }
    with pytest.raises(SchemaError):
        fill(schema)


def test_a_root_string() -> None:
    assert fill({"type": "string"}) == "stub-value"
