"""T-489, the session-wide canary audit, also named the telemetry capture test (`84-testing.md`
section 2.10; `85-observability.md` section 3.7 item 7).

The audit itself runs at the end of every integration session, inside the plugin, over everything
the session's capture received; T-562 proves that it fails a session. This test, an integration
test like the sessions it guards, proves the capture is in place: the process's providers export
spans, `docchat` log records and metric data points into it, so the session-end audit reads them.
"""

from __future__ import annotations

import logging
import secrets

import pytest
from opentelemetry import metrics, trace

from docchat_testing.telemetry_capture import CAPTURE_KEY, TelemetryCapture


def test_t489_the_session_capture_receives_what_the_audit_reads(
    pytestconfig: pytest.Config, telemetry_capture: TelemetryCapture
) -> None:
    marker = f"t489-{secrets.token_hex(4)}"

    with trace.get_tracer("docchat.meta").start_as_current_span(f"docchat.meta.{marker}"):
        pass
    logging.getLogger("docchat").warning(marker)
    metrics.get_meter("docchat.meta").create_counter(f"docchat.meta.{marker}").add(
        1, {"outcome": "ok"}
    )

    assert pytestconfig.stash[CAPTURE_KEY] is telemetry_capture
    assert f"docchat.meta.{marker}" in {span.name for span in telemetry_capture.spans()}
    assert marker in {record.log_record.body for record in telemetry_capture.log_records()}
    labels = {item.label for item in telemetry_capture.items()}
    assert f"metric 'docchat.meta.{marker}' data point #0" in labels
