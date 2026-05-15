"""Abstract chunker interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from services.ingestion.parsers.base import ParsedPage


@dataclass
class Chunk:
    """A single chunk ready for embedding and indexing."""
    id: str
    content: str
    sourcepage: str
    sourcefile: str
    sourcepath: str | None = None
    metadata_info: str = ""


class BaseChunker(ABC):
    @abstractmethod
    def chunk(self, pages: list[ParsedPage], filename: str) -> list[Chunk]:
        """Take parsed pages and return chunks with metadata."""
        ...
