"""The permissive request limiters (`84-testing.md` sections 1.1 item 7 and 1.4; `11-api.md`
section 1.11 "Test seam").

Every app the test fixtures build receives these unless a test asks for the real limiters with
`real_limiters=True`: each allows every request and records it, so a suite that sends hundreds of
requests from one user never meets a `429`, while a test can still see that the route class
consulted the limiter. The authorisation matrix and the isolation suite always run with them. No
setting disables a limiter in a deployed app.
"""

from __future__ import annotations

from uuid import UUID

from docchat_core.auth import Principal


class PermissiveBurstLimiter:
    """Implements the burst limiter's port: every check passes and is recorded."""

    def __init__(self) -> None:
        self.checked: list[UUID] = []

    def check(self, oid: UUID) -> None:
        self.checked.append(oid)


class PermissiveWriteRateLimiter:
    """Implements the write limiter's port: every check passes and is recorded with the limit
    the request's ops-config snapshot gave."""

    def __init__(self) -> None:
        self.checked: list[tuple[UUID, int]] = []

    async def check(self, principal: Principal, limit: int) -> None:
        self.checked.append((principal.oid, limit))
