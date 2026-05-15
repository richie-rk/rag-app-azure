"""Fetch a document from blob storage for citation display."""

import logging
import mimetypes

from services.shared.azure_clients import get_blob_service_client

logger = logging.getLogger(__name__)


def get_document(
    file_name: str,
    container: str,
) -> tuple[bytes, str, str]:
    """Download a document from blob storage for citation display.

    Returns (content_bytes, content_type, filename).
    """
    blob_service = get_blob_service_client()
    blob_client = blob_service.get_blob_client(container, file_name)

    download = blob_client.download_blob()
    content = download.readall()

    # Determine content type
    properties = blob_client.get_blob_properties()
    content_type = properties.content_settings.content_type

    if not content_type or content_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(file_name)
        content_type = guessed or "application/octet-stream"

    return content, content_type, file_name
