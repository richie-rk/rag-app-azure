"""PDF parser using PyMuPDF (fitz).

Replicates Max AI Route B local parsing behavior: one page = one parsed section.
No torch, no unstructured, no 7 PDF libraries — just PyMuPDF.
"""

import fitz  # PyMuPDF

from .base import BaseParser, ParsedPage


class PdfParser(BaseParser):
    def parse(self, file_path: str, file_name: str) -> list[ParsedPage]:
        """Parse PDF into one ParsedPage per page."""
        pages = []
        doc = fitz.open(file_path)
        try:
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text("text")

                # Extract tables as text blocks
                tables = page.find_tables()
                table_text = ""
                if tables and tables.tables:
                    for table in tables.tables:
                        try:
                            df = table.to_pandas()
                            table_text += "\n" + df.to_string(index=False)
                        except Exception:
                            pass

                content = text.strip()
                if table_text:
                    content += "\n\n[Tables]\n" + table_text.strip()

                if content:
                    pages.append(
                        ParsedPage(
                            page_number=page_num + 1,
                            content=content,
                            metadata={"source": file_name, "page": page_num + 1},
                        )
                    )
        finally:
            doc.close()

        return pages
