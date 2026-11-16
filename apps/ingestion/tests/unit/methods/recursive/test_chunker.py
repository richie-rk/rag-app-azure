"""Unit tests of the recursive chunker (T-183, T-209 in part; `14-ingestion.md` sections 11.2 to
11.6; ADR 0048 items 4 to 9).

The tokenizer of these tests counts each word, punctuation mark and whitespace run as one token,
so `sentence(n)` below is exactly 20 tokens and the limits can be checked by hand.
"""

from __future__ import annotations

import pytest

from docchat_core.ingestion.methods import RecursiveParametersV1
from docchat_ingestion.methods.recursive.chunker import (
    Block,
    PageLookup,
    chunk_document,
    overlap_text,
    parse_blocks,
    sections,
)
from docchat_ingestion.methods.text import Tokenizer, count_tokens
from docchat_ingestion.workfiles import ExtractDocument, PageOffset

DEFAULTS = RecursiveParametersV1(target_tokens=128, overlap_percent=25, table_max_tokens=256)


def sentence(number: int, words: int = 10) -> str:
    """`words` words and a full stop: twice `words` tokens."""
    return " ".join(f"s{number}w{index}" for index in range(words)) + "."


def paragraph(*numbers: int) -> str:
    return " ".join(sentence(number) for number in numbers)


def document(
    markdown: str, spans: list[tuple[int, int, int]] | None = None, pages: int = 9
) -> ExtractDocument:
    offsets = [
        PageOffset(page_number=page, offset=offset, length=length)
        for page, offset, length in (spans or [(1, 0, len(markdown))])
    ]
    return ExtractDocument(
        format_version=1,
        method="recursive",
        page_count=pages,
        markdown=markdown,
        page_offsets=offsets,
        doc_intel_pages=pages,
    )


# --- Blocks ---------------------------------------------------------------------------------------


def test_blocks_are_headings_paragraphs_tables_and_figure_captions() -> None:
    markdown = (
        '<!-- PageHeader="Acme report" -->\n\n'
        "# Results\n\n"
        "First line of a paragraph\nand its second line.\n\n"
        "Text with <!-- an inline comment --> inside.\n"
        "<!-- PageBreak -->\n"
        "## Table section\n\n"
        '<table><tr><th>A</th></tr><tr><td onclick="x()">1</td></tr></table>\n\n'
        "<figure>\n<figcaption>Figure 1: Growth</figcaption>\n![](figures/1)\n</figure>\n\n"
        "<figure>\n![](figures/2)\n</figure>\n\n"
        '<!-- PageNumber="2" -->\n'
        "####### Not a heading\n"
    )

    blocks = parse_blocks(markdown)

    assert [(block.kind, block.text.text) for block in blocks] == [
        ("heading", "Results"),
        ("paragraph", "First line of a paragraph\nand its second line."),
        ("paragraph", "Text with  inside."),
        ("heading", "Table section"),
        ("table", "<table><tbody><tr><th>A</th></tr><tr><td>1</td></tr></tbody></table>"),
        ("paragraph", "Figure 1: Growth"),
        ("paragraph", "####### Not a heading"),
    ]
    assert [block.level for block in blocks if block.kind == "heading"] == [1, 2]
    for block in blocks:
        if block.kind != "table":
            assert markdown[block.offset :].startswith(markdown[block.offset : block.offset + 1])
    assert blocks[0].offset == markdown.index("# Results")
    assert blocks[2].offset == markdown.index("Text with")
    assert blocks[2].text.offset(blocks[2].text.text.index("inside")) == markdown.index("inside")
    assert blocks[4].row_offsets == (markdown.index("<tr><th>"), markdown.index("<tr><td"))
    assert blocks[5].offset == markdown.index("<figure>")


def test_a_comment_or_table_left_open_runs_to_the_end() -> None:
    assert parse_blocks("Text\n\n<!-- never closed\n\nmore") == [
        Block("paragraph", 0, parse_blocks("Text")[0].text)
    ]
    (table,) = parse_blocks("<table><tr><td>open")
    assert table.kind == "table"


def test_an_empty_table_and_an_empty_paragraph_give_no_block() -> None:
    assert parse_blocks("<table><tr><td> </td></tr></table>\n\n<!-- a -->   <!-- b -->\n") == []


def test_a_comment_mid_line_keeps_the_text_after_it() -> None:
    markdown = '<!-- PageFooter="x" --> Trailing text.'

    (block,) = parse_blocks(markdown)

    assert block.text.text == "Trailing text."
    assert block.text.offset(0) == markdown.index("Trailing")


# --- Sections -------------------------------------------------------------------------------------


def test_t209_heading_paths_are_the_stack_of_open_headings_cleaned() -> None:
    markdown = "\n\n".join(
        [
            "Preface text.",
            "# Part \u202eOne\u0007",
            "## Chapter",
            "### Detail",
            "Detail text.",
            "## \u200b\u2066",
            "Unnamed text.",
            "### Skipped level",
            "# Two",
            "Two text.",
        ]
    )

    found = sections(parse_blocks(markdown))

    assert [(section.path, section.content[0].text.text) for section in found] == [
        ((), "Preface text."),
        (("Part One", "Chapter", "Detail"), "Detail text."),
        (("Part One",), "Unnamed text."),
        (("Two",), "Two text."),
    ]


def test_a_skipped_level_nests_under_the_open_heading() -> None:
    found = sections(parse_blocks("# A\n\n### C\n\nc text\n\n## B\n\nb text"))

    assert [section.path for section in found] == [("A", "C"), ("A", "B")]


# --- Pages ----------------------------------------------------------------------------------------


def test_t183_the_page_of_an_offset() -> None:
    lookup = PageLookup(
        [
            PageOffset(page_number=2, offset=10, length=10),
            PageOffset(page_number=3, offset=30, length=5),
            PageOffset(page_number=3, offset=35, length=5),
            PageOffset(page_number=12, offset=50, length=5),
        ],
        page_count=4,
    )

    assert lookup.page(15) == 2
    assert lookup.page(22) == 2
    assert lookup.page(31) == 3
    assert lookup.page(37) == 3
    assert lookup.page(3) == 2
    assert lookup.page(45) == 3
    assert lookup.page(52) == 4
    assert lookup.page(999) == 4
    assert PageLookup([], page_count=4).page(5) == 1


# --- Packing and overlap --------------------------------------------------------------------------


def test_t183_paragraphs_pack_heading_first_with_overlap_inside_the_section(
    tokenizer: Tokenizer,
) -> None:
    markdown = "\n\n".join(
        ["# H", paragraph(1, 2, 3), paragraph(4, 5, 6), paragraph(7, 8, 9), "# Next", paragraph(10)]
    )

    chunks = chunk_document(document(markdown), DEFAULTS, tokenizer)

    assert [chunk.text for chunk in chunks] == [
        f"H\n\n{paragraph(1, 2, 3)}\n\n{paragraph(4, 5, 6)}",
        f"{sentence(6)}\n\n{paragraph(7, 8, 9)}",
        f"Next\n\n{paragraph(10)}",
    ]
    assert [chunk.heading_path for chunk in chunks] == [("H",), ("H",), ("Next",)]
    overlap = 128 * 25 // 100
    for chunk in chunks:
        assert count_tokens(tokenizer, chunk.text) <= 128 + overlap


def test_t183_a_long_paragraph_splits_by_sentences_and_a_long_sentence_by_tokens(
    tokenizer: Tokenizer,
) -> None:
    long_sentence = sentence(99, words=100)
    markdown = "\n\n".join([paragraph(*range(1, 10)), long_sentence])
    params = RecursiveParametersV1(target_tokens=128, overlap_percent=0, table_max_tokens=256)

    chunks = chunk_document(document(markdown), params, tokenizer)

    assert all(count_tokens(tokenizer, chunk.text) <= 128 for chunk in chunks)
    assert " ".join(chunk.text for chunk in chunks[:2]) == paragraph(*range(1, 10))
    assert chunks[1].text.endswith(sentence(9))
    assert "".join(chunk.text for chunk in chunks[2:]) == long_sentence


def test_overlap_takes_trailing_sentences_or_the_last_tokens(tokenizer: Tokenizer) -> None:
    body = paragraph(1, 2, 3)

    assert overlap_text(body, 41, tokenizer) == paragraph(2, 3)
    assert overlap_text(body, 40, tokenizer) == sentence(3)
    assert overlap_text(body, 5, tokenizer) == " s3w8 s3w9."
    assert overlap_text(body, 0, tokenizer) == ""
    assert overlap_text("", 10, tokenizer) == ""


def test_overlap_continues_a_paragraph_with_a_space(tokenizer: Tokenizer) -> None:
    markdown = paragraph(*range(1, 8))

    chunks = chunk_document(document(markdown), DEFAULTS, tokenizer)

    assert chunks[1].text.startswith(f"{sentence(6)} {sentence(7)}")


def test_t183_the_page_is_where_the_chunks_own_text_starts(tokenizer: Tokenizer) -> None:
    first, second = paragraph(1, 2, 3, 4, 5), paragraph(6, 7)
    markdown = f"{first}\n\n<!-- PageBreak -->\n\n{second}"
    break_at = markdown.index("<!--")
    spans = [(3, 0, break_at - 2), (4, markdown.index(second), len(second))]

    chunks = chunk_document(document(markdown, spans), DEFAULTS, tokenizer)

    assert chunks[1].text.startswith(sentence(5))
    assert [chunk.page_number for chunk in chunks] == [3, 4]


def test_a_chunk_starting_between_spans_takes_the_earlier_page(tokenizer: Tokenizer) -> None:
    markdown = f"# Heading\n\n{paragraph(1)}"
    spans = [(2, 0, 3), (3, markdown.index("s1w0"), 30)]

    (chunk,) = chunk_document(document(markdown, spans), DEFAULTS, tokenizer)

    assert chunk.page_number == 2


# --- Tables ---------------------------------------------------------------------------------------


def table(rows: int, words: int = 8) -> str:
    cells = " ".join(["cell"] * words)
    body = "".join(f"<tr><td>row{index} {cells}</td></tr>" for index in range(rows))
    head = "<table><caption>Prices</caption><thead><tr><th>Name</th></tr></thead>"
    return f"{head}<tbody>{body}</tbody></table>"


def test_t183_a_table_under_the_limit_is_one_chunk_without_overlap(tokenizer: Tokenizer) -> None:
    markdown = "\n\n".join(["# T", paragraph(1, 2, 3, 4, 5), table(3), paragraph(6)])

    chunks = chunk_document(document(markdown), DEFAULTS, tokenizer)

    assert chunks[0].text == f"T\n\n{paragraph(1, 2, 3, 4, 5)}"
    assert chunks[1].text.startswith("<table><caption>Prices</caption>")
    assert chunks[1].text.count("<tr>") == 4
    assert chunks[2].text == paragraph(6)


def test_t183_a_long_table_splits_between_rows_repeating_its_header(tokenizer: Tokenizer) -> None:
    markdown = f"# Section\n\n{table(30)}"
    spans = [(1, 0, markdown.index("row15")), (2, markdown.index("row15"), len(markdown))]

    chunks = chunk_document(document(markdown, spans), DEFAULTS, tokenizer)

    assert len(chunks) > 2
    assert chunks[0].text.startswith("Section\n\n<table><caption>Prices</caption><thead>")
    for chunk in chunks:
        html = chunk.text.removeprefix("Section\n\n")
        assert html.startswith(
            "<table><caption>Prices</caption><thead><tr><th>Name</th></tr></thead>"
        )
        assert html.endswith("</table>")
        assert count_tokens(tokenizer, html) <= 256
    rows = "".join(chunk.text for chunk in chunks)
    assert all(f"row{index} " in rows for index in range(30))
    pages = [chunk.page_number for chunk in chunks]
    assert pages[0] == 1
    assert pages[-1] == 2
    assert pages == sorted(pages)


def test_a_single_row_above_the_limit_is_a_part_and_8000_tokens_cap_it(
    tokenizer: Tokenizer,
) -> None:
    huge = f"<table><tr><td>a</td></tr><tr><td>{' '.join(['w'] * 5000)}</td></tr></table>"

    chunks = chunk_document(document(huge), DEFAULTS, tokenizer)

    assert chunks[0].text == "<table><tr><td>a</td></tr></table>"
    assert len(chunks) == 3
    assert all(count_tokens(tokenizer, chunk.text) <= 8000 for chunk in chunks)


def test_a_document_of_only_comments_gives_no_chunk(tokenizer: Tokenizer) -> None:
    assert chunk_document(document("<!-- PageBreak -->\n\n# Lonely"), DEFAULTS, tokenizer) == []


@pytest.mark.parametrize("overlap", [0, 50])
def test_every_chunk_stays_within_target_plus_overlap(overlap: int, tokenizer: Tokenizer) -> None:
    params = RecursiveParametersV1(target_tokens=128, overlap_percent=overlap, table_max_tokens=256)
    markdown = "\n\n".join(
        [
            "# A",
            paragraph(*range(12)),
            "## B",
            paragraph(*range(12, 15)),
            paragraph(*range(15, 30)),
        ]
    )

    chunks = chunk_document(document(markdown), params, tokenizer)

    assert all(
        count_tokens(tokenizer, chunk.text) <= 128 + 128 * overlap // 100 for chunk in chunks
    )
