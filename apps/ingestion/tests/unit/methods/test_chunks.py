"""Unit tests of the chunk model and the chunks work file (T-209 in part; `14-ingestion.md`
sections 6.5, 6.14 item 4, 9.1 item 5 and 10.9)."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from docchat_core.ingestion.methods import MethodContext, MethodFailedError
from docchat_ingestion.methods import chunks
from docchat_ingestion.methods.chunks import (
    Chunk,
    capped,
    check_chunk_count,
    chunk_lines,
    encode_chunks_file,
    heading_path,
)
from docchat_ingestion.methods.text import Tokenizer, count_tokens
from docchat_ingestion.workfiles import decode_chunks

type ContextFactory = Callable[..., MethodContext]


def test_t209_heading_paths_hold_no_refused_code_points() -> None:
    path = heading_path(
        [
            "Intro\u202eduction",
            "\u200b\u2066",
            " Two\u0007  words \u200e",
            *[f"H{index}" for index in range(10)],
        ]
    )

    assert path == ("Introduction", "Two words", "H0", "H1", "H2", "H3", "H4", "H5")
    assert len(heading_path(["x" * 300])[0]) == 200


def test_a_chunk_cites_a_page_from_one() -> None:
    with pytest.raises(ValueError, match="page"):
        Chunk("text", (), 0)


def test_a_chunk_over_the_cap_is_cut_keeping_its_page(
    tokenizer: Tokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chunks, "MAX_CHUNK_TOKENS", 5)
    chunk = Chunk("one two three four five", ("H",), 4)

    parts = capped(chunk, tokenizer)

    assert [part.text for part in parts] == ["one two three", " four five"]
    assert {(part.page_number, part.heading_path) for part in parts} == {(4, ("H",))}
    assert capped(Chunk("one two", (), 1), tokenizer) == [Chunk("one two", (), 1)]


@pytest.mark.parametrize(
    ("count", "code"), [(0, "no_extractable_text"), (100_001, "chunk_limit_exceeded")]
)
def test_chunk_count_limits(count: int, code: str) -> None:
    with pytest.raises(MethodFailedError) as failed:
        check_chunk_count(count)
    assert failed.value.code == code


def test_100000_chunks_pass() -> None:
    check_chunk_count(1)
    check_chunk_count(100_000)


def test_the_chunks_file_round_trips(make_context: ContextFactory, tokenizer: Tokenizer) -> None:
    ctx = make_context(title="Annual plan")
    lines = chunk_lines([Chunk("first chunk", ("A",), 1), Chunk("second", (), 3)], tokenizer)

    raw = encode_chunks_file(ctx, lines)

    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert records[0] == {
        "formatVersion": 1,
        "chunkSetId": str(ctx.chunk_set_id),
        "documentId": str(ctx.document_id),
        "documentVersionId": str(ctx.version_id),
        "chunkCount": 2,
        "encoding": "cl100k_base",
        "title": "Annual plan",
    }
    assert records[1] == {
        "ordinal": 0,
        "chunkText": "first chunk",
        "headingPath": ["A"],
        "pageNumber": 1,
        "tokenCount": count_tokens(tokenizer, "first chunk"),
    }
    assert records[2]["ordinal"] == 1
    decoded = decode_chunks(raw)
    assert decoded.header.chunk_count == 2
    assert list(decoded.chunks) == lines


def test_a_damaged_chunks_file_is_refused(
    make_context: ContextFactory, tokenizer: Tokenizer
) -> None:
    raw = encode_chunks_file(make_context(), chunk_lines([Chunk("a", (), 1)], tokenizer))
    header, line = raw.decode("utf-8").splitlines()

    with pytest.raises(ValueError, match="header"):
        decode_chunks(b"")
    with pytest.raises(ValueError, match="count"):
        decode_chunks(header.encode("utf-8"))
    with pytest.raises(ValueError, match="ordinals"):
        decode_chunks(f"{header}\n{line.replace('"ordinal":0', '"ordinal":1')}".encode())
    snake = line.replace("chunkText", "chunk_text")
    with pytest.raises(ValidationError):
        decode_chunks(f"{header}\n{snake}".encode())
