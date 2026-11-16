"""Injected `429` answers (84-testing.md section 2.3).

When `DOCCHAT_STUB_THROTTLE_EVERY_N` is above 0, every N-th request to a throttled deployment
(those listed in `DOCCHAT_STUB_THROTTLE_DEPLOYMENTS`, every deployment when the list is empty)
gets `429`. Requests are counted per worker process and per deployment.
"""

from __future__ import annotations

import math
from collections import Counter

_MS_PER_SECOND = 1000


def retry_headers(retry_after_ms: int) -> dict[str, str]:
    """The delay headers of an injected `429`: milliseconds, and whole seconds rounded up."""
    return {
        "retry-after-ms": str(retry_after_ms),
        "retry-after": str(math.ceil(retry_after_ms / _MS_PER_SECOND)),
    }


class Throttle:
    def __init__(self, *, every_n: int, deployments: frozenset[str]) -> None:
        self._every_n = every_n
        self._deployments = deployments
        self._counts: Counter[str] = Counter()

    def hit(self, deployment: str) -> bool:
        """Count one request to `deployment` and tell whether it is the one to throttle."""
        if self._every_n == 0 or (self._deployments and deployment not in self._deployments):
            return False
        self._counts[deployment] += 1
        return self._counts[deployment] % self._every_n == 0
