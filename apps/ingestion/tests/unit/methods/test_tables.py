"""Unit tests of the table sanitiser and the table structure (T-184; `14-ingestion.md` sections
10.5, 10.7, 11.5 and 12.4)."""

from __future__ import annotations

import hashlib

import pytest

from docchat_ingestion.methods.tables import (
    OMITTED_ROW,
    TableStructure,
    fit_table,
    has_cell_text,
    parse_table,
    sanitize_table_html,
    split_table,
    table_sha256,
)
from docchat_ingestion.methods.text import Tokenizer, count_tokens

HOSTILE = (
    '<table onclick="steal()" style="color:red" class="x">'
    '<caption>Prices <a href="https://example.net/x">see</a></caption>'
    "<!-- hidden note -->"
    '<thead><tr><th colspan="0" scope="col" onmouseover="y()">Item</th>'
    '<th rowspan="5000">Price</th><th colspan="3">Notes</th></tr></thead>'
    '<tbody><tr><td><img src="https://example.net/i.png" onerror="alert(1)">Tea'
    "<script>alert(1)</script><style>td { color: red }</style></td>"
    '<td rowspan="1000">2 <b>EUR</b></td><td>a<br>b &lt;x&gt;</td></tr></tbody></table>'
)


def test_t184_the_sanitiser_keeps_only_the_table_allowlist() -> None:
    cleaned = sanitize_table_html(HOSTILE)

    assert cleaned == (
        "<table><caption>Prices see</caption>"
        '<thead><tr><th scope="col">Item</th><th>Price</th><th colspan="3">Notes</th></tr>'
        "</thead><tbody><tr><td>Tea</td>"
        '<td rowspan="1000">2 EUR</td><td>a<br>b &lt;x&gt;</td></tr></tbody></table>'
    )
    for removed in ("script", "style", "onclick", "onerror", "onmouseover", "href", "img", "<!--"):
        assert removed not in cleaned
    assert "alert" not in cleaned


@pytest.mark.parametrize(
    ("value", "kept"),
    [("0", False), ("1", True), ("1000", True), ("1001", False), ("5000", False),
     ("2.5", False), ("x", False), ("-1", False)],
)  # fmt: skip
def test_t184_spans_outside_1_to_1000_are_removed(value: str, kept: bool) -> None:
    cleaned = sanitize_table_html(f'<table><tr><td colspan="{value}">x</td></tr></table>')

    assert ("colspan" in cleaned) is kept


def test_scope_is_kept_on_header_cells_only() -> None:
    cleaned = sanitize_table_html(
        '<table><tr><th scope="row">a</th><td scope="row" id="x">b</td></tr></table>'
    )

    assert cleaned == '<table><tbody><tr><th scope="row">a</th><td>b</td></tr></tbody></table>'


def test_the_summary_key_is_the_sha256_of_the_sanitised_html() -> None:
    html = "<table><tbody><tr><td>x</td></tr></tbody></table>"

    assert table_sha256(html) == hashlib.sha256(html.encode("utf-8")).hexdigest()


def test_cell_text() -> None:
    assert has_cell_text("<table><tbody><tr><td> x </td></tr></tbody></table>")
    assert not has_cell_text("<table><caption>c</caption><tr><td> <br> </td><th></th></tr></table>")
    # The sanitiser writes a no-break space as a character reference, which is no text.
    assert not has_cell_text("<table><tbody><tr><td>&nbsp;</td><td>&#32;</td></tr></tbody></table>")
    assert has_cell_text("<table><tbody><tr><td>&amp;</td></tr></tbody></table>")


def test_a_thead_gives_the_header_rows() -> None:
    html = sanitize_table_html(
        "<table><caption>C</caption><thead><tr><th>A</th></tr><tr><td>sub</td></tr></thead>"
        "<tbody><tr><td>1</td></tr><tr><td>2</td></tr></tbody><tfoot><tr><td>T</td></tr></tfoot>"
        "</table>"
    )
    structure = parse_table(html)

    assert structure == TableStructure(
        caption="<caption>C</caption>",
        header=("<tr><th>A</th></tr>", "<tr><td>sub</td></tr>"),
        header_in_thead=True,
        body=("<tr><td>1</td></tr>", "<tr><td>2</td></tr>", "<tr><td>T</td></tr>"),
    )
    assert structure.html(["<tr><td>2</td></tr>"]) == (
        "<table><caption>C</caption><thead><tr><th>A</th></tr><tr><td>sub</td></tr></thead>"
        "<tr><td>2</td></tr></table>"
    )


def test_leading_header_cell_rows_are_the_header_without_a_thead() -> None:
    html = sanitize_table_html(
        "<table><tr><th>A</th><th>B</th></tr><tr><th>a</th><th>b</th></tr>"
        "<tr><th>k</th><td>1</td></tr><tr><th>x</th><th>y</th></tr></table>"
    )
    structure = parse_table(html)

    assert structure is not None
    assert structure.header == ("<tr><th>A</th><th>B</th></tr>", "<tr><th>a</th><th>b</th></tr>")
    assert not structure.header_in_thead
    assert structure.body == ("<tr><th>k</th><td>1</td></tr>", "<tr><th>x</th><th>y</th></tr>")
    assert structure.html(()) == (
        "<table><tr><th>A</th><th>B</th></tr><tr><th>a</th><th>b</th></tr></table>"
    )


def test_a_nested_table_stays_inside_its_cell() -> None:
    html = sanitize_table_html(
        "<table><tr><td>outer<table><tr><td>inner</td></tr></table></td></tr>"
        "<tr><td>second</td></tr></table><table><tr><td>next table</td></tr></table>"
    )
    structure = parse_table(html)

    assert structure is not None
    assert len(structure.body) == 2
    assert "inner" in structure.body[0]
    assert "next table" not in structure.html()


def test_html_without_a_table_has_no_structure() -> None:
    assert parse_table("just text") is None


def _rows(count: int) -> str:
    rows = "".join(f"<tr><td>row {index} value</td></tr>" for index in range(count))
    return sanitize_table_html(f"<table><thead><tr><th>Name here</th></tr></thead>{rows}</table>")


def test_a_table_splits_between_rows_repeating_its_header(tokenizer: Tokenizer) -> None:
    structure = parse_table(_rows(10))
    assert structure is not None
    header_tokens = count_tokens(tokenizer, structure.html(()))

    parts = split_table(structure, header_tokens + 40, tokenizer)

    assert len(parts) > 1
    assert [part.first_row for part in parts] == sorted({part.first_row for part in parts})
    for part in parts:
        assert part.html.startswith("<table><thead><tr><th>Name here</th></tr></thead>")
        assert part.html.endswith("</table>")
        assert count_tokens(tokenizer, part.html) <= header_tokens + 40
    rows = "".join(part.html for part in parts)
    assert all(f"row {index} value" in rows for index in range(10))


def test_the_first_part_can_have_a_smaller_limit(tokenizer: Tokenizer) -> None:
    structure = parse_table(_rows(6))
    assert structure is not None
    header_tokens = count_tokens(tokenizer, structure.html(()))

    parts = split_table(structure, header_tokens + 40, tokenizer, first_limit=header_tokens + 25)

    assert count_tokens(tokenizer, parts[0].html) <= header_tokens + 25
    assert parts[0].html.count("<tr>") == 2
    assert [part.first_row for part in parts] == [0, 1, 3, 5]


def test_a_row_too_long_for_the_limit_is_a_part_of_its_own(tokenizer: Tokenizer) -> None:
    long_row = "<tr><td>" + " ".join(["word"] * 50) + "</td></tr>"
    structure = TableStructure(
        "", (), False, ("<tr><td>a</td></tr>", long_row, "<tr><td>b</td></tr>")
    )

    parts = split_table(structure, 20, tokenizer)

    assert [part.first_row for part in parts] == [0, 1, 2]
    assert parts[1].html == f"<table>{long_row}</table>"


def test_a_table_without_body_rows_is_one_part(tokenizer: Tokenizer) -> None:
    structure = TableStructure("", ("<tr><th>h</th></tr>",), False, ())

    assert split_table(structure, 5, tokenizer)[0].html == "<table><tr><th>h</th></tr></table>"


def test_fit_table_drops_trailing_rows_and_marks_them(tokenizer: Tokenizer) -> None:
    html = _rows(20)

    fitted, truncated = fit_table(html, 80, tokenizer)

    assert truncated
    assert fitted.endswith(f"{OMITTED_ROW}</table>")
    assert count_tokens(tokenizer, fitted) <= 80
    assert "row 0 value" in fitted
    assert "row 19 value" not in fitted
    assert fit_table(html, 10_000, tokenizer) == (html, False)


def test_fit_table_cuts_what_still_does_not_fit(tokenizer: Tokenizer) -> None:
    fitted, truncated = fit_table(_rows(3), 5, tokenizer)
    assert truncated
    assert count_tokens(tokenizer, fitted) <= 5

    no_table, cut = fit_table("one two three four five six", 3, tokenizer)
    assert (no_table, cut) == ("one two", True)
