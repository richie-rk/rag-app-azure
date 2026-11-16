from __future__ import annotations

import array
import base64
import math

import pytest
from pydantic import ValidationError

from docchat_model_stub.embeddings import (
    DEFAULT_DIMENSIONS,
    EmbeddingRequest,
    estimated_tokens,
    response_body,
    vector,
)


def _norm(values: list[float]) -> float:
    return math.sqrt(math.fsum(value * value for value in values))


def test_vectors_are_unit_length_and_repeatable() -> None:
    first = vector("the same text", 1024)
    assert len(first) == 1024
    assert math.isclose(_norm(first), 1.0, rel_tol=1e-12)
    assert vector("the same text", 1024) == first
    assert vector("another text", 1024) != first


def test_one_dimension() -> None:
    (only,) = vector("x", 1)
    assert abs(only) == pytest.approx(1.0)


def test_request_defaults() -> None:
    request = EmbeddingRequest.model_validate({"model": "embed", "input": "a"})
    assert request.dimensions == DEFAULT_DIMENSIONS
    assert request.encoding_format == "float"
    assert request.texts() == ["a"]


def test_unknown_parameters_are_ignored() -> None:
    request = EmbeddingRequest.model_validate({"model": "embed", "input": ["a"], "user": "u"})
    assert request.texts() == ["a"]


@pytest.mark.parametrize(
    "body",
    [
        {"model": "embed", "input": "a", "dimensions": 0},
        {"model": "embed", "input": "a", "dimensions": 3073},
        {"model": "embed", "input": "a", "dimensions": "1024"},
        {"model": "embed", "input": []},
        {"model": "embed", "input": ["a"] * 2049},
        {"model": "embed", "input": [1, 2, 3]},
        {"model": "embed", "input": [[1, 2]]},
        {"model": "embed", "input": "a", "encoding_format": "int8"},
        {"input": "a"},
    ],
)
def test_invalid_requests_are_refused(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EmbeddingRequest.model_validate(body)


def test_the_largest_batch_is_accepted() -> None:
    request = EmbeddingRequest.model_validate(
        {"model": "embed", "input": ["a"] * 2048, "dimensions": 3072}
    )
    assert len(request.texts()) == 2048


def test_response_body_floats() -> None:
    request = EmbeddingRequest.model_validate(
        {"model": "embed-query", "input": ["abcde", "abcde"], "dimensions": 8}
    )
    body = response_body(request)
    assert body["object"] == "list"
    assert body["model"] == "embed-query"
    assert body["usage"] == {"prompt_tokens": 3, "total_tokens": 3}
    assert [item["index"] for item in body["data"]] == [0, 1]
    assert body["data"][0]["embedding"] == vector("abcde", 8)
    assert body["data"][0]["embedding"] == body["data"][1]["embedding"]


def test_response_body_base64_holds_little_endian_floats() -> None:
    request = EmbeddingRequest.model_validate(
        {"model": "embed", "input": "abc", "dimensions": 4, "encoding_format": "base64"}
    )
    encoded = response_body(request)["data"][0]["embedding"]
    decoded = array.array("f", base64.b64decode(encoded)).tolist()
    assert decoded == pytest.approx(vector("abc", 4), rel=1e-6)


def test_token_estimate() -> None:
    assert estimated_tokens(["abcd"]) == 1
    assert estimated_tokens(["abcd", "e"]) == 2
    assert estimated_tokens([""]) == 0
