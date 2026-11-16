"""Unit tests of the index-record cache (T-011; `11-api.md` section 1.4.5)."""

from __future__ import annotations

from uuid import UUID, uuid4

from docchat_core.access.cache import CACHE_CAPACITY, CACHE_LIFETIME_SECONDS, IndexRecordCache
from docchat_core.access.models import IndexRecord, PhysicalIndex
from docchat_testing.clock import FakeClock


def record(search_index_id: UUID | None = None) -> IndexRecord:
    return IndexRecord(
        search_index_id=search_index_id or uuid4(),
        status="ready",
        physical_index=PhysicalIndex(
            id=uuid4(),
            name="docchat-0123456789abcdef0123456789abcdef-g1",
            schema_version=1,
            embedding_model="text-embedding-3-large",
            embedding_dimensions=1024,
            query_embedding_deployment="embed-query-te3-large-v1",
        ),
    )


def test_the_limits_are_256_entries_and_60_seconds() -> None:
    assert (CACHE_CAPACITY, CACHE_LIFETIME_SECONDS) == (256, 60.0)


def test_t011_an_entry_lives_at_most_60_seconds() -> None:
    clock = FakeClock()
    cache = IndexRecordCache(clock)
    stored = record()
    cache.put(stored)

    clock.advance(59.9)
    assert cache.get(stored.search_index_id) is stored
    clock.advance(0.1)
    assert cache.get(stored.search_index_id) is None
    assert len(cache) == 0


def test_t011_the_least_recently_used_entry_is_evicted_at_capacity() -> None:
    cache = IndexRecordCache(FakeClock(), capacity=3)
    first, second, third, fourth = record(), record(), record(), record()
    for entry in (first, second, third):
        cache.put(entry)

    assert cache.get(first.search_index_id) is first
    cache.put(fourth)

    assert len(cache) == 3
    assert cache.get(second.search_index_id) is None
    assert cache.get(first.search_index_id) is first
    assert cache.get(third.search_index_id) is third
    assert cache.get(fourth.search_index_id) is fourth


def test_a_new_record_replaces_the_old_and_restarts_its_lifetime() -> None:
    clock = FakeClock()
    cache = IndexRecordCache(clock)
    index_id = uuid4()
    cache.put(record(index_id))
    clock.advance(50)
    newer = record(index_id)
    cache.put(newer)
    clock.advance(50)

    assert cache.get(index_id) is newer
    assert len(cache) == 1


def test_a_missing_entry_is_none() -> None:
    assert IndexRecordCache().get(uuid4()) is None
