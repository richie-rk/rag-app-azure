from __future__ import annotations

import hashlib
import io

import pytest

from docchat_core.ingestion.methods import MethodFailedError
from docchat_testing.fakes.layout import FakeLayoutAnalyzer, layout_result, page_count

TWO_PAGES = (
    b"%PDF-1.7\n1 0 obj << /Type /Pages >>\n"
    b"2 0 obj << /Type /Page >>\n3 0 obj << /Type /Page >>\n%%EOF\n"
)


def test_pages_are_counted_without_the_page_tree() -> None:
    assert page_count(TWO_PAGES) == 2
    assert page_count(b"%PDF-1.7\n%%EOF\n") == 1


def test_each_page_holds_a_table_and_its_span_counts_code_points() -> None:
    result = layout_result(3)

    assert result["contentFormat"] == "markdown"
    assert result["stringIndexType"] == "unicodeCodePoint"
    assert result["content"].count("<table>") == 3
    spans = [page["spans"][0] for page in result["pages"]]
    assert spans[0]["offset"] == 0
    assert spans[-1]["offset"] + spans[-1]["length"] == len(result["content"])


async def test_an_unknown_pdf_gets_a_page_per_page_object() -> None:
    analyzer = FakeLayoutAnalyzer()

    result = await analyzer.analyze(io.BytesIO(TWO_PAGES))

    assert [page.page_number for page in result.pages or []] == [1, 2]
    assert analyzer.analysed == [hashlib.sha256(TWO_PAGES).hexdigest()]


async def test_a_recorded_result_answers_its_pdf() -> None:
    recorded = layout_result(1) | {"content": "# Recorded\n"}
    analyzer = FakeLayoutAnalyzer(recorded={hashlib.sha256(TWO_PAGES).hexdigest(): recorded})

    result = await analyzer.analyze(io.BytesIO(TWO_PAGES))

    assert result.content == "# Recorded\n"


async def test_an_encrypted_pdf_and_queued_failures_are_refused() -> None:
    analyzer = FakeLayoutAnalyzer()
    analyzer.failures.append(MethodFailedError("docintel_unavailable"))

    with pytest.raises(MethodFailedError) as queued:
        await analyzer.analyze(io.BytesIO(TWO_PAGES))
    with pytest.raises(MethodFailedError) as encrypted:
        await analyzer.analyze(io.BytesIO(b"%PDF-1.7\ntrailer << /Encrypt 5 0 R >>\n%%EOF\n"))

    assert (queued.value.code, encrypted.value.code) == (
        "docintel_unavailable",
        "encrypted_document",
    )
