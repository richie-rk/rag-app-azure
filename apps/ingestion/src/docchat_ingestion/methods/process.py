"""The worker process's side of the methods' ports (`14-ingestion.md` sections 4.4 rule 4, 9.1
and 18.6; ADR 0113).

`docchat_ingestion.function_app` registers each method's `IMPLEMENTATION` at import, before any
client exists, so the implementations resolve their ports per call: from the process's one open
`ClientRegistry`, which the activities' runtime opens at the first invocation, and from the app
settings the start-up checks validated.
"""

from __future__ import annotations

import functools

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.settings import IngestionAppSettings
from docchat_core.settings import load_settings
from docchat_ingestion.methods.store import BlobStore


@functools.cache
def app_settings() -> IngestionAppSettings:
    """The ingestion app settings, read once per process."""
    return load_settings(IngestionAppSettings)


def process_store() -> BlobStore:
    """The blob store over the open registry's container clients."""
    return BlobStore(ClientRegistry.current().blob_container)
