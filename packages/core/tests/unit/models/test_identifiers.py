from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from docchat_core.models.identifiers import (
    UUID_PATTERN,
    CanonicalUuid,
    CorrelationId,
    LowerCaseUuid,
    parse_uuid,
)

_LOWER = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"


class _Ids(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: CanonicalUuid


class _Settingish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: LowerCaseUuid


class _Trace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: CorrelationId


@pytest.mark.parametrize("text", [_LOWER, _LOWER.upper(), "3F2C0D9E-8b7a-41C6-a5d4-E3F2C1B0A998"])
def test_canonical_uuid_accepts_either_case_and_lower_cases(text: str) -> None:
    model = _Ids(entity_id=text)  # type: ignore[arg-type]

    assert model.entity_id == UUID(_LOWER)
    assert model.model_dump(mode="json") == {"entity_id": _LOWER}


@pytest.mark.parametrize(
    "text",
    [
        "{" + _LOWER + "}",
        "urn:uuid:" + _LOWER,
        _LOWER.replace("-", ""),
        _LOWER + "\n",
        " " + _LOWER,
        _LOWER[:-1],
        _LOWER[:-1] + "g",
        "",
    ],
)
def test_canonical_uuid_refuses_other_spellings_as_uuid_parsing(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _Ids(entity_id=text)  # type: ignore[arg-type]

    assert [error["type"] for error in caught.value.errors()] == ["uuid_parsing"]


def test_canonical_uuid_refuses_other_types() -> None:
    with pytest.raises(ValidationError):
        _Ids(entity_id=12)  # type: ignore[arg-type]


def test_canonical_uuid_accepts_a_uuid_instance() -> None:
    value = UUID(_LOWER)

    assert _Ids(entity_id=value).entity_id is value


def test_canonical_uuid_json_schema_states_the_pattern() -> None:
    schema = _Ids.model_json_schema()["properties"]["entity_id"]

    assert schema["pattern"] == UUID_PATTERN
    assert schema["format"] == "uuid"


def test_lower_case_uuid_refuses_upper_case() -> None:
    assert _Settingish(client_id=_LOWER).client_id == UUID(_LOWER)  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as caught:
        _Settingish(client_id=_LOWER.upper())  # type: ignore[arg-type]

    assert caught.value.errors()[0]["type"] == "uuid_parsing"


def test_lower_case_uuid_accepts_a_uuid_instance_and_refuses_braces() -> None:
    assert _Settingish(client_id=UUID(_LOWER)).client_id == UUID(_LOWER)
    with pytest.raises(ValidationError):
        _Settingish(client_id="{" + _LOWER + "}")  # type: ignore[arg-type]


def test_parse_uuid() -> None:
    assert parse_uuid(_LOWER.upper()) == UUID(_LOWER)
    with pytest.raises(ValueError, match="canonical"):
        parse_uuid(_LOWER.replace("-", ""))


def test_correlation_id_accepts_a_trace_id() -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    assert _Trace(correlation_id=trace_id).correlation_id == trace_id


@pytest.mark.parametrize(
    "text",
    ["4BF92F3577B34DA6A3CE929D0E0E4736", "4bf92f3577b34da6a3ce929d0e0e473", "0" * 32, "x" * 32],
)
def test_correlation_id_refuses_other_shapes_and_the_all_zero_id(text: str) -> None:
    with pytest.raises(ValidationError):
        _Trace(correlation_id=text)
