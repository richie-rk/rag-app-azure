"""The audit area's signals (`85-observability.md` sections 4 and 6): the counter
`docchat.audit.write_failures`, the custom event `audit_write_failed` and its error log record,
all emitted only when `write_audit_event_after_commit` gave up.

The counter is created once at import, as an area's own instruments are, through the telemetry
package's factory, which checks its dimensions against the attribute registry; the event and the
record go through `emit_event` and `log_event`, which check theirs.
"""

from __future__ import annotations

import logging
from typing import Final

from docchat_core.audit.models import AuditEvent, MemoryFactsExtractedMetadata
from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event
from docchat_core.telemetry.instruments import counter

EVENT_NAME: Final = "audit_write_failed"

WRITE_FAILURES: Final = counter("docchat.audit.write_failures")

LOGGER: Final = logging.getLogger(LOGGER_NAME)


def report_write_failure(event: AuditEvent) -> None:
    """Count the lost row and emit the event and the error record, naming the action and the
    target; the metadata's session and turn IDs join them when it holds them."""
    WRITE_FAILURES.add(1, action=event.action)
    properties: dict[str, str] = {
        "action": event.action,
        "actorType": event.actor_type,
        "targetType": event.target_type,
    }
    if event.target_id is not None:
        properties["targetId"] = event.target_id
    record: dict[str, str] = {"docchat.audit.action": event.action}
    if isinstance(event.metadata, MemoryFactsExtractedMetadata):
        properties["sessionId"] = record["docchat.session_id"] = str(event.metadata.session_id)
        properties["turnId"] = record["docchat.turn_id"] = str(event.metadata.turn_id)
    emit_event(EVENT_NAME, **properties)
    log_event(LOGGER, EVENT_NAME, logging.ERROR, **record)
