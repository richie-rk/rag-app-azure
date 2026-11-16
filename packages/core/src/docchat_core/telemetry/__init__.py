"""Telemetry for both apps (`85-observability.md` sections 1 to 4; ADRs 0014 and 0137).

`configure_telemetry` is the only place telemetry is configured. Application code emits records
only through `log_event`, `emit_event`, `start_span`, `set_attributes` and `record_failure`,
which check every name and value against the attribute registry
(`docchat_core.telemetry.attributes`); the scrubbing attach points
(`docchat_core.telemetry.scrubbing`) are the safety net behind them. The shared package's
instruments live in `docchat_core.telemetry.instruments`, each area's in its own `telemetry.py`.
"""

from docchat_core.telemetry.configure import TestExporters, build_resource, configure_telemetry
from docchat_core.telemetry.instruments import MILLISECOND_HISTOGRAM_VIEW
from docchat_core.telemetry.looplag import monitor_loop_lag
from docchat_core.telemetry.records import (
    LOGGER_NAME,
    emit_event,
    log_event,
    record_failure,
    set_attributes,
    start_span,
)
from docchat_core.telemetry.scrubbing import (
    ScrubbingLogFilter,
    ScrubbingLogRecordProcessor,
    ScrubbingSpanProcessor,
    qualified_type_name,
    safe_traceback,
    scrub,
)
from docchat_core.telemetry.settings import (
    TelemetryLogLevel,
    TelemetrySettings,
    check_telemetry_log_level,
)
from docchat_core.telemetry.state import ServiceName

__all__ = [
    "LOGGER_NAME",
    "MILLISECOND_HISTOGRAM_VIEW",
    "ScrubbingLogFilter",
    "ScrubbingLogRecordProcessor",
    "ScrubbingSpanProcessor",
    "ServiceName",
    "TelemetryLogLevel",
    "TelemetrySettings",
    "TestExporters",
    "build_resource",
    "check_telemetry_log_level",
    "configure_telemetry",
    "emit_event",
    "log_event",
    "monitor_loop_lag",
    "qualified_type_name",
    "record_failure",
    "safe_traceback",
    "scrub",
    "set_attributes",
    "start_span",
]
