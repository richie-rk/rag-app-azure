"""The process's telemetry state: test mode and the service (`85-observability.md` sections 1.2,
3.2 and 4.7).

Telemetry is process-wide by nature (the OpenTelemetry providers are process globals), so the two
facts every record helper needs live here, set once by `configure_telemetry`: whether a breach of
the attribute registry raises (test mode) or is dropped and counted (production), and which app
the process is, for the `service` dimension of the shared package's instruments. Before
`configure_telemetry` runs, test mode follows the test plugin and the service is unknown.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from docchat_core.telemetry.settings import TEST_PLUGIN_MODULE

type ServiceName = Literal["docchat-api", "docchat-ingestion"]
"""`service.name`: a constant in code, never a setting (section 1.4)."""

SERVICE_NAMES: Final[tuple[ServiceName, ...]] = ("docchat-api", "docchat-ingestion")

SERVICE_DIMENSIONS: Final[Mapping[ServiceName, str]] = MappingProxyType(
    {"docchat-api": "api", "docchat-ingestion": "ingestion"}
)
"""The `service` dimension of each app on the shared package's instruments (section 4.7)."""


@dataclass(slots=True)
class TelemetryState:
    strict: bool
    service: ServiceName | None = None


STATE: Final = TelemetryState(strict=TEST_PLUGIN_MODULE in sys.modules)


def is_strict() -> bool:
    """Whether a breach of the attribute registry raises `ValueError` (test mode)."""
    return STATE.strict


def service_dimension() -> str | None:
    """The `service` dimension value of this process, once telemetry is configured."""
    return None if STATE.service is None else SERVICE_DIMENSIONS[STATE.service]


def set_state(*, strict: bool, service: ServiceName) -> None:
    STATE.strict = strict
    STATE.service = service
