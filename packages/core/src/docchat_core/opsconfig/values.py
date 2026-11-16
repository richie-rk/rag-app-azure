"""The stored form of an ops-config value (`80-data-model.md` sections 2.7 and 2.24).

`value_json` and `default_json` each hold `{"value": <typed value>}`, so `ISJSON` stays meaningful
for scalars. `encode_value` writes the one canonical form the migrations and `docchat-ops config
set` and `config reset` store, so a text comparison of two stored values is a value comparison.
`decode_value` reads a stored value back as typed JSON data; nothing is ever evaluated.
"""

from __future__ import annotations

import json
from typing import Final, NoReturn

from pydantic import JsonValue

VALUE_MEMBER: Final = "value"


class StoredValueError(ValueError):
    """A stored text that isn't exactly `{"value": <JSON value>}`. The message never quotes it."""


def encode_value(value: JsonValue) -> str:
    """The canonical JSON of an ops-config value `v`:
    `json.dumps({"value": v}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`."""
    return json.dumps(
        {VALUE_MEMBER: value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _refuse_constant(name: str) -> NoReturn:
    # `NaN` and `Infinity` aren't JSON, though Python's parser accepts them.
    raise StoredValueError(f"{name} is not a JSON value")


def _unique_members(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    # A duplicated member would make two different texts mean one value, or one text two.
    members = dict(pairs)
    if len(members) != len(pairs):
        raise StoredValueError("a JSON object repeats a member")
    return members


def decode_value(value_json: str) -> JsonValue:
    """The typed value of a stored `{"value": <value>}`.

    Raises `StoredValueError` for text that isn't JSON, a document of any other shape, a repeated
    member or a non-finite number.
    """
    try:
        document = json.loads(
            value_json, parse_constant=_refuse_constant, object_pairs_hook=_unique_members
        )
    except json.JSONDecodeError:
        raise StoredValueError("the stored value is not JSON") from None
    if not isinstance(document, dict) or set(document) != {VALUE_MEMBER}:
        raise StoredValueError('the stored value is not {"value": <value>}')
    value: JsonValue = document[VALUE_MEMBER]
    return value


def area_of(key: str) -> str:
    """The area of a dotted key: the text before its first dot (`CK_ops_config_key` guarantees a
    stored key has one)."""
    return key.partition(".")[0]
