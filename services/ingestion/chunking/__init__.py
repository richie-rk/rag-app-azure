from .page_wise import PageWiseChunker

CHUNKER_MAP: dict[str, type] = {
    "page_wise": PageWiseChunker,
}


def get_chunker(strategy: str = "page_wise"):
    """Return the appropriate chunker class for a strategy name."""
    return CHUNKER_MAP.get(strategy, PageWiseChunker)
