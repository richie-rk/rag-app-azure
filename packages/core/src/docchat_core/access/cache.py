"""The index-record cache of `resolve_chat_index` (`11-api.md` section 1.4.5; `82-security.md`
SEC-21; ADR 0020 item 8).

It holds a logical index's status and its active physical index, keyed by `search_index_id`, for
at most 60 seconds and at most 256 entries, the least recently used evicted first. It holds no
access decision: memberships, overrides, mappings and project status are read on every request.
Only a record with an active physical index is ever stored (an `IndexRecord` can't exist
without one), so chat works from the first request after an index's first physical index
becomes active; a retired physical index is deleted no sooner than 10 minutes after a repoint
(ADR 0045), so a cached record never names a deleted index.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final
from uuid import UUID

from docchat_core.access.models import IndexRecord
from docchat_core.models.clock import Clock, SystemClock

CACHE_CAPACITY: Final = 256
CACHE_LIFETIME_SECONDS: Final = 60.0


class IndexRecordCache:
    """One per process. Each worker serves its event loop alone, so no lock is needed."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        capacity: int = CACHE_CAPACITY,
        lifetime_seconds: float = CACHE_LIFETIME_SECONDS,
    ) -> None:
        self._clock: Clock = SystemClock() if clock is None else clock
        self._capacity = capacity
        self._lifetime = lifetime_seconds
        self._entries: OrderedDict[UUID, tuple[float, IndexRecord]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, search_index_id: UUID) -> IndexRecord | None:
        """The record while it is younger than the lifetime; an older one is dropped."""
        entry = self._entries.get(search_index_id)
        if entry is None:
            return None
        stored_at, record = entry
        if self._clock.monotonic() - stored_at >= self._lifetime:
            del self._entries[search_index_id]
            return None
        self._entries.move_to_end(search_index_id)
        return record

    def put(self, record: IndexRecord) -> None:
        """Store a record for the lifetime, evicting the least recently used at capacity."""
        self._entries[record.search_index_id] = (self._clock.monotonic(), record)
        self._entries.move_to_end(record.search_index_id)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
