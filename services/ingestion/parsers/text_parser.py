"""Plain text / code file parser."""

from .base import BaseParser, ParsedPage

_MAX_CHARS_PER_CHUNK = 3000


class TextParser(BaseParser):
    def parse(self, file_path: str, file_name: str) -> list[ParsedPage]:
        """Parse a text file into chunks of ~3000 characters."""
        with open(file_path, encoding="utf-8", errors="replace") as f:
            text = f.read()

        if not text.strip():
            return []

        pages = []
        page_num = 1
        start = 0

        while start < len(text):
            end = min(start + _MAX_CHARS_PER_CHUNK, len(text))

            # Try to break at a newline boundary
            if end < len(text):
                nl = text.rfind("\n", start, end)
                if nl > start:
                    end = nl + 1

            chunk = text[start:end].strip()
            if chunk:
                pages.append(
                    ParsedPage(
                        page_number=page_num,
                        content=chunk,
                        metadata={"source": file_name, "page": page_num},
                    )
                )
                page_num += 1

            start = end

        return pages
