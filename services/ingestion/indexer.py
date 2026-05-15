"""Azure AI Search index creation and document upload."""

import hashlib
import logging

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from services.shared.azure_clients import get_search_client, get_search_index_client
from services.shared.config import get_settings

from .chunking.base import Chunk

logger = logging.getLogger(__name__)

_UPLOAD_BATCH_SIZE = 1000


def create_search_index(index_name: str) -> None:
    """Create the search index, or no-op if it already exists."""
    settings = get_settings()
    index_client = get_search_index_client()

    existing = [name for name in index_client.list_index_names()]
    if index_name in existing:
        logger.info("Index '%s' already exists, skipping creation", index_name)
        return

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
        ),
        SimpleField(
            name="sourcepage",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SearchableField(
            name="sourcefile",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchableField(
            name="sourcepath",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchableField(
            name="metadata_info",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="document_hash",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.embedding_dimensions,
            vector_search_profile_name="defaultProfile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="default",
                parameters=HnswParameters(
                    m=4,
                    ef_construction=400,
                    ef_search=500,
                    metric="cosine",
                ),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="defaultProfile",
                algorithm_configuration_name="default",
            )
        ],
    )

    semantic_config = SemanticConfiguration(
        name="default",
        prioritized_fields=SemanticPrioritizedFields(
            content_fields=[SemanticField(field_name="content")]
        ),
    )

    semantic_search = SemanticSearch(configurations=[semantic_config])

    index = SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )

    index_client.create_or_update_index(index)
    logger.info("Created search index '%s'", index_name)


def compute_file_hash(content: bytes) -> str:
    """Compute SHA-256 hash of file content for dedup detection."""
    return hashlib.sha256(content).hexdigest()


def remove_existing_chunks(index_name: str, source_file: str) -> int:
    """Delete all existing chunks for a file from the search index.

    Returns the number of chunks deleted.
    """
    search_client = get_search_client(index_name)

    # Query for all chunk IDs belonging to this file
    results = search_client.search(
        search_text="*",
        filter=f"sourcefile eq '{source_file}'",
        select=["id"],
        top=10000,
    )

    chunk_ids = [{"id": r["id"]} for r in results]
    if not chunk_ids:
        return 0

    # Batch delete
    for batch_start in range(0, len(chunk_ids), _UPLOAD_BATCH_SIZE):
        batch = chunk_ids[batch_start : batch_start + _UPLOAD_BATCH_SIZE]
        search_client.delete_documents(documents=batch)

    logger.info("Deleted %d existing chunks for '%s' in index '%s'", len(chunk_ids), source_file, index_name)
    return len(chunk_ids)


def upload_documents(
    index_name: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
    document_hash: str,
) -> int:
    """Upload chunks with embeddings to Azure AI Search in batches.

    Returns number of documents uploaded.
    """
    search_client = get_search_client(index_name)

    documents = []
    for chunk, embedding in zip(chunks, embeddings):
        documents.append(
            {
                "id": chunk.id,
                "content": chunk.content,
                "sourcepage": chunk.sourcepage,
                "sourcefile": chunk.sourcefile,
                "sourcepath": chunk.sourcepath or "",
                "metadata_info": chunk.metadata_info,
                "document_hash": document_hash,
                "content_vector": embedding,
            }
        )

    uploaded = 0
    for batch_start in range(0, len(documents), _UPLOAD_BATCH_SIZE):
        batch = documents[batch_start : batch_start + _UPLOAD_BATCH_SIZE]
        result = search_client.upload_documents(documents=batch)
        uploaded += sum(1 for r in result if r.succeeded)

    logger.info("Uploaded %d chunks to index '%s'", uploaded, index_name)
    return uploaded
