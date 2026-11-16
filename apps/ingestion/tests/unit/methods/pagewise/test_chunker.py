"""Unit tests of the page-wise chunker (T-182 in part, T-209 in part; `14-ingestion.md` sections
10.5 to 10.7 and 10.9; ADR 0047 items 6 and 7)."""

from __future__ import annotations

import pytest

from docchat_ingestion.isolation.results import ExtractElement, ExtractPage, ExtractPartV1
from docchat_ingestion.methods import chunks
from docchat_ingestion.methods.chunks import Chunk
from docchat_ingestion.methods.pagewise import chunker
from docchat_ingestion.methods.pagewise.chunker import chunk_pages, element_text, page_texts
from docchat_ingestion.methods.tables import table_sha256
from docchat_ingestion.methods.text import Tokenizer, count_tokens


def element(kind: str, text: str = "", html: str | None = None) -> ExtractElement:
    return ExtractElement(kind=kind, text=text, table_html=html, heading_level=None)  # type: ignore[arg-type]


def part(
    pages: dict[int, list[ExtractElement]], first: int, last: int, count: int
) -> ExtractPartV1:
    return ExtractPartV1(
        format_version=1,
        method="pagewise",
        page_count=count,
        first_page=first,
        last_page=last,
        pages=[ExtractPage(page_number=page, elements=items) for page, items in pages.items()],
    )


TABLE = (
    "<table><thead><tr><th>Item</th><th>Price</th></tr></thead>"
    "<tbody><tr><td>Tea</td><td>2</td></tr></tbody></table>"
)


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """A 40-token cap, so oversize pages stay small."""
    monkeypatch.setattr(chunker, "MAX_CHUNK_TOKENS", 40)
    monkeypatch.setattr(chunks, "MAX_CHUNK_TOKENS", 40)
    return 40


def test_t182_one_chunk_per_page_in_page_order(tokenizer: Tokenizer) -> None:
    parts = [
        part(
            {
                3: [element("header", "Header"), element("text", "Third page.")],
                4: [],
            },
            3,
            4,
            4,
        ),
        part(
            {
                1: [
                    element("title", "Overview"),
                    element("text", "First paragraph."),
                    element("list_item", "A point"),
                    element("footer", "Footer 1"),
                    element("other", "Figure 1"),
                ],
                2: [element("text", "Second page.")],
            },
            1,
            2,
            4,
        ),
    ]

    result = chunk_pages(parts, {}, tokenizer)

    assert result == [
        Chunk(
            "Overview\n\nFirst paragraph.\n\nA point\n\nFooter 1\n\nFigure 1",
            ("Overview",),
            1,
        ),
        Chunk("Second page.", (), 2),
        Chunk("Header\n\nThird page.", (), 3),
    ]


def test_t182_a_table_is_its_summary_then_its_html(tokenizer: Tokenizer) -> None:
    summaries = {table_sha256(TABLE): "Prices of drinks in EUR."}
    other = TABLE.replace("Tea", "Coffee")
    page = part({1: [element("table", "Item Price", TABLE), element("table", "x", other)]}, 1, 1, 1)

    result = chunk_pages([page], summaries, tokenizer)

    assert result[0].text == f"Prices of drinks in EUR.\n\n{TABLE}\n\n{other}"
    assert element_text(element("table", "fallback"), summaries) == "fallback"


def test_t209_the_heading_path_comes_from_the_titles_cleaned(tokenizer: Tokenizer) -> None:
    titles = [element("title", "Re\u202eport\u200b"), element("title", "\u200b\u2066")]
    titles += [element("title", f"Title {index}") for index in range(9)]
    page = part({1: [*titles, element("text", "Body.")]}, 1, 1, 1)

    (chunk,) = chunk_pages([page], {}, tokenizer)

    assert chunk.heading_path == ("Report", *(f"Title {index}" for index in range(7)))
    assert all("\u202e" not in entry and "\u200b" not in entry for entry in chunk.heading_path)


def test_t182_a_20000_token_page_splits_into_ordered_sub_chunks(tokenizer: Tokenizer) -> None:
    paragraphs = [
        element("text", " ".join(f"w{index}x{word}" for word in range(250))) for index in range(40)
    ]
    page = part({5: [element("title", "Long page"), *paragraphs]}, 5, 5, 9)
    assert (
        count_tokens(tokenizer, "\n\n".join(element_text(e, {}) for e in page.pages[0].elements))
        > 20_000
    )

    result = chunk_pages([page], {}, tokenizer)

    assert len(result) == 3
    assert all(count_tokens(tokenizer, chunk.text) <= 8000 for chunk in result)
    assert {(chunk.page_number, chunk.heading_path) for chunk in result} == {(5, ("Long page",))}
    assert result[0].text.startswith("Long page\n\nw0x0 ")
    assert result[-1].text.endswith("w39x249")
    positions = [int(chunk.text.split("w", 2)[-1].split("x")[0]) for chunk in result[1:]]
    assert positions == sorted(positions)


def test_elements_fill_sub_chunks_whole(tokenizer: Tokenizer, small_cap: int) -> None:
    texts = ["one two three four five", "six seven eight nine ten", "eleven twelve"]

    result = page_texts([element("text", text) for text in texts * 3], {}, tokenizer)

    assert all(count_tokens(tokenizer, text) <= small_cap for text in result)
    assert "\n\n".join(result) == "\n\n".join(texts * 3)


def test_one_long_element_splits_at_sentences_then_tokens(
    tokenizer: Tokenizer, small_cap: int
) -> None:
    sentences = [f"Sentence {index} has a few words." for index in range(6)]
    long_sentence = " ".join(["word"] * 30)
    elements = [
        element("text", "Before."),
        element("text", " ".join(sentences)),
        element("text", long_sentence),
        element("text", "After."),
    ]

    result = page_texts(elements, {}, tokenizer)

    assert result[0] == "Before."
    assert all(count_tokens(tokenizer, text) <= small_cap for text in result)
    assert " ".join(result[1:3]) == " ".join(sentences)
    assert "".join(result[3:5]) == long_sentence
    assert result[-1] == "After."


def test_one_long_table_splits_between_rows_with_its_header(
    tokenizer: Tokenizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The header table alone is 27 tokens and each row 17, so two rows fit in a part.
    small_cap = 80
    monkeypatch.setattr(chunker, "MAX_CHUNK_TOKENS", small_cap)
    rows = "".join(f"<tr><td>row {index}</td></tr>" for index in range(12))
    html = f"<table><thead><tr><th>H</th></tr></thead><tbody>{rows}</tbody></table>"
    summaries = {table_sha256(html): "Rows."}

    result = page_texts([element("table", "t", html)], summaries, tokenizer)

    assert len(result) > 1
    assert result[0].startswith("Rows.\n\n<table><thead><tr><th>H</th></tr></thead>")
    assert all(text.count("<thead><tr><th>H</th></tr></thead>") == 1 for text in result)
    assert sum(text.count("<td>row") for text in result) == 12
    assert all(count_tokens(tokenizer, text) <= small_cap for text in result)


def test_a_long_table_without_rows_is_cut_by_tokens(tokenizer: Tokenizer, small_cap: int) -> None:
    html = "<table>" + " ".join(["cell"] * 30) + "</table>"

    result = page_texts([element("table", "t", html)], {}, tokenizer)

    assert "".join(result) == html
    assert all(count_tokens(tokenizer, text) <= small_cap for text in result)
