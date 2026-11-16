"""The page-wise chunker (`14-ingestion.md` sections 10.5 to 10.7 and 10.9; ADR 0047 items 6
and 7).

One chunk per page in page order: the page's elements in reading order joined with blank lines,
every element kept, each table as its summary, a blank line and its sanitised HTML (the HTML
alone without a summary). The heading path is the page's titles. A page above 8,000 tokens splits
into ordered sub-chunks that keep its page and heading path: at element boundaries first; one
element above the cap at sentence boundaries, then at token boundaries; one table above the cap
between rows, each part repeating the header rows, its summary staying with the first part.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from docchat_ingestion.isolation.results import ExtractElement, ExtractPartV1
from docchat_ingestion.methods.chunks import MAX_CHUNK_TOKENS, Chunk, capped, heading_path
from docchat_ingestion.methods.tables import parse_table, split_table, table_sha256
from docchat_ingestion.methods.text import (
    Piece,
    Tokenizer,
    count_tokens,
    joined,
    pack,
    sentences,
    token_slices,
)

ELEMENT_JOINER: Final = "\n\n"
SENTENCE_JOINER: Final = " "


def element_text(element: ExtractElement, summaries: Mapping[str, str]) -> str:
    """An element as the chunk writes it: a table as its summary, a blank line and its HTML."""
    if element.kind == "table" and element.table_html is not None:
        summary = summaries.get(table_sha256(element.table_html))
        if summary:
            return f"{summary}{ELEMENT_JOINER}{element.table_html}"
        return element.table_html
    return element.text


def _split_table_element(
    element: ExtractElement, summaries: Mapping[str, str], tokenizer: Tokenizer
) -> list[str] | None:
    """The parts of one table above the cap, or `None` when it has no body rows."""
    html = element.table_html
    structure = parse_table(html) if html is not None else None
    if html is None or structure is None or not structure.body:
        return None
    summary = summaries.get(table_sha256(html))
    prefix = f"{summary}{ELEMENT_JOINER}" if summary else ""
    first_limit = MAX_CHUNK_TOKENS - count_tokens(tokenizer, prefix)
    parts = split_table(structure, MAX_CHUNK_TOKENS, tokenizer, first_limit=max(first_limit, 1))
    return [prefix + part.html if index == 0 else part.html for index, part in enumerate(parts)]


def _split_text(text: str, tokenizer: Tokenizer) -> list[str]:
    """One element above the cap: sub-chunks of whole sentences up to the cap, a sentence above
    the cap in decoded slices of 8,000 tokens."""
    pieces: list[Piece] = []
    for _, sentence in sentences(text):
        size = count_tokens(tokenizer, sentence)
        if size <= MAX_CHUNK_TOKENS:
            pieces.append(Piece(sentence, size, SENTENCE_JOINER))
        else:
            pieces.extend(
                Piece(part, count_tokens(tokenizer, part), "")
                for part in token_slices(tokenizer, sentence, MAX_CHUNK_TOKENS)
            )
    return [joined(group) for group in pack(pieces, MAX_CHUNK_TOKENS, tokenizer)]


def _split_element(
    element: ExtractElement, text: str, summaries: Mapping[str, str], tokenizer: Tokenizer
) -> list[str]:
    if element.kind == "table":
        parts = _split_table_element(element, summaries, tokenizer)
        # A table without rows to split between has no sentences either.
        return parts if parts is not None else token_slices(tokenizer, text, MAX_CHUNK_TOKENS)
    return _split_text(text, tokenizer)


def page_texts(
    elements: Sequence[ExtractElement], summaries: Mapping[str, str], tokenizer: Tokenizer
) -> list[str]:
    """The texts of one page's chunk or sub-chunks (section 10.7)."""
    texts = [element_text(element, summaries) for element in elements]
    whole = ELEMENT_JOINER.join(texts)
    if count_tokens(tokenizer, whole) <= MAX_CHUNK_TOKENS:
        return [whole]
    chunks: list[str] = []
    run: list[Piece] = []

    def flush() -> None:
        chunks.extend(joined(group) for group in pack(run, MAX_CHUNK_TOKENS, tokenizer))
        run.clear()

    for element, text in zip(elements, texts, strict=True):
        size = count_tokens(tokenizer, text)
        if size > MAX_CHUNK_TOKENS:
            flush()
            chunks.extend(_split_element(element, text, summaries, tokenizer))
        else:
            run.append(Piece(text, size, ELEMENT_JOINER))
    flush()
    return chunks


def chunk_pages(
    parts: Sequence[ExtractPartV1], summaries: Mapping[str, str], tokenizer: Tokenizer
) -> list[Chunk]:
    """Every page's chunks in page order; a page with no element gives none."""
    chunks: list[Chunk] = []
    pages = sorted(
        (page for part in parts for page in part.pages), key=lambda page: page.page_number
    )
    for page in pages:
        if not page.elements:
            continue
        path = heading_path(element.text for element in page.elements if element.kind == "title")
        for text in page_texts(page.elements, summaries, tokenizer):
            chunks.extend(capped(Chunk(text, path, page.page_number), tokenizer))
    return chunks
