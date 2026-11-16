"""Token counting, sentence boundaries and greedy packing, shared by both chunkers
(`14-ingestion.md` sections 10.7, 11.4 and 12.4).

Every limit of both methods is in tokens of a named `tiktoken` encoding: the physical index's
encoding for chunks, `DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING` for the table-summary call. The
encodings load from the image's `TIKTOKEN_CACHE_DIR`; loading one reads a file, so callers load
it in a worker thread, never on the event loop.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from docchat_core.ingestion.settings import TokenEncoding

SENTENCE_BOUNDARY: Final = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")
"""The sentence boundary of both methods: whitespace after `.`, `!` or `?`, or after the
ideographic full stop or the fullwidth exclamation or question mark (U+3002, U+FF01, U+FF1F,
written as escapes so no look-alike character sits in the source)."""


class Tokenizer(Protocol):
    """The two operations the chunkers need of an encoding."""

    def encode(self, text: str) -> list[int]: ...

    def decode(self, tokens: Sequence[int]) -> str: ...


type TokenizerSource = Callable[[TokenEncoding], Tokenizer]
"""Gives the tokenizer of an encoding; `load_tokenizer` in production."""


class TiktokenTokenizer:
    """A `tiktoken` encoding that treats special-token text as ordinary text: a document that
    contains `<|endoftext|>` is counted, never refused."""

    def __init__(self, encoding: TokenEncoding) -> None:
        # Imported here: the parsing child imports this package's table code and never counts.
        import tiktoken  # noqa: PLC0415

        self._encoding = tiktoken.get_encoding(encoding.value)

    def encode(self, text: str) -> list[int]:
        return self._encoding.encode(text, disallowed_special=())

    def decode(self, tokens: Sequence[int]) -> str:
        return self._encoding.decode(list(tokens))


@functools.cache
def load_tokenizer(encoding: TokenEncoding) -> Tokenizer:
    """The process's one tokenizer per encoding, loaded from the image's cache on first use."""
    return TiktokenTokenizer(encoding)


def count_tokens(tokenizer: Tokenizer, text: str) -> int:
    return len(tokenizer.encode(text)) if text else 0


def token_slices(tokenizer: Tokenizer, text: str, size: int) -> list[str]:
    """`text` cut into decoded slices of `size` tokens, the last one shorter; the method's last
    resort for one sentence or one table row above a limit."""
    if size < 1:
        raise ValueError("a slice holds at least one token")
    tokens = tokenizer.encode(text)
    return [tokenizer.decode(tokens[start : start + size]) for start in range(0, len(tokens), size)]


def last_tokens(tokenizer: Tokenizer, text: str, size: int) -> str:
    """The decoded last `size` tokens of `text`."""
    tokens = tokenizer.encode(text)
    return tokenizer.decode(tokens[-size:]) if size > 0 else ""


def sentences(text: str) -> list[tuple[int, str]]:
    """The sentences of `text` with their start index in it; empty pieces are left out."""
    found: list[tuple[int, str]] = []
    start = 0
    for boundary in SENTENCE_BOUNDARY.finditer(text):
        if boundary.start() > start:
            found.append((start, text[start : boundary.start()]))
        start = boundary.end()
    if start < len(text):
        found.append((start, text[start:]))
    return found


@dataclass(frozen=True, slots=True)
class Piece:
    """One unit a chunker packs: its text, its own token count, the separator written before it
    when it follows another piece in one chunk, its start offset in the method's source text and
    the paragraph or element it comes from."""

    text: str
    tokens: int
    joiner: str = "\n\n"
    offset: int = 0
    source: int = 0


def joined(pieces: Sequence[Piece]) -> str:
    """The text of consecutive pieces of one chunk."""
    if not pieces:
        return ""
    return pieces[0].text + "".join(piece.joiner + piece.text for piece in pieces[1:])


def pack(
    pieces: Sequence[Piece], limit: int, tokenizer: Tokenizer, *, first_limit: int | None = None
) -> list[list[Piece]]:
    """Consecutive pieces packed greedily into groups whose joined text has at most `limit`
    tokens (`first_limit` for the first group); a piece above the limit is a group of its own.

    Groups are formed from the pieces' own counts, then each group's joined text is counted
    once and trailing pieces move on while it is over, because tokens at a join can differ from
    the sum of the parts.
    """
    separator_tokens: dict[str, int] = {}

    def joiner_tokens(joiner: str) -> int:
        if joiner not in separator_tokens:
            separator_tokens[joiner] = count_tokens(tokenizer, joiner)
        return separator_tokens[joiner]

    def bound(index: int) -> int:
        return first_limit if index == 0 and first_limit is not None else limit

    groups: list[list[Piece]] = []
    current: list[Piece] = []
    used = 0
    for piece in pieces:
        added = piece.tokens + (joiner_tokens(piece.joiner) if current else 0)
        if current and used + added > bound(len(groups)):
            groups.append(current)
            current, used, added = [], 0, piece.tokens
        current.append(piece)
        used += added
    if current:
        groups.append(current)
    index = 0
    while index < len(groups):
        group = groups[index]
        moved: list[Piece] = []
        while len(group) > 1 and count_tokens(tokenizer, joined(group)) > bound(index):
            moved.insert(0, group.pop())
        if moved:
            if index + 1 < len(groups):
                groups[index + 1][:0] = moved
            else:
                groups.append(moved)
        index += 1
    return groups
