"""First-sign-in provisioning: `ensure_user(engine, principal, throttle)`, step 5 of every
authenticated `/v1` request (`11-api.md` sections 1.3 and 3.2.1; `80-data-model.md` section 2.2;
ADR 0021).

Every person who reaches the API with a valid role gets exactly one `app.users` row, keyed by the
token's `oid`, holding the default memberships of their first moment; the row's display values
come only from the validated token. A known user costs at most one indexed `UPDATE` and usually
nothing: the per-process last-seen throttle skips the step while the caller's row was written or
confirmed less than 15 minutes ago. The throttle holds no access decision; losing it costs one
more write.

Failures: the unit of work re-runs on the data-access layer's transient errors (a deadlock
included); a duplicate key (two processes inserting the same new user) or a deadlock that reaches
here re-runs the whole transaction once more; a second failure raises `SqlUnavailable`, which the
API answers with `503 dependency_unavailable`, and leaves the throttle unchanged.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final, Literal
from uuid import UUID

from docchat_core.auth import Principal
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.sql.engine import (
    SqlDeadlock,
    SqlEngine,
    SqlError,
    SqlUnavailable,
    SqlUniqueViolation,
)
from docchat_core.sql.repositories.users import ProvisionResult, provision_user
from docchat_core.users import telemetry

type ProvisioningOutcome = Literal["skipped", "touched", "provisioned"]

THROTTLE_SECONDS: Final = 15 * 60
THROTTLE_CAPACITY: Final = 50_000
ATTEMPTS: Final = 2


class LastSeenThrottle:
    """The per-process map of `oid` to the time this process last wrote or confirmed that
    user's row: at most 50,000 entries, the oldest evicted first."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        capacity: int = THROTTLE_CAPACITY,
        window_seconds: float = THROTTLE_SECONDS,
    ) -> None:
        self._clock: Clock = SystemClock() if clock is None else clock
        self._capacity = capacity
        self._window = window_seconds
        self._seen: OrderedDict[UUID, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def is_fresh(self, oid: UUID) -> bool:
        """The caller's row was written or confirmed less than 15 minutes ago."""
        seen = self._seen.get(oid)
        return seen is not None and self._clock.monotonic() - seen < self._window

    def record(self, oid: UUID) -> None:
        self._seen[oid] = self._clock.monotonic()
        self._seen.move_to_end(oid)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)


async def _provision(engine: SqlEngine, principal: Principal) -> ProvisionResult:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await provision_user(
                engine,
                oid=principal.oid,
                tid=principal.tid,
                display_name=principal.display_name,
                email=principal.email,
                role=principal.last_known_role,
            )
        except (SqlDeadlock, SqlUniqueViolation) as failure:
            if attempt == ATTEMPTS:
                raise SqlUnavailable("exhausted", failure.number, failure.sqlstate) from None
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


async def ensure_user(
    engine: SqlEngine, principal: Principal, throttle: LastSeenThrottle
) -> ProvisioningOutcome:
    """Provision the caller on their first request, or refresh their row's display values and
    `last_seen_at` at most every 15 minutes per process; `SqlError` when SQL can't serve it."""
    with telemetry.ensure_span() as span:
        if throttle.is_fresh(principal.oid):
            telemetry.record_outcome(span, "skipped")
            return "skipped"
        try:
            result = await _provision(engine, principal)
        except SqlError:
            telemetry.record_outcome(span, "failed")
            raise
        if result.provisioned:
            telemetry.report_provisioned(span, principal.oid, result.default_project_count)
        else:
            telemetry.record_outcome(span, "touched")
    throttle.record(principal.oid)
    return "provisioned" if result.provisioned else "touched"
