"""What the stub saw: the request recorder and the per-deployment counters (84-testing.md 2.3).

The recorder keeps the last 2,000 requests with sequence number, time, path, deployment and JSON
body, and only when `DOCCHAT_STUB_RECORD` is on. No header is kept, so the bearer token a caller
sends is never stored. Both live in the worker process.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RECORDER_CAPACITY = 2000


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RecordedRequest:
    seq: int
    time: str
    path: str
    deployment: str | None
    body: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": self.time,
            "path": self.path,
            "deployment": self.deployment,
            "body": self.body,
        }


class Recorder:
    """The last `capacity` requests, numbered from 1 in arrival order."""

    def __init__(self, *, enabled: bool, capacity: int = RECORDER_CAPACITY) -> None:
        self._enabled = enabled
        self._entries: deque[RecordedRequest] = deque(maxlen=capacity)
        self._seq = 0

    def record(self, path: str, deployment: str | None, body: dict[str, Any]) -> None:
        if not self._enabled:
            return
        self._seq += 1
        self._entries.append(RecordedRequest(self._seq, _utc_now_text(), path, deployment, body))

    def after(self, seq: int) -> list[RecordedRequest]:
        """The kept requests whose sequence number is above `seq`, oldest first."""
        return [entry for entry in self._entries if entry.seq > seq]

    def clear(self) -> None:
        # Sequence numbers keep counting, so an `after` cursor taken before a clear stays valid.
        self._entries.clear()


@dataclass
class DeploymentStats:
    started: int = 0
    completed: int = 0
    throttled: int = 0
    client_disconnected: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "started": self.started,
            "completed": self.completed,
            "throttled": self.throttled,
            "clientDisconnected": self.client_disconnected,
        }


class Stats:
    """Counters per deployment, created at a deployment's first request."""

    def __init__(self) -> None:
        self._by_deployment: dict[str, DeploymentStats] = {}

    def of(self, deployment: str) -> DeploymentStats:
        return self._by_deployment.setdefault(deployment, DeploymentStats())

    def to_json(self) -> dict[str, dict[str, int]]:
        return {name: stats.to_json() for name, stats in sorted(self._by_deployment.items())}
