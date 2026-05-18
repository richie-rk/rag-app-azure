"""Fetch a document from the shared blob container for citation display.

The caller (function_app.get_document_fn) runs the access check first; this
function only downloads the blob and classifies it for safe delivery.
"""

import logging
import os
from urllib.parse import quote

from services.shared.azure_clients import get_blob_service_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)

# Extensions safe to render inline. Anything else is forced to download as
# application/octet-stream, so an HTML/SVG/XML blob cannot execute as script
# in the app's origin even when a citation link opens it directly.
_INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def get_document(file_name: str) -> tuple[bytes, str, str]:
    """Download a document for citation display.

    Returns (content_bytes, content_type, content_disposition).
    """
    settings = get_settings()
    blob_service = get_blob_service_client()
    blob_client = blob_service.get_blob_client(
        settings.default_blob_container, file_name
    )

    content = blob_client.download_blob().readall()

    # Classify by extension, never by the blob's stored content_type: whoever
    # uploaded the blob controls that value, and a lie there is the XSS vector.
    ext = os.path.splitext(file_name)[1].lower()
    if ext in _INLINE_TYPES:
        content_type = _INLINE_TYPES[ext]
        disposition_type = "inline"
    else:
        content_type = "application/octet-stream"
        disposition_type = "attachment"

    # RFC 5987/6266: quote(safe="") percent-encodes quotes, CR and LF, so a
    # crafted filename cannot inject extra response headers.
    display_name = os.path.basename(file_name)
    disposition = f"{disposition_type}; filename*=UTF-8''{quote(display_name, safe='')}"

    return content, content_type, disposition
