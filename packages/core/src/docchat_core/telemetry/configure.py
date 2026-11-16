"""`configure_telemetry`, the only place telemetry is configured (`85-observability.md` sections
1.2 to 1.6; ADR 0137).

Every exporter passes through the scrubbing processors, so nothing else may configure one: the
API calls this once per Gunicorn worker at the start of its lifespan, the ingestion app once per
worker process when its function app is imported. Export goes to the environment's Application
Insights through the Azure Monitor distro, which builds its Entra credential from
`APPLICATIONINSIGHTS_AUTHENTICATION_STRING`, so no credential appears here. Without a connection
string (local runs) nothing is exported and only the stderr handler writes, scrubbed. The
test-only `exporters` argument builds the same pipeline (sampler, resource, scrubbing processors,
view, instrumentations, loggers) with in-memory exporters in place of the Azure ones, so no test
exports anywhere.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Final, override

from opentelemetry import metrics, propagate, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from docchat_core.settings import CoreSettings, load_settings
from docchat_core.telemetry.instruments import MILLISECOND_HISTOGRAM_VIEW
from docchat_core.telemetry.records import LOGGER_NAME
from docchat_core.telemetry.scrubbing import (
    ScrubbingLogFilter,
    ScrubbingLogRecordProcessor,
    ScrubbingSpanProcessor,
)
from docchat_core.telemetry.settings import TelemetrySettings
from docchat_core.telemetry.state import SERVICE_NAMES, ServiceName, set_state

if TYPE_CHECKING:
    from collections.abc import Mapping

OTEL_TRACES_SAMPLER: Final = "OTEL_TRACES_SAMPLER"
OTEL_TRACES_SAMPLER_ARG: Final = "OTEL_TRACES_SAMPLER_ARG"
OTEL_PROPAGATORS: Final = "OTEL_PROPAGATORS"
FIXED_PERCENTAGE_SAMPLER: Final = "microsoft.fixed_percentage"
"""The distro's percentage sampler: it decides from the trace ID alone and ignores the caller's
sampled flag, so a trace is kept or dropped whole across both apps (section 1.5)."""

TRACECONTEXT: Final = "tracecontext"
AZURE_SDK_TRACING_IMPLEMENTATION: Final = "AZURE_SDK_TRACING_IMPLEMENTATION"

SERVICE_NAME_ATTRIBUTE: Final = "service.name"
SERVICE_VERSION_ATTRIBUTE: Final = "service.version"
ENVIRONMENT_ATTRIBUTE: Final = "deployment.environment.name"

LEVELLED_LOGGERS: Final = (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "httpx",
    "httpcore",
    "httpx2",
    "openai",
    "opentelemetry",
    "uvicorn.error",
)
"""Third-party loggers held at `WARNING` (section 1.6)."""

DISABLED_LOGGERS: Final = ("uvicorn.access",)
STDERR_LEVEL: Final = logging.WARNING
STDERR_FORMAT: Final = "%(levelname)s %(name)s %(message)s"


def instrumentation_options(service: ServiceName) -> dict[str, dict[str, bool]]:
    """The distro's instrumentations: Azure SDK and `httpx` in both apps, FastAPI in the API, and
    nothing else (section 1.2)."""
    return {
        "azure_sdk": {"enabled": True},
        "httpx": {"enabled": True},
        "fastapi": {"enabled": service == "docchat-api"},
        "django": {"enabled": False},
        "flask": {"enabled": False},
        "psycopg2": {"enabled": False},
        "requests": {"enabled": False},
        "urllib": {"enabled": False},
        "urllib3": {"enabled": False},
    }


@dataclass(frozen=True, slots=True)
class TestExporters:
    """In-memory span, log and metric exporters for tests (`84-testing.md` section 2.10)."""

    # Not a test class, though its name starts with `Test`.
    __test__ = False

    spans: InMemorySpanExporter = field(default_factory=InMemorySpanExporter)
    logs: InMemoryLogRecordExporter = field(default_factory=InMemoryLogRecordExporter)
    metrics: InMemoryMetricReader = field(default_factory=InMemoryMetricReader)


def build_resource(service: ServiceName, settings: TelemetrySettings) -> Resource:
    """`service.name`, `service.version` and `deployment.environment.name` (section 1.4).

    The environment is `DOCCHAT_CORE_ENVIRONMENT`, a `CoreSettings` field that the signature of
    `configure_telemetry` doesn't carry, so it is read here through its own class; start-up has
    validated that class before telemetry is configured. `service.namespace` is never set,
    because the exporter would prefix the role name with it.
    """
    environment = load_settings(CoreSettings).core_environment
    return Resource(
        {
            SERVICE_NAME_ATTRIBUTE: service,
            SERVICE_VERSION_ATTRIBUTE: settings.build_commit,
            ENVIRONMENT_ATTRIBUTE: environment.value,
        }
    )


def _configure_azure_monitor(**options: object) -> None:
    # Imported here, so importing `docchat_core.telemetry` never loads the distro and its
    # exporter until a process configures export.
    from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: PLC0415

    configure_azure_monitor(**options)


def configure_telemetry(
    service: ServiceName, settings: TelemetrySettings, exporters: TestExporters | None = None
) -> None:
    """Configure the process's telemetry: the sampler and propagator variables, the distro (or the
    in-memory pipeline), then the loggers. It emits no event itself.

    An exception from the distro propagates, so a process that can't configure telemetry
    doesn't serve.
    """
    if service not in SERVICE_NAMES:
        raise ValueError("service is docchat-api or docchat-ingestion")
    resource = build_resource(service, settings)
    os.environ[OTEL_TRACES_SAMPLER] = FIXED_PERCENTAGE_SAMPLER
    os.environ[OTEL_TRACES_SAMPLER_ARG] = str(settings.telemetry_sampling_ratio)
    os.environ[OTEL_PROPAGATORS] = TRACECONTEXT
    # The variable only reaches propagators loaded after it is set; setting the global one too
    # keeps client-supplied baggage out whatever was imported first.
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    set_state(strict=settings.strict, service=service)
    if exporters is not None:
        _install_test_pipeline(service, settings, resource, exporters)
    elif settings.applicationinsights_connection_string is not None:
        _configure_azure_monitor(
            connection_string=settings.applicationinsights_connection_string,
            logger_name=LOGGER_NAME,
            resource=resource,
            span_processors=[ScrubbingSpanProcessor()],
            log_record_processors=[ScrubbingLogRecordProcessor()],
            views=[MILLISECOND_HISTOGRAM_VIEW],
            instrumentation_options=instrumentation_options(service),
            enable_live_metrics=False,
            enable_performance_counters=True,
        )
    configure_loggers(settings)


def _install_test_pipeline(
    service: ServiceName,
    settings: TelemetrySettings,
    resource: Resource,
    exporters: TestExporters,
) -> None:
    # The distro's own sampler class for `microsoft.fixed_percentage`.
    from azure.monitor.opentelemetry.exporter import ApplicationInsightsSampler  # noqa: PLC0415

    tracer_provider = TracerProvider(
        sampler=ApplicationInsightsSampler(sampling_ratio=settings.telemetry_sampling_ratio),
        resource=resource,
    )
    tracer_provider.add_span_processor(ScrubbingSpanProcessor())
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporters.spans))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            metric_readers=[exporters.metrics],
            resource=resource,
            views=[MILLISECOND_HISTOGRAM_VIEW],
        )
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(ScrubbingLogRecordProcessor())
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(exporters.logs))
    set_logger_provider(logger_provider)
    logger = logging.getLogger(LOGGER_NAME)
    for handler in [h for h in logger.handlers if isinstance(h, LoggingHandler)]:
        logger.removeHandler(handler)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))

    instrument_libraries(service)


def instrument_libraries(service: ServiceName) -> None:
    """Enable the instrumentations `instrumentation_options` enables, as the distro does: each
    library instrumentor through its entry point, and the Azure SDK's OpenTelemetry tracing."""
    options: Mapping[str, Mapping[str, bool]] = instrumentation_options(service)
    if options["azure_sdk"]["enabled"]:
        os.environ.setdefault(AZURE_SDK_TRACING_IMPLEMENTATION, "opentelemetry")
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        if options.get(entry_point.name, {}).get("enabled", False):
            entry_point.load()().instrument()


class StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """The `docchat` logger's standard-error handler: `WARNING` and above, scrubbed."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setLevel(STDERR_LEVEL)
        self.addFilter(ScrubbingLogFilter())
        self.setFormatter(logging.Formatter(STDERR_FORMAT))

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} ({logging.getLevelName(self.level)})>"


def configure_loggers(settings: TelemetrySettings) -> None:
    """The loggers of section 1.6: `docchat` at the configured level, not propagating (so the
    Functions worker's forwarding handler never duplicates a record), scrubbed, with one stderr
    handler; the noisy third-party loggers at `WARNING`; `uvicorn.access` off."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.telemetry_log_level.value)
    logger.propagate = False
    for log_filter in [f for f in logger.filters if isinstance(f, ScrubbingLogFilter)]:
        logger.removeFilter(log_filter)
    logger.addFilter(ScrubbingLogFilter())
    for handler in [h for h in logger.handlers if isinstance(h, StderrHandler)]:
        logger.removeHandler(handler)
    logger.addHandler(StderrHandler())
    for name in LEVELLED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in DISABLED_LOGGERS:
        logging.getLogger(name).disabled = True
