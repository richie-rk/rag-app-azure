"""Per-file ingestion pipeline, invoked as the process_file Durable activity.

Files already indexed under the same content hash are skipped, so re-running
ingestion is idempotent.
"""

import logging
import os
import tempfile

from services.shared.config import get_settings
from services.shared.azure_clients import get_blob_service_client
from services.shared.database import get_session_factory
from services.shared.models import IngestionAudit, Project

from .chunking import get_chunker
from .embedding import embed_texts
from .indexer import (
    compute_file_hash,
    create_search_index,
    remove_existing_chunks,
    upload_documents,
)
from .parsers import get_parser

logger = logging.getLogger(__name__)


def process_file(
    project_id: int,
    index_name: str,
    container_name: str,
    blob_name: str,
    chunking_strategy: str = "page_wise",
) -> dict:
    """Process a single file: download, parse, chunk, embed, index.

    Returns a status dict with processing results.
    """
    settings = get_settings()
    blob_service = get_blob_service_client()
    # blob_name is the project-prefixed key ("{project_id}/report.pdf"); it is
    # the file's identity in the search index (sourcefile) and ingestion_audit,
    # so a citation resolves back through get_document. basename is only for
    # picking a parser by extension and naming the temp file.
    filename = os.path.basename(blob_name)
    _, ext = os.path.splitext(filename)

    # Ensure index exists
    create_search_index(index_name)

    # Download blob to temp file
    blob_client = blob_service.get_blob_client(container_name, blob_name)
    blob_data = blob_client.download_blob().readall()

    # Compute hash for dedup
    file_hash = compute_file_hash(blob_data)

    # Check if this exact file has already been indexed
    session_factory = get_session_factory()
    with session_factory() as session:
        existing = (
            session.query(IngestionAudit)
            .filter(
                IngestionAudit.project_id == project_id,
                IngestionAudit.source_file == blob_name,
                IngestionAudit.document_hash == file_hash,
                IngestionAudit.status == "completed",
            )
            .first()
        )
        if existing:
            logger.info("File '%s' already indexed with same hash, skipping", blob_name)
            return {"file": blob_name, "status": "skipped", "reason": "already indexed"}

    # Write to temp file for parsing
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(blob_data)
        tmp_path = tmp.name

    try:
        # Parse
        parser_cls = get_parser(ext)
        parser = parser_cls()
        pages = parser.parse(tmp_path, filename)

        if not pages:
            _log_audit(project_id, blob_name, "failed", 0, file_hash, "No content extracted")
            return {"file": blob_name, "status": "failed", "reason": "no content extracted"}

        # Chunk
        chunker_cls = get_chunker(chunking_strategy)
        chunker = chunker_cls()
        chunks = chunker.chunk(pages, blob_name)

        if not chunks:
            _log_audit(project_id, blob_name, "failed", 0, file_hash, "No chunks produced")
            return {"file": blob_name, "status": "failed", "reason": "no chunks produced"}

        # Embed
        texts = [c.content for c in chunks]
        embeddings = embed_texts(texts)

        # Everything that can fail expensively (parse, chunk, embed) has now
        # succeeded, so it is safe to drop the previous version's chunks.
        # Deleting any earlier would erase the document from the index if a
        # later step failed. Upload overwrites same-ID chunks anyway; this
        # delete only clears stale IDs (e.g. pages removed in a new version).
        remove_existing_chunks(index_name, blob_name)

        # Upload to index
        uploaded = upload_documents(index_name, chunks, embeddings, file_hash)

        # Log success
        _log_audit(project_id, blob_name, "completed", uploaded, file_hash)

        return {"file": blob_name, "status": "completed", "chunks": uploaded}

    except Exception as exc:
        logger.exception("Error processing file '%s'", blob_name)
        _log_audit(project_id, blob_name, "failed", 0, file_hash, str(exc))
        return {"file": blob_name, "status": "failed", "reason": str(exc)}

    finally:
        os.unlink(tmp_path)


def _log_audit(
    project_id: int,
    source_file: str,
    status: str,
    chunk_count: int,
    document_hash: str,
    error_message: str | None = None,
) -> None:
    """Write an audit log entry to the database."""
    session_factory = get_session_factory()
    with session_factory() as session:
        audit = IngestionAudit(
            project_id=project_id,
            source_file=source_file,
            status=status,
            chunk_count=chunk_count,
            document_hash=document_hash,
            error_message=error_message,
        )
        session.add(audit)
        session.commit()
