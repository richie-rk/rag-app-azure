"""The recursive chunker (`14-ingestion.md` sections 11.2 to 11.6; ADR 0048 items 4 to 9).

The layout service's Markdown is parsed, in order, into headings, paragraphs, tables and figure
captions, each recording its start offset in the Markdown; every HTML comment is dropped, the
page markers among them, because pages come only from the result's spans. Headings open sections
whose heading path is the stack of open headings. Within a section paragraphs pack up to
`targetTokens`, a longer paragraph by sentences and a longer sentence by tokens, each chunk after
the first repeating the trailing sentences of the one before it; a table is a chunk of its own,
split between rows with its header rows repeated when it is longer than `tableMaxTokens`. Each
chunk cites the page of the span holding the offset of the first text it adds.
"""

from __future__ import annotations

import bisect
import html
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from docchat_core.ingestion.methods import RecursiveParametersV1
from docchat_ingestion.methods.chunks import MAX_CHUNK_TOKENS, Chunk, capped, heading_path
from docchat_ingestion.methods.tables import (
    has_cell_text,
    parse_table,
    sanitize_table_html,
    split_table,
)
from docchat_ingestion.methods.text import (
    Piece,
    Tokenizer,
    count_tokens,
    joined,
    last_tokens,
    pack,
    sentences,
    token_slices,
)
from docchat_ingestion.workfiles import ExtractDocument, PageOffset

PARAGRAPH_JOINER: Final = "\n\n"
SENTENCE_JOINER: Final = " "

_HEADING: Final = re.compile(r"(#{1,6}) (.*)")
_COMMENT: Final = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_FIGCAPTION: Final = re.compile(
    r"<figcaption\b[^>]*>(.*?)</figcaption\s*>", re.DOTALL | re.IGNORECASE
)
_ANY_TAG: Final = re.compile(r"<[^>]*>")
_OPENS_BLOCK: Final = re.compile(r"\s*(?:<table\b|<figure\b|<!--)", re.IGNORECASE)
_TR: Final = re.compile(r"<(/?)(table|tr)\b[^>]*>", re.IGNORECASE)
_WHITESPACE: Final = re.compile(r"\s+")


# --- Blocks ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MappedText:
    """Text cut from the Markdown with pieces removed, and where each kept run started there,
    so every index into the text has its Markdown offset."""

    text: str
    starts: tuple[int, ...]
    sources: tuple[int, ...]

    def offset(self, index: int) -> int:
        run = bisect.bisect_right(self.starts, index) - 1
        return self.sources[max(run, 0)] + index - self.starts[max(run, 0)]


def _without_comments(markdown: str, start: int, end: int) -> MappedText:
    """`markdown[start:end]` with every HTML comment removed and the ends trimmed."""
    runs: list[tuple[str, int]] = []
    position = start
    for comment in _COMMENT.finditer(markdown, start, end):
        if comment.start() > position:
            runs.append((markdown[position : comment.start()], position))
        position = comment.end()
    if position < end:
        runs.append((markdown[position:end], position))
    text = "".join(run for run, _ in runs)
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    starts: list[int] = []
    sources: list[int] = []
    index = 0
    for run, source in runs:
        starts.append(index - lead)
        sources.append(source)
        index += len(run)
    return MappedText(stripped, tuple(starts), tuple(sources))


@dataclass(frozen=True, slots=True)
class Block:
    """One block of the Markdown: a heading (with its level), a paragraph (a figure's caption
    included) or a table (its sanitised HTML and the Markdown offset of each top-level row)."""

    kind: Literal["heading", "paragraph", "table"]
    offset: int
    text: MappedText
    level: int = 0
    row_offsets: tuple[int, ...] = ()


def _line_end(markdown: str, position: int) -> int:
    end = markdown.find("\n", position)
    return len(markdown) if end < 0 else end


def _element_end(markdown: str, start: int, tag: str) -> int:
    """The index after the `</tag>` that closes the element opened at `start`, nested elements
    of the same tag counted; the end of the Markdown when it is never closed."""
    pattern = re.compile(rf"<(/?){tag}\b[^>]*>", re.IGNORECASE)
    depth = 0
    for match in pattern.finditer(markdown, start):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return match.end()
    return len(markdown)


def _row_offsets(markdown: str, start: int, end: int) -> tuple[int, ...]:
    offsets: list[int] = []
    depth = 0
    for match in _TR.finditer(markdown, start, end):
        closing, name = bool(match.group(1)), match.group(2).lower()
        if name == "table":
            depth += -1 if closing else 1
        elif not closing and depth == 1:
            offsets.append(match.start())
    return tuple(offsets)


def _at(text: str, offset: int) -> MappedText:
    """Text that stands for the block at `offset` as a whole."""
    return MappedText(text, (0,), (offset,))


def _table_block(markdown: str, lead: int) -> tuple[Block | None, int]:
    """The table opened at `lead`, sanitised, and the index after it; a table whose cells hold
    no text gives no block."""
    close = _element_end(markdown, lead, "table")
    sanitised = sanitize_table_html(markdown[lead:close])
    if not has_cell_text(sanitised):
        return None, close
    block = Block(
        "table", lead, _at(sanitised, lead), row_offsets=_row_offsets(markdown, lead, close)
    )
    return block, close


def _figure_block(markdown: str, lead: int) -> tuple[Block | None, int]:
    """The figure opened at `lead` as a paragraph of its caption's text, and the index after
    it; a figure without a caption gives no block."""
    close = _element_end(markdown, lead, "figure")
    caption = _FIGCAPTION.search(markdown, lead, close)
    if caption is None:
        return None, close
    text = _ANY_TAG.sub("", _COMMENT.sub("", caption.group(1)))
    text = _WHITESPACE.sub(" ", html.unescape(text)).strip()
    return (Block("paragraph", lead, _at(text, lead)) if text else None), close


def _paragraph_end(markdown: str, end: int) -> int:
    """The end of the paragraph whose first line ends at `end`: the next blank line, or the
    line before one that starts a heading, a table, a figure or a comment."""
    stop, following = end, end + 1
    while following < len(markdown):
        next_end = _line_end(markdown, following)
        line = markdown[following:next_end]
        if not line.strip() or _HEADING.fullmatch(line) or _OPENS_BLOCK.match(line):
            break
        stop, following = next_end, next_end + 1
    return stop


def parse_blocks(markdown: str) -> list[Block]:
    """The blocks of the layout Markdown in order (section 11.2)."""
    blocks: list[Block] = []
    position = 0
    while position < len(markdown):
        end = _line_end(markdown, position)
        line = markdown[position:end]
        lead = position + len(line) - len(line.lstrip())
        opening = markdown[lead : lead + 8].lower()
        block: Block | None = None
        if not line.strip():
            position = end + 1
        elif opening.startswith("<!--"):
            close = markdown.find("-->", lead + 4)
            position = len(markdown) if close < 0 else close + 3
        elif opening.startswith("<table"):
            block, position = _table_block(markdown, lead)
        elif opening.startswith("<figure"):
            block, position = _figure_block(markdown, lead)
        elif (heading := _HEADING.fullmatch(line)) is not None:
            text = _without_comments(markdown, position + heading.start(2), end)
            block = Block("heading", position, text, level=len(heading.group(1)))
            position = end + 1
        else:
            stop = _paragraph_end(markdown, end)
            text = _without_comments(markdown, position, stop)
            block = Block("paragraph", position, text) if text.text else None
            position = stop + 1
        if block is not None:
            blocks.append(block)
    return blocks


# --- Sections -------------------------------------------------------------------------------------


@dataclass(slots=True)
class Section:
    """A heading's section: the heading (`None` before the first one), the cleaned heading path
    and its paragraphs and tables in order."""

    heading: Block | None
    path: tuple[str, ...]
    content: list[Block] = field(default_factory=list)


def sections(blocks: Sequence[Block]) -> list[Section]:
    """The sections of the blocks (section 11.3): a heading closes every open heading of its
    level or deeper and opens its own."""
    stack: list[Block] = []
    found = [Section(None, ())]
    for block in blocks:
        if block.kind == "heading":
            while stack and stack[-1].level >= block.level:
                stack.pop()
            stack.append(block)
            found.append(Section(block, heading_path(entry.text.text for entry in stack)))
        else:
            found[-1].content.append(block)
    return [section for section in found if section.content]


# --- Pages ----------------------------------------------------------------------------------------


class PageLookup:
    """The page of a Markdown offset (section 11.6 item 2): the page of the span holding it; in
    no span, the page of the span that starts closest before it; before every span, the first
    span's page; with no spans, page 1; never above `page_count`.

    The layout service's spans don't overlap, so the span holding an offset is the one that
    starts closest before it, and one search over the sorted starts answers both rules.
    """

    def __init__(self, spans: Sequence[PageOffset], page_count: int) -> None:
        ordered = sorted(spans, key=lambda span: span.offset)
        self._starts = [span.offset for span in ordered]
        self._pages = [span.page_number for span in ordered]
        self._page_count = page_count

    def page(self, offset: int) -> int:
        if not self._pages:
            return 1
        index = bisect.bisect_right(self._starts, offset) - 1
        page = self._pages[max(index, 0)]
        return min(max(page, 1), self._page_count)


# --- Packing --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Draft:
    """A chunk before its page is known: its text, the offset of the first text it adds and,
    for the overlap of the next chunk, its body without the heading line."""

    text: str
    offset: int
    body: str
    last_source: int


def _paragraph_pieces(
    block: Block, source: int, target: int, tokenizer: Tokenizer
) -> Iterator[Piece]:
    """A paragraph as one piece, or by sentences, a sentence above the target by tokens."""
    text = block.text.text
    size = count_tokens(tokenizer, text)
    if size <= target:
        yield Piece(text, size, PARAGRAPH_JOINER, block.text.offset(0), source)
        return
    first = True
    for start, sentence in sentences(text):
        joiner = PARAGRAPH_JOINER if first else SENTENCE_JOINER
        first = False
        sentence_size = count_tokens(tokenizer, sentence)
        if sentence_size <= target:
            yield Piece(sentence, sentence_size, joiner, block.text.offset(start), source)
            continue
        cut = start
        for index, part in enumerate(token_slices(tokenizer, sentence, target)):
            yield Piece(
                part,
                count_tokens(tokenizer, part),
                joiner if index == 0 else "",
                block.text.offset(cut),
                source,
            )
            cut += len(part)


def overlap_text(body: str, budget: int, tokenizer: Tokenizer) -> str:
    """The trailing whole sentences of `body` that fit in `budget` tokens; when even the last
    sentence is longer, its last `budget` tokens (section 11.4 item 2)."""
    if budget <= 0 or not body:
        return ""
    found = [sentence for _, sentence in sentences(body)]
    kept: list[str] = []
    for sentence in reversed(found):
        candidate = SENTENCE_JOINER.join([sentence, *kept])
        if count_tokens(tokenizer, candidate) > budget:
            break
        kept.insert(0, sentence)
    if kept:
        return SENTENCE_JOINER.join(kept)
    return last_tokens(tokenizer, found[-1], budget) if found else ""


@dataclass(slots=True)
class _SectionChunker:
    section: Section
    params: RecursiveParametersV1
    tokenizer: Tokenizer
    drafts: list[_Draft] = field(default_factory=list)
    run: list[Piece] = field(default_factory=list)
    previous: _Draft | None = None

    @property
    def heading(self) -> str | None:
        heading = self.section.heading
        if heading is None or self.drafts:
            return None
        return heading.text.text or None

    def _start_offset(self, own: int) -> int:
        heading = self.section.heading
        return heading.offset if heading is not None and not self.drafts else own

    def _with_heading(self, body: str) -> str:
        heading = self.heading
        return f"{heading}{PARAGRAPH_JOINER}{body}" if heading is not None else body

    def flush(self) -> None:
        if not self.run:
            return
        target = self.params.target_tokens
        budget = target * self.params.overlap_percent // 100
        heading = self.heading
        first_limit = target
        if heading is not None:
            first_limit = max(target - count_tokens(self.tokenizer, heading + PARAGRAPH_JOINER), 1)
        for group in pack(self.run, target, self.tokenizer, first_limit=first_limit):
            own = joined(group)
            body = own
            if self.previous is not None:
                overlap = overlap_text(self.previous.body, budget, self.tokenizer)
                if overlap:
                    continues = self.previous.last_source == group[0].source
                    joiner = SENTENCE_JOINER if continues else PARAGRAPH_JOINER
                    body = f"{overlap}{joiner}{own}"
            draft = _Draft(
                self._with_heading(body),
                self._start_offset(group[0].offset),
                body,
                group[-1].source,
            )
            self.drafts.append(draft)
            self.previous = draft
        self.run.clear()

    def table(self, block: Block) -> None:
        self.flush()
        limit = self.params.table_max_tokens
        sanitised = block.text.text
        structure = parse_table(sanitised)
        if count_tokens(self.tokenizer, sanitised) <= limit or structure is None:
            parts = [(sanitised, block.offset)]
        else:
            parts = []
            for index, part in enumerate(split_table(structure, limit, self.tokenizer)):
                row = len(structure.header) + part.first_row
                offset = block.offset
                if index > 0 and len(block.row_offsets) == len(structure.header) + len(
                    structure.body
                ):
                    offset = block.row_offsets[row]
                parts.append((part.html, offset))
        for part_html, offset in parts:
            for piece in self._capped(part_html):
                self.drafts.append(
                    _Draft(self._with_heading(piece), self._start_offset(offset), piece, -1)
                )
        # The chunk after a table starts without overlap.
        self.previous = None

    def _capped(self, text: str) -> list[str]:
        if count_tokens(self.tokenizer, text) <= MAX_CHUNK_TOKENS:
            return [text]
        return token_slices(self.tokenizer, text, MAX_CHUNK_TOKENS)

    def chunk(self) -> list[_Draft]:
        target = self.params.target_tokens
        for source, block in enumerate(self.section.content):
            if block.kind == "table":
                self.table(block)
            else:
                self.run.extend(_paragraph_pieces(block, source, target, self.tokenizer))
        self.flush()
        return self.drafts


def chunk_document(
    document: ExtractDocument, params: RecursiveParametersV1, tokenizer: Tokenizer
) -> list[Chunk]:
    """Every chunk of the extract file, in document order."""
    pages = PageLookup(document.page_offsets, document.page_count)
    chunks: list[Chunk] = []
    for section in sections(parse_blocks(document.markdown)):
        for draft in _SectionChunker(section, params, tokenizer).chunk():
            chunk = Chunk(draft.text, section.path, pages.page(draft.offset))
            chunks.extend(capped(chunk, tokenizer))
    return chunks
