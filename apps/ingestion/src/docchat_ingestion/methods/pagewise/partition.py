"""The page-wise extraction of one part, run inside the parsing child (`14-ingestion.md` sections
10.2 to 10.5 and 15.6; ADR 0047 items 1 and 3 to 5).

`docchat_ingestion.isolation.parse_worker` calls `partition_part` after the child's system-call
filter is loaded and writes what it returns to `partition.json`. The child copies the part's
pages into a temporary PDF with `pikepdf`, partitions it with `unstructured`'s `partition_pdf`
(`hi_res`, table structure, the index's OCR languages; every other argument at the pinned
version's default), and maps the elements onto the extract part of section 6.14 item 1: each
element's kind, its page in the version's PDF, and each table's HTML after the sanitiser.

`unstructured` is imported inside `partition_part` only, so the activity process, which imports
this package's other modules, never loads the parsing library, and the import runs under the
filter. Everything above the library call is a pure function of the elements.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

import pikepdf

from docchat_core.ingestion.methods import PagewiseParametersV1
from docchat_ingestion.isolation.results import ExtractElement, ExtractPage, ExtractPartV1
from docchat_ingestion.methods.tables import sanitize_table_html

PARTITION_MODULE: Final = "unstructured.partition.pdf"
"""The module `partition_part` imports `partition_pdf` from."""
STRATEGY: Final = "hi_res"
PAGE_COUNT_PARAMETER: Final = "pageCount"
"""The key the runner adds to the page-wise parameters: the version's page count."""
HEADING_LEVEL_MAX: Final = 100

type ElementKind = Literal["text", "title", "list_item", "table", "header", "footer", "other"]

KINDS: Final[dict[str, ElementKind]] = {
    "Title": "title",
    "NarrativeText": "text",
    "ListItem": "list_item",
    "Table": "table",
    "Header": "header",
    "Footer": "footer",
}
"""The library categories with their own kind; every other category is `other`."""


class ElementMetadata(Protocol):
    """The metadata fields of an `unstructured` element the mapping reads."""

    @property
    def page_number(self) -> int | None: ...

    @property
    def text_as_html(self) -> str | None: ...

    @property
    def category_depth(self) -> int | None: ...


class Element(Protocol):
    """An `unstructured` element as `partition_pdf` returns it."""

    @property
    def category(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def metadata(self) -> ElementMetadata: ...


class CorruptPartError(Exception):
    """`pikepdf` couldn't open the version's PDF or copy the part's pages; the child reports
    `corrupt_document`."""

    def __init__(self) -> None:
        super().__init__("corrupt_document")


def map_element(element: Element) -> ExtractElement | None:
    """The work-file element of a library element (section 10.3), or `None` for one to drop:
    empty text after trimming and no table HTML."""
    kind: ElementKind = KINDS.get(element.category, "other")
    text = (element.text or "").strip()
    metadata = element.metadata
    table_html: str | None = None
    heading_level: int | None = None
    if kind == "table":
        source = metadata.text_as_html
        sanitised = sanitize_table_html(source) if source else ""
        if sanitised.strip():
            table_html = sanitised
        else:
            kind = "text"
    elif kind == "title" and metadata.category_depth is not None:
        heading_level = min(max(metadata.category_depth + 1, 1), HEADING_LEVEL_MAX)
    if not text and table_html is None:
        return None
    return ExtractElement(kind=kind, text=text, table_html=table_html, heading_level=heading_level)


def assign_pages(elements: Sequence[Element], *, first: int, last: int) -> list[int]:
    """The page of each element in the version's PDF (section 10.4): its own page in the part
    plus `first - 1`; without one, the page of the element before it, or `first` for the first;
    clamped into `first` to `last`."""
    pages: list[int] = []
    previous = first
    for element in elements:
        number = element.metadata.page_number
        page = previous if number is None else number + first - 1
        page = min(max(page, first), last)
        pages.append(page)
        previous = page
    return pages


def build_part(
    elements: Sequence[Element], *, first: int, last: int, page_count: int
) -> ExtractPartV1:
    """The extract part of pages `first` to `last`: one entry per page of the part, in page
    order, each holding its elements in the parser's reading order."""
    by_page: dict[int, list[ExtractElement]] = {page: [] for page in range(first, last + 1)}
    for element, page in zip(elements, assign_pages(elements, first=first, last=last), strict=True):
        mapped = map_element(element)
        if mapped is not None:
            by_page[page].append(mapped)
    return ExtractPartV1(
        format_version=1,
        method="pagewise",
        page_count=page_count,
        first_page=first,
        last_page=last,
        pages=[ExtractPage(page_number=page, elements=items) for page, items in by_page.items()],
    )


def copy_pages(pdf: Path, target: Path, *, first: int, last: int) -> None:
    """Pages `first` to `last` of `pdf` into a new PDF at `target`."""
    try:
        with pikepdf.open(pdf) as source, pikepdf.new() as part:
            part.pages.extend(source.pages[first - 1 : last])
            part.save(target)
    except (pikepdf.PdfError, IndexError, ValueError):
        raise CorruptPartError from None


def partition_part(
    pdf: Path, *, first: int, last: int, page_count: int, parameters: dict[str, object]
) -> ExtractPartV1:
    """The `partition` command's work on the version's PDF `pdf`, in the child's scratch
    directory: the part's pages copied with `pikepdf`, partitioned, mapped and returned."""
    if not 1 <= first <= last <= page_count:
        raise CorruptPartError
    method_parameters = {
        name: value for name, value in parameters.items() if name != PAGE_COUNT_PARAMETER
    }
    # Read by camelCase names, as the descriptor reads stored parameters.
    validated = PagewiseParametersV1.model_validate(method_parameters, by_name=False)
    descriptor, name = tempfile.mkstemp(suffix=".pdf", dir=pdf.parent)
    os.close(descriptor)
    part_file = Path(name)
    try:
        copy_pages(pdf, part_file, first=first, last=last)
        # Imported here, under the child's filter, never by the activity process (module docstring).
        from unstructured.partition.pdf import partition_pdf  # noqa: PLC0415

        elements = partition_pdf(
            filename=str(part_file),
            strategy=STRATEGY,
            infer_table_structure=True,
            languages=list(validated.ocr_languages),
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            part_file.unlink()
    return build_part(elements, first=first, last=last, page_count=page_count)
