"""Upload a document into the shared blob container for a project.

Files are keyed "{project_id}/{filename}" inside the single shared container
(config.default_blob_container). The project_id prefix scopes ingestion, which
lists blobs by prefix, to one project, and it is also the file's identity in
the search index and ingestion_audit so citations resolve through get_document.
See docs/adr/0004-shared-container-project-prefix.md.
"""

import logging
import os

from services.shared.azure_clients import get_blob_service_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)


def upload_document(project_id: int, file_name: str, data: bytes) -> dict:
    """Write an uploaded file to documents/{project_id}/{basename}."""
    settings = get_settings()
    container_name = settings.default_blob_container

    # file_name is whatever the browser put in the multipart part, so drop any
    # directory components (either separator) before it becomes a blob key.
    safe_name = os.path.basename((file_name or "").replace("\\", "/"))
    if not safe_name:
        return {"error": "Invalid file name", "status": 400}

    blob_prefix = f"{project_id}/"
    blob_name = f"{blob_prefix}{safe_name}"

    blob_service = get_blob_service_client()
    container_client = blob_service.get_container_client(container_name)
    if not container_client.exists():
        container_client.create_container()

    container_client.get_blob_client(blob_name).upload_blob(data, overwrite=True)

    logger.info("Uploaded '%s' to container '%s'", blob_name, container_name)
    return {
        "file_name": safe_name,
        "blob_name": blob_name,
        "blob_prefix": blob_prefix,
        "container": container_name,
        "status": 201,
    }
