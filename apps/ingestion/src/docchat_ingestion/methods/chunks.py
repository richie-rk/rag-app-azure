"""The chunks both methods produce and the chunks work file Chunk writes (`14-ingestion.md`
sections 6.5, 6.14 item 4, 8.13, 9.1 item 5, 10.9 and 11.6).

Every chunk carries the page of the version's PDF where its own text starts and its heading path,
each entry cleaned by `display_text`; ordinals run from 0 in document order. The file's format is
`docchat_ingestion.workfiles`, which Embed and Index read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from docchat_core.ingestion.methods import MethodContext, MethodFailedError
from docchat_core.ingestion.names import HEADING_MAX_LENGTH, display_text
from docchat_core.search.schema import MAX_HEADINGS
from docchat_ingestion.methods.text import Tokenizer, count_tokens, token_slices
from docchat_ingestion.workfiles import MAX_CHUNKS, ChunkLine, ChunksHeader, encode_chunks

MAX_CHUNK_TOKENS: Final = 8000
"""192 tokens below the embedding model's 8,192-token input limit."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk before its ordinal is assigned: the text that is embedded and indexed, the
    cleaned heading path and the 1-based page where its own text starts."""

    text: str
    heading_path: tuple[str, ...]
    page_number: int

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("a chunk cites a page from 1")


def heading_path(headings: Iterable[str]) -> tuple[str, ...]:
    """Each heading cleaned and cut to 200 code points by `display_text`, an entry empty after
    cleaning left out, at most 8 entries, the outermost first."""
    cleaned = (display_text(heading, HEADING_MAX_LENGTH) for heading in headings)
    return tuple(entry for entry in cleaned if entry)[:MAX_HEADINGS]


def capped(chunk: Chunk, tokenizer: Tokenizer) -> list[Chunk]:
    """The chunk, or its decoded slices of 8,000 tokens when it is longer, each keeping the
    page and the heading path; no chunk ever exceeds the embedding input cap."""
    if count_tokens(tokenizer, chunk.text) <= MAX_CHUNK_TOKENS:
        return [chunk]
    return [
        Chunk(text, chunk.heading_path, chunk.page_number)
        for text in token_slices(tokenizer, chunk.text, MAX_CHUNK_TOKENS)
    ]


def check_chunk_count(count: int) -> None:
    """Zero chunks fail the item with `no_extractable_text`, more than 100,000 with
    `chunk_limit_exceeded`."""
    if count == 0:
        raise MethodFailedError("no_extractable_text")
    if count > MAX_CHUNKS:
        raise MethodFailedError("chunk_limit_exceeded")


def chunk_lines(chunks: Sequence[Chunk], tokenizer: Tokenizer) -> list[ChunkLine]:
    """The chunks with their ordinals and their token counts in the index's encoding."""
    return [
        ChunkLine(
            ordinal=ordinal,
            chunk_text=chunk.text,
            heading_path=list(chunk.heading_path),
            page_number=chunk.page_number,
            token_count=count_tokens(tokenizer, chunk.text),
        )
        for ordinal, chunk in enumerate(chunks)
    ]


def encode_chunks_file(ctx: MethodContext, lines: Sequence[ChunkLine]) -> bytes:
    """`chunks/{chunkSetId}.jsonl`: the header, then one line per chunk, UTF-8 JSON Lines."""
    header = ChunksHeader(
        chunk_set_id=ctx.chunk_set_id,
        document_id=ctx.document_id,
        document_version_id=ctx.version_id,
        chunk_count=len(lines),
        encoding=ctx.encoding,
        title=ctx.title,
    )
    return encode_chunks(header, lines)
