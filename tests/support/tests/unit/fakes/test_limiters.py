from __future__ import annotations

from uuid import uuid4

from docchat_api.http.ratelimit import BurstLimiterPort, WriteLimiterPort
from docchat_core.auth import Principal
from docchat_testing.fakes.limiters import PermissiveBurstLimiter, PermissiveWriteRateLimiter


def test_the_permissive_burst_limiter_allows_and_records_every_request() -> None:
    limiter: BurstLimiterPort = PermissiveBurstLimiter()
    oid = uuid4()

    for _ in range(1_000):
        limiter.check(oid)

    assert isinstance(limiter, PermissiveBurstLimiter)
    assert limiter.checked == [oid] * 1_000


async def test_the_permissive_write_limiter_allows_and_records_every_write() -> None:
    limiter: WriteLimiterPort = PermissiveWriteRateLimiter()
    principal = Principal(
        oid=uuid4(), tid=uuid4(), azp=uuid4(), client="web", roles=frozenset({"User"})
    )

    for _ in range(500):
        await limiter.check(principal, 10)

    assert isinstance(limiter, PermissiveWriteRateLimiter)
    assert limiter.checked == [(principal.oid, 10)] * 500
