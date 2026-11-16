"""The three Cosmos DB containers of database `docchat` (`80-data-model.md` sections 3.1, 3.3, 3.8,
3.9 and 3.13; ADR 0030).

Every container has no unique keys, because every `id` is unique within its logical partition and
derived or random. `conversations` keeps one session per logical partition `[userId, sessionId]`
(a hierarchical key); `memories` and `usage` are keyed by `userId` alone. The indexing policies
hold exactly the paths the queries of section 3.11 filter or sort on, so questions, answers and
model input add no index cost to a write. Bicep declares the containers; these definitions are
what the emulator fixtures create and what the deployed checks compare with.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Final, Literal

from azure.cosmos import PartitionKey

PARTITION_KEY_VERSION: Final = 2
SECONDS_PER_DAY: Final = 86_400
RETENTION_DAYS_MAX: Final = 3_650
NO_DEFAULT_EXPIRY: Final = -1
"""`defaultTtl` `-1`: nothing expires unless an item carries its own `ttl`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerDefinition:
    """One container: its name, partition key paths, indexing policy and expiry rule.

    `retention_parameter` names the `environmentConfig` field whose day count sets `defaultTtl`;
    `None` means the container's `defaultTtl` is always `-1` and each item carries its own `ttl`.
    """

    name: str
    partition_key_paths: tuple[str, ...]
    retention_parameter: str | None
    _indexing_policy: dict[str, Any]

    @property
    def partition_key_kind(self) -> Literal["Hash", "MultiHash"]:
        return "MultiHash" if len(self.partition_key_paths) > 1 else "Hash"

    def partition_key_definition(self) -> dict[str, Any]:
        """The partition key as Bicep and the resource API write it."""
        return {
            "paths": list(self.partition_key_paths),
            "kind": self.partition_key_kind,
            "version": PARTITION_KEY_VERSION,
        }

    def partition_key(self) -> PartitionKey:
        """The SDK's partition key for `create_container`."""
        if len(self.partition_key_paths) > 1:
            return PartitionKey(
                path=list(self.partition_key_paths), kind="MultiHash", version=PARTITION_KEY_VERSION
            )
        return PartitionKey(
            path=self.partition_key_paths[0], kind="Hash", version=PARTITION_KEY_VERSION
        )

    def indexing_policy(self) -> dict[str, Any]:
        """A fresh copy of the indexing policy, so no caller can change the definition."""
        return copy.deepcopy(self._indexing_policy)

    def default_ttl(self, retention_days: int = 0) -> int:
        """`defaultTtl` for a retention of `retention_days` (0 to 3,650, 0 = keep): `-1` for 0,
        otherwise the days in seconds, so each item expires that long after its last change."""
        if not 0 <= retention_days <= RETENTION_DAYS_MAX:
            raise ValueError(f"retention is 0 to {RETENTION_DAYS_MAX} days")
        if self.retention_parameter is None and retention_days:
            raise ValueError(f"container {self.name} has no retention parameter")
        if retention_days == 0:
            return NO_DEFAULT_EXPIRY
        return retention_days * SECONDS_PER_DAY


_POINT_ACCESS_ONLY: Final[dict[str, Any]] = {
    "indexingMode": "consistent",
    "automatic": True,
    "includedPaths": [],
    "excludedPaths": [{"path": "/*"}],
}

CONVERSATIONS: Final = ContainerDefinition(
    name="conversations",
    partition_key_paths=("/userId", "/sessionId"),
    retention_parameter="conversationRetentionDays",
    _indexing_policy={
        "indexingMode": "consistent",
        "automatic": True,
        "includedPaths": [
            {"path": "/userId/?"},
            {"path": "/sessionId/?"},
            {"path": "/type/?"},
            {"path": "/projectId/?"},
            {"path": "/lastTurnAt/?"},
            {"path": "/seq/?"},
            {"path": "/status/?"},
            {"path": "/endedAt/?"},
            {"path": "/memoryExtraction/state/?"},
            {"path": "/retrieval/kept/[]/documentId/?"},
        ],
        "excludedPaths": [{"path": "/*"}, {"path": '/"_etag"/?'}],
        # Serves the session list's `lastTurnAt` descending, `sessionId` descending order.
        "compositeIndexes": [
            [
                {"path": "/lastTurnAt", "order": "ascending"},
                {"path": "/sessionId", "order": "ascending"},
            ]
        ],
    },
)

# Every access is a point read or a conditioned replace or create, so nothing is indexed.
MEMORIES: Final = ContainerDefinition(
    name="memories",
    partition_key_paths=("/userId",),
    retention_parameter="memoryRetentionDays",
    _indexing_policy=_POINT_ACCESS_ONLY,
)

# Point reads, creates, patches, replaces and deletes only; each item carries its own `ttl`.
USAGE: Final = ContainerDefinition(
    name="usage",
    partition_key_paths=("/userId",),
    retention_parameter=None,
    _indexing_policy=_POINT_ACCESS_ONLY,
)

CONTAINERS: Final = (CONVERSATIONS, MEMORIES, USAGE)
