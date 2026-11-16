"""`TelemetrySettings` and start-up check 9 (`81-config.md` sections 2.1, 2.3, 2.9, 2.10 and 6.1;
`85-observability.md` section 1.9).

Both apps read the five settings once at start. The sampling ratio is the one sampling setting:
`configure_telemetry` derives the `OTEL_*` variables from it inside the process, so no second
setting can disagree with it. The connection string is an identifier, not a credential, because
local authentication is off on the component; it is absent in local runs, where nothing is
exported.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from docchat_core.settings import (
    BuildCommit,
    DocChatSettings,
    LocalSettings,
    SettingsValidationError,
)

TEST_PLUGIN_MODULE: Final = "docchat_testing.plugin"
"""The pytest plugin every DocChat test session loads; while it is loaded, telemetry is strict."""

SAMPLING_RATIO_MIN: Final = 0.1
SAMPLING_RATIO_MAX: Final = 1.0
LOOP_LAG_INTERVAL_MIN_MS: Final = 100
LOOP_LAG_INTERVAL_MAX_MS: Final = 5_000


class TelemetryLogLevel(StrEnum):
    """`DOCCHAT_TELEMETRY_LOG_LEVEL`: the level of the `docchat` logger. `DEBUG` is allowed only in
    local-emulator mode (check 9)."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class TelemetrySettings(DocChatSettings):
    """Read by the API and the ingestion app; `configure_telemetry` and the loop-lag monitor use
    them."""

    telemetry_sampling_ratio: Annotated[
        float, Field(ge=SAMPLING_RATIO_MIN, le=SAMPLING_RATIO_MAX, allow_inf_nan=False)
    ] = 1.0
    telemetry_log_level: TelemetryLogLevel = TelemetryLogLevel.INFO
    telemetry_loop_lag_interval_ms: Annotated[
        int, Field(ge=LOOP_LAG_INTERVAL_MIN_MS, le=LOOP_LAG_INTERVAL_MAX_MS)
    ] = 500
    build_commit: BuildCommit
    applicationinsights_connection_string: str | None = Field(
        default=None, validation_alias="APPLICATIONINSIGHTS_CONNECTION_STRING"
    )

    @property
    def strict(self) -> bool:
        """Test mode: an unregistered attribute, record or event raises instead of being dropped
        and counted (`85-observability.md` section 3.2).

        It is a property, not a setting, because no app setting may switch it: it is true exactly
        while the test plugin is loaded, which production code can see without importing it.
        """
        return TEST_PLUGIN_MODULE in sys.modules


def check_telemetry_log_level(telemetry: TelemetrySettings, local: LocalSettings) -> None:
    """Check 9: `DEBUG` outside local-emulator mode is refused, so no debug record reaches a
    deployed workspace."""
    if telemetry.telemetry_log_level is TelemetryLogLevel.DEBUG and not local.local_emulators:
        raise SettingsValidationError((TelemetrySettings.setting_names()["telemetry_log_level"],))
