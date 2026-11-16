"""The Azure CLI calls of the load verdicts (`84-testing.md` sections 7.5 and 7.6).

Platform metrics, the Activity Log, the API's instances and plan, and the Cosmos DB containers'
autoscale maxima are read with `az` as the job's signed-in identity (`id-docchat-citest`, whose
Log Analytics Reader at `rg-docchat-load` covers every read). Each call is a child process; its
standard error is kept to its last line, which names the failure without the request.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from docchat_testing.load.common import RESOURCE_GROUP, LoadError, iso

type AzRunner = Callable[[Sequence[str]], Awaitable[str]]

SCALE_UP_OPERATION: Final = "Microsoft.Insights/AutoscaleSettings/Scaleup/Action"
COSMOS_DATABASE: Final = "docchat"
AZ_TIMEOUT_SECONDS: Final = 300.0


class AzError(LoadError):
    def __init__(self, message: str) -> None:
        super().__init__("az_failed", message)


async def run_az(arguments: Sequence[str]) -> str:
    """`az <arguments>`; its standard output, or `AzError` naming the command and its exit."""
    executable = shutil.which("az")
    if executable is None:
        raise AzError("the Azure CLI isn't on PATH")
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate()
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise AzError(f"az {' '.join(arguments[:3])} didn't finish") from error
    if process.returncode != 0:
        lines = stderr.decode("utf-8", "replace").strip().splitlines()
        last = lines[-1][:300] if lines else ""
        raise AzError(f"az {' '.join(arguments[:3])} exited {process.returncode}: {last}")
    return stdout.decode("utf-8")


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One 1-minute aggregate; `time` is the start of its minute."""

    time: datetime
    value: float | None


def _parse_points(body: Any) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for metric in body.get("value", []) if isinstance(body, dict) else []:
        for series in metric.get("timeseries", []):
            for entry in series.get("data", []):
                average = entry.get("average")
                points.append(
                    MetricPoint(
                        time=datetime.fromisoformat(str(entry["timeStamp"])),
                        value=float(average) if isinstance(average, int | float) else None,
                    )
                )
    return sorted(points, key=lambda point: point.time)


class Azure:
    """The reads of the verdicts, over an `az` runner a test replaces."""

    def __init__(self, runner: AzRunner = run_az, resource_group: str = RESOURCE_GROUP) -> None:
        self._run = runner
        self._group = resource_group

    async def _json(self, arguments: Sequence[str]) -> Any:
        text = await self._run([*arguments, "-o", "json"])
        try:
            return json.loads(text) if text.strip() else None
        except ValueError as error:
            raise AzError(f"az {' '.join(arguments[:3])} didn't answer JSON") from error

    async def plan_id(self, app_name: str) -> str:
        """The API plan's resource ID, from the site."""
        value = await self._json(
            [
                "webapp",
                "show",
                "--resource-group",
                self._group,
                "--name",
                app_name,
                "--query",
                "serverFarmId",
            ]
        )
        if not isinstance(value, str) or not value:
            raise AzError(f"{app_name} names no App Service plan")
        return value

    async def instance_count(self, app_name: str) -> int:
        value = await self._json(
            [
                "webapp",
                "list-instances",
                "--resource-group",
                self._group,
                "--name",
                app_name,
                "--query",
                "length(@)",
            ]
        )
        if not isinstance(value, int):
            raise AzError(f"{app_name}'s instances couldn't be counted")
        return value

    async def cpu_minutes(self, plan_id: str, start: datetime, end: datetime) -> list[MetricPoint]:
        """The plan's 1-minute `CpuPercentage` averages from `start` to `end`."""
        body = await self._json(
            [
                "monitor",
                "metrics",
                "list",
                "--resource",
                plan_id,
                "--metric",
                "CpuPercentage",
                "--interval",
                "PT1M",
                "--aggregation",
                "Average",
                "--start-time",
                iso(start),
                "--end-time",
                iso(end),
            ]
        )
        return _parse_points(body)

    async def scale_ups(self, start: datetime, end: datetime) -> list[datetime]:
        """The `eventTimestamp`s of successful autoscale scale-ups in the resource group."""
        query = (
            f"[?operationName.value=='{SCALE_UP_OPERATION}' && status.value=='Succeeded']"
            ".eventTimestamp"
        )
        value = await self._json(
            [
                "monitor",
                "activity-log",
                "list",
                "--resource-group",
                self._group,
                "--start-time",
                iso(start),
                "--end-time",
                iso(end),
                "--query",
                query,
            ]
        )
        return sorted(datetime.fromisoformat(str(item)) for item in value or [])

    async def max_throughput(self, account: str, container: str) -> int:
        """A Cosmos DB container's provisioned autoscale maximum, in RU/s."""
        value = await self._json(
            [
                "cosmosdb",
                "sql",
                "container",
                "throughput",
                "show",
                "--account-name",
                account,
                "--resource-group",
                self._group,
                "--database-name",
                COSMOS_DATABASE,
                "--name",
                container,
                "--query",
                "resource.autoscaleSettings.maxThroughput",
            ]
        )
        if not isinstance(value, int):
            raise AzError(f"{container} has no autoscale maximum")
        return value
