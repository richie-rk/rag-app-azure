"""DOCX parser using python-docx."""

from docx import Document

from .base import BaseParser, ParsedPage


class DocxParser(BaseParser):
    def parse(self, file_path: str, file_name: str) -> list[ParsedPage]:
        """Parse DOCX into pages. Since DOCX has no page concept, each
        paragraph group (split by headings or every ~3000 chars) becomes a page."""
        doc = Document(file_path)
        pages = []
        current_text = ""
        page_num = 1
        max_chars = 3000

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            is_heading = para.style and para.style.name.startswith("Heading")

            if is_heading and current_text:
                pages.append(
                    ParsedPage(
                        page_number=page_num,
                        content=current_text.strip(),
                        metadata={"source": file_name, "page": page_num},
                    )
                )
                page_num += 1
                current_text = text + "\n"
            elif len(current_text) + len(text) > max_chars:
                pages.append(
                    ParsedPage(
                        page_number=page_num,
                        content=current_text.strip(),
                        metadata={"source": file_name, "page": page_num},
                    )
                )
                page_num += 1
                current_text = text + "\n"
            else:
                current_text += text + "\n"

        if current_text.strip():
            pages.append(
                ParsedPage(
                    page_number=page_num,
                    content=current_text.strip(),
                    metadata={"source": file_name, "page": page_num},
                )
            )

        return pages
