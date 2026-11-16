from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from docchat_core.models.values import (
    COUNT_MAX,
    Count,
    RowVersion,
    Sha256Hex,
    decode_row_version,
    encode_row_version,
)

_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class _Values(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: Count = 0
    sha256: Sha256Hex = _SHA256
    row_version: RowVersion = "AAAAAAAAB9E="


@pytest.mark.parametrize("count", [0, 1, COUNT_MAX])
def test_count_accepts_its_range(count: int) -> None:
    assert _Values(count=count).count == count


@pytest.mark.parametrize("count", [-1, COUNT_MAX + 1])
def test_count_refuses_outside_its_range(count: int) -> None:
    with pytest.raises(ValidationError):
        _Values(count=count)


def test_count_max_is_sql_int_max() -> None:
    assert COUNT_MAX == 2**31 - 1


def test_sha256_is_64_lower_case_hex() -> None:
    assert _Values(sha256=_SHA256).sha256 == _SHA256
    for refused in (_SHA256.upper(), _SHA256[:-1], _SHA256 + "0", "g" * 64):
        with pytest.raises(ValidationError):
            _Values(sha256=refused)


@pytest.mark.parametrize("text", ["AAAAAAAAB9E=", "abc+/xyz012=", "ZZZZZZZZZZZ="])
def test_row_version_accepts_the_12_character_form(text: str) -> None:
    assert _Values(row_version=text).row_version == text


@pytest.mark.parametrize("text", ["AAAAAAAAB9E", "AAAAAAAAB9E==", "AAAAAAAAB9-=", '"AAAAAAAAB9E="'])
def test_row_version_refuses_other_shapes(text: str) -> None:
    with pytest.raises(ValidationError):
        _Values(row_version=text)


def test_row_version_round_trip() -> None:
    value = bytes([0, 0, 0, 0, 0, 0, 7, 209])

    text = encode_row_version(value)

    assert text == "AAAAAAAAB9E="
    assert decode_row_version(text) == value


def test_encode_row_version_needs_eight_bytes() -> None:
    with pytest.raises(ValueError, match="8 bytes"):
        encode_row_version(b"\x00" * 7)


@pytest.mark.parametrize("text", ["*", 'W/"AAAAAAAAB9E="', "AAAAAAAAB9E", "AAAAAAAAB9E=,x"])
def test_decode_row_version_refuses_other_shapes(text: str) -> None:
    with pytest.raises(ValueError, match="not a row version"):
        decode_row_version(text)
