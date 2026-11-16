"""The Cosmos DB container definitions and document models (`80-data-model.md` section 3;
`13-conversations-and-memory.md`; `12-chat-pipeline.md` section 3; ADR 0030, ADR 0071).

`conversations` holds the session, turn and turn-trace models and the derived identifiers,
`memories` the memory document, `usage` the counters, windows and leases. The repositories that
read and write them sit beside them.
"""

from typing import Final

from docchat_core.cosmos.base import CosmosDocument, CosmosModel, DocumentTooLargeError
from docchat_core.cosmos.containers import (
    CONTAINERS,
    CONVERSATIONS,
    MEMORIES,
    NO_DEFAULT_EXPIRY,
    PARTITION_KEY_VERSION,
    RETENTION_DAYS_MAX,
    USAGE,
    ContainerDefinition,
)
from docchat_core.cosmos.conversations import SessionDocument, TurnDocument, TurnTraceDocument
from docchat_core.cosmos.memories import MemoryDocument
from docchat_core.cosmos.usage import USAGE_DOCUMENT_MODELS

DOCUMENT_MODELS: Final[tuple[type[CosmosDocument], ...]] = (
    SessionDocument,
    TurnDocument,
    TurnTraceDocument,
    MemoryDocument,
    *USAGE_DOCUMENT_MODELS,
)
"""Every stored document type, for the checks that walk the registered models."""

__all__ = [
    "CONTAINERS",
    "CONVERSATIONS",
    "DOCUMENT_MODELS",
    "MEMORIES",
    "NO_DEFAULT_EXPIRY",
    "PARTITION_KEY_VERSION",
    "RETENTION_DAYS_MAX",
    "USAGE",
    "ContainerDefinition",
    "CosmosDocument",
    "CosmosModel",
    "DocumentTooLargeError",
]
