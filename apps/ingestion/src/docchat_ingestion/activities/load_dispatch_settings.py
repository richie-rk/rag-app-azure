"""`load_dispatch_settings` (`14-ingestion.md` section 5.2; ADR 0028).

The dispatcher's first activity of every wake reads the `ingestion` area through the process's
`OpsConfigStore`, which refreshes on the first access after 60 seconds and never on a timer. A
stored area that fails `IngestionOpsConfig` is reported here, once per wake, because the store is
built with `report_invalid=False`: `docchat.config.invalid` and the event and record
`ops_config_invalid` for each failing key, and `valid: false` with no settings, so the dispatcher
keeps its own last valid snapshot and never runs on a default that replaces a stored value.
"""

from __future__ import annotations

from typing import Any, Final

import azure.durable_functions as df

from docchat_core.ingestion.models import ActivityResult
from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.opsconfig import AreaUnavailableError, report_invalid
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.support import WireModel, wire
from docchat_ingestion.boundary import failure_result, run_activity

NAME: Final = "load_dispatch_settings"
DEADLINE_SECONDS: Final = 60
AREA: Final = "ingestion"


class LoadDispatchSettingsInput(WireModel):
    """The activity takes `{}`."""


class LoadDispatchSettingsResult(ActivityResult):
    """`{ok, valid, settings}` with the `ActivityResult` fields; `settings` is the snapshot as
    its dotted keys."""

    valid: bool = False
    settings: IngestionOpsConfig | None = None


async def load(process: IngestionProcess) -> LoadDispatchSettingsResult:
    """The area's current snapshot, or `valid: false` when the stored area fails validation."""
    snapshot = await process.ops.snapshot()
    invalid = process.ops.invalid_keys(AREA)
    if invalid:
        report_invalid(AREA, invalid)
        return LoadDispatchSettingsResult.success(valid=False)
    try:
        settings = snapshot.ingestion
    except AreaUnavailableError:
        # No stored value of the area has validated since the process started.
        return LoadDispatchSettingsResult.success(valid=False)
    return LoadDispatchSettingsResult.success(valid=True, settings=settings)


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    """The activity inside its guard and deadline; `provider` gives the process."""

    async def body() -> dict[str, Any]:
        LoadDispatchSettingsInput.from_wire({} if payload is None else payload)
        return wire(await load(await provider()))

    return await run_activity(NAME, DEADLINE_SECONDS, body, failure_result)


async def load_dispatch_settings(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(load_dispatch_settings)
