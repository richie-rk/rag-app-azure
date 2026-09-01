"""Hybrid query against Azure AI Search: vector + full-text + semantic reranking."""

import logging

from azure.search.documents.models import (
    QueryType,
    VectorizedQuery,
)

from services.shared.azure_clients import get_openai_client, get_search_client
from services.shared.config import get_settings
from services.shared.odata import odata_escape

logger = logging.getLogger(__name__)


def _embed_query(query: str) -> list[float]:
    """Embed the query client-side with the same model used at ingestion.

    The index's vector profile has no vectorizer attached, so server-side
    embedding (VectorizableTextQuery) is not available; we must send a
    pre-computed vector via VectorizedQuery.
    """
    settings = get_settings()
    client = get_openai_client()
    response = client.embeddings.create(
        input=query,
        model=settings.embedding_deployment,
    )
    return response.data[0].embedding


def hybrid_search(
    query: str,
    index_name: str,
    top_k: int | None = None,
    file_filter: str | None = None,
) -> list[dict]:
    """Run hybrid search: vector + full-text + semantic reranker.

    Returns list of {content, sourcepage, sourcefile, score, id}.
    """
    settings = get_settings()
    search_client = get_search_client(index_name)
    k = top_k or settings.default_top_k

    # Build vector query using the same embedding model as ingestion
    vector_query = VectorizedQuery(
        vector=_embed_query(query),
        k_nearest_neighbors=k,
        fields="content_vector",
    )

    # Build filter if file_name is specified
    search_filter = None
    if file_filter:
        search_filter = f"sourcefile eq '{odata_escape(file_filter)}'"

    results = search_client.search(
        search_text=query,
        vector_queries=[vector_query],
        query_type=QueryType.SEMANTIC,
        query_language="en-us",
        query_speller="lexicon",
        semantic_configuration_name="default",
        top=k,
        filter=search_filter,
        select=["id", "content", "sourcepage", "sourcefile"],
    )

    documents = []
    for i, result in enumerate(results):
        documents.append(
            {
                "content": result.get("content", ""),
                "sourcepage": result.get("sourcepage", ""),
                "sourcefile": result.get("sourcefile", ""),
                "id": str(i),
                "score": result.get("@search.score", 0.0),
            }
        )

    logger.info("Hybrid search returned %d results for index=%s", len(documents), index_name)
    return documents
