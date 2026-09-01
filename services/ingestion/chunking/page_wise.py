"""Page-wise chunker: one page per chunk.

Chunk IDs are a deterministic, sanitized "{filename}_{page}", so re-ingesting
a file overwrites its chunks instead of duplicating them.
"""

import hashlib
import re

from services.ingestion.parsers.base import ParsedPage

from .base import BaseChunker, Chunk


def _sanitize_id(raw: str) -> str:
    """Build a deterministic, collision-free Azure Search document key.

    Sanitization alone collapses distinct names ("a.b.pdf", "a b.pdf" and
    "a_b.pdf" all become "a_b_pdf"), which would let one document silently
    overwrite another's chunks in the same index. An 8-char hash of the raw
    string keeps keys unique per raw input while remaining deterministic,
    so re-ingesting a file still overwrites its own chunks.
    """
    sanitized = re.sub(r"[^0-9a-zA-Z_-]", "_", raw)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


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
