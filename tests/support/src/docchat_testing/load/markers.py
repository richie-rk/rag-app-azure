"""The marker lines of a load generator's log (`84-testing.md` sections 7.1 and 7.2; T-579).

The bootstrap prints `K6_EXIT <code>` or `K6_BOOTSTRAP_FAILED <reason>`; the scenario prints
`K6_SUMMARY_JSON <json>` (every threshold's `ok` and `limit_retries`), `K6_FAILED_REQUEST <json>`
and `K6_SESSION <json>`. The bootstrap runs k6 with raw console output on standard output, so a
marker is normally a bare line; one inside k6's default `msg="…"` log format is read too, so a log
written any other way still yields its markers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

EXIT: Final = "K6_EXIT"
BOOTSTRAP_FAILED: Final = "K6_BOOTSTRAP_FAILED"
SUMMARY: Final = "K6_SUMMARY_JSON"
FAILED_REQUEST: Final = "K6_FAILED_REQUEST"
SESSION: Final = "K6_SESSION"
MARKERS: Final = (EXIT, BOOTSTRAP_FAILED, SUMMARY, FAILED_REQUEST, SESSION)

THRESHOLD_FAILED_EXIT: Final = 99
"""k6's exit code when a threshold was crossed."""

_LOGFMT_MESSAGE: Final = re.compile(r'\bmsg="((?:[^"\\]|\\.)*)"')
_TRACE_ID: Final = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class FailedRequest:
    """One `K6_FAILED_REQUEST`: a request the scenario counted as failed."""

    trace_id: str
    started_at: datetime
    ended_at: datetime
    status: int
    reason: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One `K6_SESSION`: a chat session and the trace IDs of its turns, in order."""

    session_id: str | None
    trace_ids: tuple[str, ...]


@dataclass(slots=True)
class K6Log:
    """Everything a log's markers say."""

    exit_code: int | None = None
    bootstrap_failure: str | None = None
    summary: dict[str, Any] | None = None
    failed_requests: list[FailedRequest] = field(default_factory=list)
    sessions: list[SessionRecord] = field(default_factory=list)
    unreadable: int = 0
    """Marker lines whose payload couldn't be read."""

    @property
    def ran(self) -> bool:
        """k6 ran to an exit code: no bootstrap failure and a `K6_EXIT` line."""
        return self.bootstrap_failure is None and self.exit_code is not None

    @property
    def limit_retries(self) -> int | None:
        if self.summary is None:
            return None
        value = self.summary.get("limitRetries")
        return value if isinstance(value, int) else None

    def threshold(self, metric: str, expression: str) -> bool | None:
        """Whether a threshold held, or `None` when the summary doesn't name it."""
        if self.summary is None:
            return None
        thresholds = self.summary.get("thresholds")
        if not isinstance(thresholds, Mapping):
            return None
        outcomes = thresholds.get(metric)
        if not isinstance(outcomes, Mapping):
            return None
        outcome = outcomes.get(expression)
        return outcome if isinstance(outcome, bool) else None

    def value(self, metric: str, name: str) -> float | None:
        """A thresholded metric's value (`p(95)`, `rate`, `count`) from the summary."""
        if self.summary is None:
            return None
        values = self.summary.get("values")
        metric_values = values.get(metric) if isinstance(values, Mapping) else None
        number = metric_values.get(name) if isinstance(metric_values, Mapping) else None
        return float(number) if isinstance(number, int | float) else None


def _unescape(quoted: str) -> str:
    try:
        text: str = json.loads(f'"{quoted}"')
    except ValueError:
        return quoted.replace('\\"', '"').replace("\\\\", "\\")
    return text


def marker_text(line: str) -> str | None:
    """The marker part of a log line: the line itself, or the message of k6's default format."""
    stripped = line.strip()
    if stripped.startswith(MARKERS):
        return stripped
    found = _LOGFMT_MESSAGE.search(stripped)
    if found is not None:
        message = _unescape(found.group(1)).strip()
        if message.startswith(MARKERS):
            return message
    return None


def _time(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _failed_request(payload: Any) -> FailedRequest:
    trace_id = str(payload["traceId"])
    if not _TRACE_ID.match(trace_id):
        raise ValueError("traceId isn't 32 hexadecimal characters")
    return FailedRequest(
        trace_id=trace_id,
        started_at=_time(payload["startedAt"]),
        ended_at=_time(payload["endedAt"]),
        status=int(payload["status"]),
        reason=str(payload["reason"]),
    )


def _session(payload: Any) -> SessionRecord:
    session_id = payload["sessionId"]
    trace_ids = payload["traceIds"]
    if session_id is not None and not isinstance(session_id, str):
        raise TypeError("sessionId isn't a string")
    if not isinstance(trace_ids, list) or not all(isinstance(item, str) for item in trace_ids):
        raise TypeError("traceIds isn't a list of strings")
    return SessionRecord(session_id=session_id, trace_ids=tuple(trace_ids))


def _summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("the summary isn't an object")
    return payload


def parse_log(lines: Iterable[str]) -> K6Log:
    """Read the markers of a load generator's log; later `K6_EXIT` and summary lines win."""
    log = K6Log()
    for line in lines:
        text = marker_text(line)
        if text is None:
            continue
        name, _, rest = text.partition(" ")
        rest = rest.strip()
        try:
            if name == EXIT:
                log.exit_code = int(rest)
            elif name == BOOTSTRAP_FAILED:
                log.bootstrap_failure = rest or "unknown"
            elif name == SUMMARY:
                log.summary = _summary(json.loads(rest))
            elif name == FAILED_REQUEST:
                log.failed_requests.append(_failed_request(json.loads(rest)))
            elif name == SESSION:
                log.sessions.append(_session(json.loads(rest)))
        except (ValueError, TypeError, KeyError):
            log.unreadable += 1
    return log


def read_log(text: str) -> K6Log:
    return parse_log(text.splitlines())
