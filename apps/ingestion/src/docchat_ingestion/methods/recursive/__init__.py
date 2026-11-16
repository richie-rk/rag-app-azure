"""The recursive method, `recursive` version 1 (`14-ingestion.md` section 11; ADR 0048): headings
first, then paragraphs, then sentences, up to a target size with overlap, on the Markdown output
of Document Intelligence's `prebuilt-layout`. `IMPLEMENTATION` is what
`docchat_ingestion.function_app` registers."""

from docchat_ingestion.methods.recursive.method import IMPLEMENTATION

__all__ = ["IMPLEMENTATION"]
