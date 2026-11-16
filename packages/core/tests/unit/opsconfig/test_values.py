"""Unit tests of the stored value form (`80-data-model.md` sections 2.7 and 2.24)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import JsonValue

from docchat_core.opsconfig.values import StoredValueError, area_of, decode_value, encode_value


def _data_model_rows() -> dict[str, str]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    raise FileNotFoundError("80-data-model.md")


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (120, '{"value":120}'),
        (2.0, '{"value":2.0}'),
        (True, '{"value":true}'),
        ("USD", '{"value":"USD"}'),
        ({}, '{"value":{}}'),
        (
            {"m": {"outputPerMillion": "2", "inputPerMillion": "1"}},
            '{"value":{"m":{"inputPerMillion":"1","outputPerMillion":"2"}}}',
        ),
        ("é", '{"value":"é"}'),
    ],
)
def test_encode_value_is_the_canonical_form(value: JsonValue, stored: str) -> None:
    assert encode_value(value) == stored
    assert encode_value(value) == json.dumps(
        {"value": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert decode_value(stored) == value


def test_every_seeded_value_of_the_data_model_round_trips() -> None:
    rows = _data_model_rows()
    assert len(rows) == 73
    for key, stored in rows.items():
        assert encode_value(decode_value(stored)) == stored, key


def test_decoding_keeps_json_types() -> None:
    assert type(decode_value('{"value":2.0}')) is float
    assert type(decode_value('{"value":2}')) is int
    assert decode_value('{"value":false}') is False
    assert decode_value('{"value":null}') is None


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "",
        "120",
        '"value"',
        "[120]",
        "{}",
        '{"value":1,"other":2}',
        '{"Value":1}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1,"value":2}',
        '{"value":{"m":1,"m":2}}',
    ],
)
def test_decoding_refuses_any_other_text(stored: str) -> None:
    with pytest.raises(StoredValueError) as raised:
        decode_value(stored)
    # Constant messages only: a stored value is never quoted.
    assert str(raised.value) in {
        "the stored value is not JSON",
        'the stored value is not {"value": <value>}',
        "a JSON object repeats a member",
        "NaN is not a JSON value",
        "Infinity is not a JSON value",
        "-Infinity is not a JSON value",
    }


def test_area_is_the_text_before_the_first_dot() -> None:
    assert area_of("chat.context_token_budget") == "chat"
    assert area_of("a.b.c") == "a"
    assert area_of("nodot") == "nodot"
