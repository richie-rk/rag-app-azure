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
from services.shared.odata import odata_escape

from .chunking.base import Chunk

logger = logging.getLogger(__name__)

# Azure AI Search service limits (as of 2024-07-01 API):
#   - $top on a query is capped at 1000
#   - an indexing request may contain at most 1000 docs AND at most ~16 MB
# Vector docs are large (1536 floats ≈ 20-30 KB each), so batches are capped
# by estimated payload size as well as count.
_DELETE_PAGE_SIZE = 1000
_UPLOAD_MAX_DOCS = 500
_UPLOAD_MAX_BYTES = 12 * 1024 * 1024  # headroom under the 16 MB limit


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

    Pages through matches in blocks of _DELETE_PAGE_SIZE ($top is capped at
    1000 by the service; the previous top=10000 both errored and would have
    silently stranded chunks of very large documents). The loop is bounded
    as a guard against eventual-consistency re-reads.

    Returns the number of chunks deleted.
    """
    search_client = get_search_client(index_name)
    total_deleted = 0

    for _ in range(100):  # safety bound: 100k chunks per file is beyond any expected doc
        results = search_client.search(
            search_text="*",
            filter=f"sourcefile eq '{odata_escape(source_file)}'",
            select=["id"],
            top=_DELETE_PAGE_SIZE,
        )
        chunk_ids = [{"id": r["id"]} for r in results]
        if not chunk_ids:
            break
        search_client.delete_documents(documents=chunk_ids)
        total_deleted += len(chunk_ids)
        if len(chunk_ids) < _DELETE_PAGE_SIZE:
            break

    if total_deleted:
        logger.info(
            "Deleted %d existing chunks for '%s' in index '%s'",
            total_deleted, source_file, index_name,
        )
    return total_deleted


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

    # Batch by estimated payload size as well as count: vector docs at 1536
    # dims are ~20-30 KB each, so 1000-doc batches exceeded the service's
    # 16 MB indexing payload limit.
    per_vector_bytes = len(documents[0]["content_vector"]) * 15 if documents else 0

    uploaded = 0
    failed: list[tuple[str, str]] = []
    batch: list[dict] = []
    batch_bytes = 0

    def _flush() -> None:
        nonlocal uploaded, batch, batch_bytes
        if not batch:
            return
        result = search_client.upload_documents(documents=batch)
        for r in result:
            if r.succeeded:
                uploaded += 1
            else:
                failed.append((r.key, r.error_message or "unknown error"))
        batch = []
        batch_bytes = 0

    for doc in documents:
        doc_bytes = len(doc["content"]) + per_vector_bytes + 500
        if batch and (
            len(batch) >= _UPLOAD_MAX_DOCS or batch_bytes + doc_bytes > _UPLOAD_MAX_BYTES
        ):
            _flush()
        batch.append(doc)
        batch_bytes += doc_bytes
    _flush()

    if failed:
        # Previously failures were silently counted away, leaving a partial
        # index behind a lower chunk count. Log each and fail the file so the
        # audit row records the error and a retry re-processes it (chunk IDs
        # are deterministic, so retries overwrite rather than duplicate).
        for key, err in failed[:20]:
            logger.error("Failed to index chunk '%s': %s", key, err)
        raise RuntimeError(
            f"{len(failed)} of {len(documents)} chunks failed to index "
            f"(first: {failed[0][0]}: {failed[0][1]})"
        )

    logger.info("Uploaded %d chunks to index '%s'", uploaded, index_name)
    return uploaded
