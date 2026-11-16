"""Unit tests of the page-wise extraction in the parsing child (`14-ingestion.md` sections 10.2 to
10.5; ADR 0047 items 3 to 5).

`unstructured` can't be installed here (it needs `torch`), so the library boundary is a fake
`unstructured.partition.pdf` module whose elements have the shape the library returns: a
`category`, a `text` and `metadata` with `page_number`, `text_as_html` and `category_depth`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pikepdf
import pytest
from pydantic import ValidationError

from docchat_ingestion.isolation.results import ExtractElement, ExtractPartV1, parse_partition
from docchat_ingestion.methods.pagewise import partition
from docchat_ingestion.methods.pagewise.partition import (
    CorruptPartError,
    assign_pages,
    build_part,
    map_element,
    partition_part,
)


def element(
    category: str,
    text: str = "",
    *,
    page: int | None = 1,
    html: str | None = None,
    depth: int | None = None,
) -> Any:
    return SimpleNamespace(
        category=category,
        text=text,
        metadata=SimpleNamespace(page_number=page, text_as_html=html, category_depth=depth),
    )


@pytest.mark.parametrize(
    ("category", "kind"),
    [
        ("Title", "title"),
        ("NarrativeText", "text"),
        ("ListItem", "list_item"),
        ("Header", "header"),
        ("Footer", "footer"),
        ("UncategorizedText", "other"),
        ("FigureCaption", "other"),
        ("Image", "other"),
        ("Formula", "other"),
        ("Address", "other"),
        ("EmailAddress", "other"),
        ("CodeSnippet", "other"),
        ("PageNumber", "other"),
    ],
)
def test_categories_map_to_kinds(category: str, kind: str) -> None:
    mapped = map_element(element(category, "  some text  "))

    assert mapped == ExtractElement.model_validate(
        {"kind": kind, "text": "some text", "tableHtml": None, "headingLevel": None}
    )


def test_a_title_takes_its_depth_plus_one() -> None:
    assert map_element(element("Title", "Intro", depth=0)) == ExtractElement(
        kind="title", text="Intro", table_html=None, heading_level=1
    )
    deep = map_element(element("Title", "Deep", depth=250))
    assert deep is not None
    assert deep.heading_level == 100
    assert map_element(element("NarrativeText", "x", depth=2)) == ExtractElement(
        kind="text", text="x", table_html=None, heading_level=None
    )


def test_a_table_carries_its_sanitised_html() -> None:
    mapped = map_element(
        element(
            "Table",
            "A 1",
            html='<table><tr><td onclick="x()">A</td><td colspan="0">1</td></tr></table>',
        )
    )

    assert mapped == ExtractElement(
        kind="table",
        text="A 1",
        table_html="<table><tbody><tr><td>A</td><td>1</td></tr></tbody></table>",
        heading_level=None,
    )


def test_a_table_without_html_is_text() -> None:
    assert map_element(element("Table", "A 1")) == ExtractElement(
        kind="text", text="A 1", table_html=None, heading_level=None
    )
    assert map_element(element("Table", "B", html="<script>x</script>")) == ExtractElement(
        kind="text", text="B", table_html=None, heading_level=None
    )


def test_an_empty_element_without_table_html_is_dropped() -> None:
    assert map_element(element("NarrativeText", "  \n ")) is None
    assert map_element(element("Table", "", html="<script>x()</script>")) is None
    # Table HTML keeps an element whatever its text; Enrich skips a table without cell text.
    kept = map_element(element("Table", "", html="<table></table>"))
    assert kept is not None
    assert kept.kind == "table"


def test_pages_get_the_part_offset_and_inherit_or_clamp() -> None:
    elements = [
        element("NarrativeText", "a", page=None),
        element("NarrativeText", "b", page=2),
        element("NarrativeText", "c", page=None),
        element("NarrativeText", "d", page=9),
        element("NarrativeText", "e", page=0),
    ]

    assert assign_pages(elements, first=41, last=43) == [41, 42, 42, 43, 41]


def test_build_part_writes_one_entry_per_page() -> None:
    part = build_part(
        [
            element("Title", "Heading", page=1, depth=0),
            element("NarrativeText", "", page=1),
            element("NarrativeText", "Body", page=2),
            element("Footer", "Footer", page=None),
        ],
        first=5,
        last=7,
        page_count=10,
    )

    assert [page.page_number for page in part.pages] == [5, 6, 7]
    assert [element.text for element in part.pages[0].elements] == ["Heading"]
    assert [element.text for element in part.pages[1].elements] == ["Body", "Footer"]
    assert part.pages[2].elements == []
    raw = part.model_dump_json(by_alias=True).encode("utf-8")
    assert parse_partition(raw, page_count=10, first=5, last=7) == part


def _pdf(path: Path, pages: int) -> Path:
    with pikepdf.new() as pdf:
        for _ in range(pages):
            pdf.add_blank_page()
        pdf.save(path)
    return path


class _FakeLibrary:
    def __init__(self, elements: list[Any]) -> None:
        self.elements = elements
        self.calls: list[dict[str, Any]] = []
        self.pages: list[int] = []

    def partition_pdf(self, **arguments: Any) -> list[Any]:
        self.calls.append(arguments)
        with pikepdf.open(arguments["filename"]) as part:
            self.pages.append(len(part.pages))
        return self.elements


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> _FakeLibrary:
    fake = _FakeLibrary([element("NarrativeText", "Page two text", page=1)])
    monkeypatch.setitem(sys.modules, partition.PARTITION_MODULE, fake)
    return fake


def test_partition_part_copies_the_part_and_calls_hi_res(
    library: _FakeLibrary, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.bin", 5)

    part = partition_part(
        source,
        first=2,
        last=4,
        page_count=5,
        parameters={"summarizeTables": True, "ocrLanguages": ["fra", "eng"], "pageCount": 5},
    )

    assert library.pages == [3]
    call = library.calls[0]
    assert {key: value for key, value in call.items() if key != "filename"} == {
        "strategy": "hi_res",
        "infer_table_structure": True,
        "languages": ["fra", "eng"],
    }
    assert Path(call["filename"]).parent == tmp_path
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.bin"]
    assert isinstance(part, ExtractPartV1)
    assert (part.first_page, part.last_page, part.page_count) == (2, 4, 5)
    assert part.pages[0].elements[0].text == "Page two text"


def test_partition_part_refuses_a_broken_pdf(library: _FakeLibrary, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"not a pdf")
    parameters: dict[str, object] = {"pageCount": 2}

    with pytest.raises(CorruptPartError, match="corrupt_document"):
        partition_part(source, first=1, last=2, page_count=2, parameters=parameters)
    with pytest.raises(CorruptPartError):
        partition_part(source, first=3, last=2, page_count=2, parameters=parameters)
    assert library.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.bin"]


def test_partition_part_validates_the_parameters(library: _FakeLibrary, tmp_path: Path) -> None:
    source = _pdf(tmp_path / "source.bin", 1)

    with pytest.raises(ValidationError):
        partition_part(source, first=1, last=1, page_count=1, parameters={"ocrLanguages": ["xxx"]})
    assert library.calls == []
