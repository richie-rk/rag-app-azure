"""Page-wise chunker: one page per chunk.

Chunk IDs are a deterministic, sanitized "{filename}_{page}", so re-ingesting
a file overwrites its chunks instead of duplicating them.
"""

import re

from services.ingestion.parsers.base import ParsedPage

from .base import BaseChunker, Chunk


def _sanitize_id(raw: str) -> str:
    """Replace non-alphanumeric chars (except - and _) with underscore."""
    return re.sub(r"[^0-9a-zA-Z_-]", "_", raw)


def _blob_name_from_file_page(filename: str, page: int) -> str:
    """Generate a sourcepage identifier like 'document-3.pdf'."""
    base, _, ext = filename.rpartition(".")
    if ext:
        return f"{base}-{page}.{ext}"
    return f"{filename}-{page}"


class PageWiseChunker(BaseChunker):
    """One page per chunk."""

    def chunk(self, pages: list[ParsedPage], filename: str) -> list[Chunk]:
        chunks = []
        for page in pages:
            chunk_id = _sanitize_id(f"{filename}_{page.page_number}")
            sourcepage = _blob_name_from_file_page(filename, page.page_number)

            chunks.append(
                Chunk(
                    id=chunk_id,
                    content=page.content,
                    sourcepage=sourcepage,
                    sourcefile=filename,
                    metadata_info=str(page.metadata or {}),
                )
            )
        return chunks
