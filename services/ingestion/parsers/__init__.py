from .pdf_parser import PdfParser
from .docx_parser import DocxParser
from .pptx_parser import PptxParser
from .text_parser import TextParser

PARSER_MAP: dict[str, type] = {
    ".pdf": PdfParser,
    ".docx": DocxParser,
    ".pptx": PptxParser,
    ".txt": TextParser,
    ".md": TextParser,
    ".py": TextParser,
    ".js": TextParser,
    ".ts": TextParser,
    ".html": TextParser,
    ".json": TextParser,
    ".csv": TextParser,
}


def get_parser(extension: str):
    """Return the appropriate parser class for a file extension."""
    return PARSER_MAP.get(extension.lower(), TextParser)
