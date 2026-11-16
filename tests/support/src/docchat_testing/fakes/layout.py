"""`FakeLayoutAnalyzer`: the in-memory double of the recursive method's layout service
(`84-testing.md` sections 1.4 and 2.4; `14-ingestion.md` sections 11.1 and 11.7).

It implements the port `docchat_ingestion.methods.recursive.layout.LayoutAnalyzer`: `analyze(pdf)`
answers the `prebuilt-layout` result (API 2024-11-30, Markdown content, offsets in Unicode code
points) recorded for that PDF's SHA-256, or, for any other PDF, a result with one heading, one
paragraph and one HTML table per page, as the service writes tables in Markdown output. A PDF
holding an `/Encrypt` dictionary is refused as the service refuses a password-protected file, with
`MethodFailedError("encrypted_document")`; queued failures answer first. The contract suite of
`tests/integration/contracts/` runs the same cases against this fake and against the real layout
call over recorded responses, so the two can't drift.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import IO, Any, Final

from azure.ai.documentintelligence.models import AnalyzeResult

from docchat_core.ingestion.methods import MethodFailedError

API_VERSION: Final = "2024-11-30"
MODEL_ID: Final = "prebuilt-layout"
ENCRYPT_MARKER: Final = b"/Encrypt"
PAGE_MARKER: Final = b"/Type /Page"
"""The page objects a PDF holds; `/Type /Pages` (the page tree) is counted out."""


def page_count(pdf: bytes) -> int:
    """The number of page objects of a PDF, at least 1."""
    pages = pdf.count(PAGE_MARKER) - pdf.count(PAGE_MARKER + b"s")
    return max(pages, 1)


def layout_result(pages: int) -> dict[str, Any]:
    """A `prebuilt-layout` result of `pages` pages: per page one heading, one paragraph and one
    HTML table, each page's span counted in code points."""
    content = ""
    spans: list[dict[str, int]] = []
    for number in range(1, pages + 1):
        text = (
            f"# Section {number}\n\nThe rules of section {number}.\n\n"
            f"<table>\n<tr><th>Item</th><th>Limit</th></tr>\n"
            f"<tr><td>Meals</td><td>{number * 10}</td></tr>\n</table>\n\n"
        )
        spans.append({"offset": len(content), "length": len(text)})
        content += text
    return {
        "apiVersion": API_VERSION,
        "modelId": MODEL_ID,
        "stringIndexType": "unicodeCodePoint",
        "contentFormat": "markdown",
        "content": content,
        "pages": [
            {
                "pageNumber": number,
                "angle": 0,
                "width": 8.5,
                "height": 11,
                "unit": "inch",
                "spans": [span],
            }
            for number, span in enumerate(spans, start=1)
        ],
    }


@dataclass
class FakeLayoutAnalyzer:
    """The layout port over recorded results; every analysed PDF's hash is recorded."""

    recorded: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """`AnalyzeResult` JSON by the lower-case hex SHA-256 of the PDF it was recorded for."""
    failures: deque[MethodFailedError] = field(default_factory=deque)
    analysed: list[str] = field(default_factory=list)

    async def analyze(self, pdf: IO[bytes]) -> AnalyzeResult:
        data = pdf.read()
        digest = hashlib.sha256(data).hexdigest()
        self.analysed.append(digest)
        if self.failures:
            raise self.failures.popleft()
        if ENCRYPT_MARKER in data:
            raise MethodFailedError("encrypted_document")
        recorded = self.recorded.get(digest)
        return AnalyzeResult(dict(recorded) if recorded else layout_result(page_count(data)))
