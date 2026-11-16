"""Table HTML: the sanitiser both methods apply, and the row structure their splitting and the
table-summary input cap work on (`14-ingestion.md` sections 10.5, 10.7, 11.5 and 12.4; ADR 0047
item 5).

Table markup comes from an untrusted document and reaches the index and the table-summary prompt
only through `sanitize_table_html`. The structure functions read only sanitised HTML, which the
sanitiser serialises with lower-case tags, quoted attributes and every `<` in text escaped, so a
tag scan over it is exact.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import unescape
from typing import Final

import nh3

from docchat_ingestion.methods.text import Tokenizer, count_tokens, token_slices

TABLE_TAGS: Final = frozenset(
    {"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "br"}
)
CELL_ATTRIBUTES: Final = {"th": {"colspan", "rowspan", "scope"}, "td": {"colspan", "rowspan"}}
SPAN_ATTRIBUTES: Final = frozenset({"colspan", "rowspan"})
SPAN_MIN: Final = 1
SPAN_MAX: Final = 1000
OMITTED_ROW: Final = "<tr><td>[rows omitted]</td></tr>"
"""The row the table-summary input gets in place of the body rows dropped to fit its cap."""

_TAG: Final = re.compile(r"<(/?)([a-z]+)\b[^>]*>")
_DIGITS: Final = re.compile(r"[0-9]+")


def _span_filter(tag: str, attribute: str, value: str) -> str | None:
    """Removes a `colspan` or `rowspan` whose value isn't an integer from 1 to 1,000."""
    del tag
    if attribute in SPAN_ATTRIBUTES and not (
        _DIGITS.fullmatch(value) and SPAN_MIN <= int(value) <= SPAN_MAX
    ):
        return None
    return value


def sanitize_table_html(html: str) -> str:
    """`nh3.clean()` with the table allowlist: the tags `table`, `caption`, `thead`, `tbody`,
    `tfoot`, `tr`, `th`, `td` and `br`; `colspan` and `rowspan` on `th` and `td` and `scope` on
    `th`, spans outside 1 to 1,000 removed; no URL scheme; comments stripped. Every other
    element keeps only its text, and `script` and `style` lose their content too."""
    return nh3.clean(
        html,
        tags=set(TABLE_TAGS),
        attributes={tag: set(names) for tag, names in CELL_ATTRIBUTES.items()},
        attribute_filter=_span_filter,
        url_schemes=set(),
        strip_comments=True,
        link_rel=None,
    )


def table_sha256(html: str) -> str:
    """The key of a table's summary: the SHA-256 of its sanitised HTML, hexadecimal."""
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def _cell_texts(html: str) -> list[str]:
    texts: list[str] = []
    depth = 0
    start: int | None = None
    for tag in _TAG.finditer(html):
        closing, name = tag.group(1) == "/", tag.group(2)
        if name in {"th", "td"}:
            if not closing:
                depth += 1
                if depth == 1:
                    start = tag.end()
            else:
                depth -= 1
                if depth == 0 and start is not None:
                    texts.append(_TAG.sub("", html[start : tag.start()]))
                    start = None
    return texts


def has_cell_text(html: str) -> bool:
    """Whether any `th` or `td` cell of a sanitised table holds text. Character references are
    resolved first, because the sanitiser writes a no-break space as `&nbsp;`, which is no
    text."""
    return any(unescape(text).strip() for text in _cell_texts(html))


@dataclass(frozen=True, slots=True)
class TableStructure:
    """A sanitised table's top-level rows: the `<caption>`, the header rows (the `<thead>`
    rows, or else the leading rows made only of `th` cells) and the body rows, each row its
    own `<tr>…</tr>` text."""

    caption: str
    header: tuple[str, ...]
    header_in_thead: bool
    body: tuple[str, ...]

    def html(self, body: Sequence[str] | None = None, *, tail: str = "") -> str:
        """`<table>`, the caption, the header rows, `body` (every body row by default) and
        `tail`, closed with `</table>`."""
        rows = self.body if body is None else body
        header = "".join(self.header)
        if self.header_in_thead and header:
            header = f"<thead>{header}</thead>"
        return f"<table>{self.caption}{header}{''.join(rows)}{tail}</table>"


@dataclass(frozen=True, slots=True)
class _Row:
    html: str
    in_thead: bool
    all_th: bool


@dataclass(slots=True)
class _TableScan:
    """The top-level caption and rows of one sanitised table, tag by tag."""

    html: str
    caption: str = ""
    rows: list[_Row] = field(default_factory=list)
    _caption_start: int | None = None
    _in_thead: bool = False
    _row_start: int | None = None
    _row_in_thead: bool = False
    _cells: list[str] = field(default_factory=list)

    def caption_tag(self, tag: re.Match[str], closing: bool) -> None:
        if not closing:
            self._caption_start = tag.start()
        elif self._caption_start is not None:
            self.caption = self.html[self._caption_start : tag.end()]

    def row_tag(self, tag: re.Match[str], closing: bool) -> None:
        if not closing:
            self._row_start, self._row_in_thead, self._cells = tag.start(), self._in_thead, []
        elif self._row_start is not None:
            all_th = bool(self._cells) and all(cell == "th" for cell in self._cells)
            self.rows.append(
                _Row(self.html[self._row_start : tag.end()], self._row_in_thead, all_th)
            )
            self._row_start = None

    def feed(self, tag: re.Match[str]) -> None:
        closing, name = tag.group(1) == "/", tag.group(2)
        if name == "caption":
            self.caption_tag(tag, closing)
        elif name == "thead":
            self._in_thead = not closing
        elif name == "tr":
            self.row_tag(tag, closing)
        elif name in {"th", "td"} and not closing and self._row_start is not None:
            self._cells.append(name)


def parse_table(html: str) -> TableStructure | None:
    """The structure of the first top-level table of sanitised `html`; `None` without one."""
    scan = _TableScan(html)
    depth = 0
    found = False
    for tag in _TAG.finditer(html):
        if tag.group(2) == "table":
            depth += -1 if tag.group(1) else 1
            found = True
            if depth == 0:
                break
        elif depth == 1:
            scan.feed(tag)
    if not found:
        return None
    rows = scan.rows
    thead = [row.html for row in rows if row.in_thead]
    if thead:
        header = thead
        body = [row.html for row in rows if not row.in_thead]
    else:
        count = 0
        while count < len(rows) and rows[count].all_th:
            count += 1
        header = [row.html for row in rows[:count]]
        body = [row.html for row in rows[count:]]
    return TableStructure(scan.caption, tuple(header), bool(thead), tuple(body))


@dataclass(frozen=True, slots=True)
class TablePart:
    """One part of a split table and the index of its first body row in the table."""

    html: str
    first_row: int


def split_table(
    structure: TableStructure, limit: int, tokenizer: Tokenizer, *, first_limit: int | None = None
) -> list[TablePart]:
    """Parts between rows: each is `<table>`, the caption, the header rows and as many body
    rows as fit in `limit` tokens (`first_limit` for the first part), closed with `</table>`. A
    single row that doesn't fit with the header is a part of its own, whatever its size."""
    if not structure.body:
        return [TablePart(structure.html(), 0)]
    base = count_tokens(tokenizer, structure.html(()))
    parts: list[list[int]] = []
    current: list[int] = []
    used = base
    for index, row in enumerate(structure.body):
        bound = first_limit if not parts and first_limit is not None else limit
        size = count_tokens(tokenizer, row)
        if current and used + size > bound:
            parts.append(current)
            current, used = [], base
        current.append(index)
        used += size
    parts.append(current)
    # Joins can count differently from the parts, so each part is counted once and trailing
    # rows move to the next part while it is over.
    position = 0
    while position < len(parts):
        bound = first_limit if position == 0 and first_limit is not None else limit
        rows = parts[position]
        moved: list[int] = []
        while len(rows) > 1 and count_tokens(tokenizer, _part_html(structure, rows)) > bound:
            moved.insert(0, rows.pop())
        if moved:
            if position + 1 < len(parts):
                parts[position + 1][:0] = moved
            else:
                parts.append(moved)
        position += 1
    return [TablePart(_part_html(structure, rows), rows[0]) for rows in parts]


def _part_html(structure: TableStructure, rows: Sequence[int]) -> str:
    return structure.html([structure.body[index] for index in rows])


def fit_table(html: str, limit: int, tokenizer: Tokenizer) -> tuple[str, bool]:
    """The table-summary input of a sanitised table and whether it was cut: trailing body rows
    dropped and `OMITTED_ROW` appended before `</table>` until it fits in `limit` tokens; a table
    that still doesn't fit with no body row left is cut at the token limit."""
    if count_tokens(tokenizer, html) <= limit:
        return html, False
    structure = parse_table(html)
    if structure is None:
        return token_slices(tokenizer, html, limit)[0], True

    def candidate(kept: int) -> str:
        return structure.html(structure.body[:kept], tail=OMITTED_ROW)

    low, high = 0, len(structure.body) - 1
    best: int | None = None
    while low <= high:
        middle = (low + high) // 2
        if count_tokens(tokenizer, candidate(middle)) <= limit:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    if best is None:
        return token_slices(tokenizer, candidate(0), limit)[0], True
    return candidate(best), True
