"""Abstract parser interface for document parsing."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ParsedPage:
    """A single parsed page/section from a document."""
    page_number: int
    content: str
    metadata: dict | None = None


class BaseParser(ABC):
    @abstractmethod
    def parse(self, file_path: str, file_name: str) -> list[ParsedPage]:
        """Parse a document file and return a list of pages."""
        ...
