"""`docchat-load s3-rate` (`84-testing.md` section 7.5, S3 step 1; ADR 0151; T-583).

After the 4-minute calibration at 5 turns per second, it waits until the metrics and logs of
calibration minutes 2 to 4 have arrived (every 30 s, at most 10 minutes) and reads, over
[T0 + 1 min, T0 + 4 min]:

- C5, the mean 1-minute `CpuPercentage` of the API plan;
- s, the mean Prompt Shields calls per turn (`docchat.calls` on the span `docchat.chat.screen`);
- ru[c], each container's request charge per completed turn (S1-h's query);
- T[c], each of `conversations`, `memories` and `usage`'s provisioned autoscale maximum.

R = min(R_cpu, R_ps, R_db, 60) turns per second, with R_cpu = ceil(5 * 85 / C5) (the input rate in
its place when given), R_ps = floor(80 / s) and R_db the smallest floor(T[c] / (1.25 * ru[c])).
Every value goes into `rate.json`, whose measurements the S3 verdict carries. R below 5, the
calibration rate, ends S3 before the main phase: `verdict.json` beside `rate.json` is written
`inconclusive` with cause `dependency_bound`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from docchat_testing.load import queries
from docchat_testing.load.common import iso, write_json
from docchat_testing.load.verdict import Sources, Verdict

CALIBRATION_RATE: Final = 5
CPU_TARGET: Final = 85.0
SCREENING_CALLS_PER_SECOND: Final = 80.0
"""Prompt Shields S0's 100 requests a second, with 25% headroom."""
COSMOS_HEADROOM: Final = 1.25
MAX_RATE: Final = 60
CONTAINERS: Final = ("conversations", "memories", "usage")
WINDOW_START: Final = timedelta(minutes=1)
WINDOW_END: Final = timedelta(minutes=4)
POLL_SECONDS: Final = 30.0
POLL_LIMIT: Final = timedelta(minutes=10)
CALIBRATION_MINUTES: Final = 3


def token_delay_ms(answer_tokens: int) -> int:
    """d = max(1, floor(800 / L)) ms, so each answer streams for about 800 ms."""
    return max(1, 800 // answer_tokens)


def choose_rate(
    *,
    c5: float | None,
    screen_calls: float | None,
    ru_per_turn: Mapping[str, float | None],
    max_throughput: Mapping[str, int],
    rate_input: int | None,
) -> dict[str, Any]:
    """R and its bounds; a bound whose measurement is zero or missing doesn't limit R."""
    if rate_input is not None:
        r_cpu = rate_input
    elif c5 is not None and c5 > 0:
        r_cpu = math.ceil(CALIBRATION_RATE * CPU_TARGET / c5)
    else:
        r_cpu = MAX_RATE
    r_ps = math.floor(SCREENING_CALLS_PER_SECOND / screen_calls) if screen_calls else MAX_RATE
    per_container = {
        name: math.floor(max_throughput[name] / (COSMOS_HEADROOM * per_turn))
        for name, per_turn in ru_per_turn.items()
        if per_turn and name in max_throughput
    }
    r_db = min(per_container.values(), default=MAX_RATE)
    bounds = {"cpu": r_cpu, "promptShields": r_ps, "cosmos": r_db, "max": MAX_RATE}
    rate = min(bounds.values())
    bound_by = next(name for name, value in bounds.items() if value == rate)
    return {
        "rate": rate,
        "bounds": bounds,
        "boundBy": bound_by,
        "cosmosBounds": per_container,
        "inconclusive": rate < CALIBRATION_RATE,
        "cause": "dependency_bound" if rate < CALIBRATION_RATE else None,
    }


async def _arrived(sources: Sources, plan_id: str, start: datetime) -> bool:
    """Calibration minutes 2 to 4 have their CPU averages and their last minute's logs."""
    points = await sources.azure.cpu_minutes(plan_id, start + WINDOW_START, start + WINDOW_END)
    complete = [point for point in points if point.value is not None]
    count = await sources.scalar(queries.telemetry_arrived(start + WINDOW_END), start, "Count")
    return len(complete) >= CALIBRATION_MINUTES and bool(count)


async def measure(
    sources: Sources,
    *,
    start: datetime,
    answer_tokens: int,
    rate_input: int | None,
    app_name: str,
    cosmos_account: str,
) -> dict[str, Any]:
    """The rate file's content."""
    plan_id = await sources.azure.plan_id(app_name)
    deadline = sources.now() + POLL_LIMIT
    arrived = await _arrived(sources, plan_id, start)
    while not arrived and sources.now() < deadline:
        await sources.sleep(POLL_SECONDS)
        arrived = await _arrived(sources, plan_id, start)
    window_start, window_end = start + WINDOW_START, start + WINDOW_END
    points = await sources.azure.cpu_minutes(plan_id, window_start, window_end)
    values = [point.value for point in points if point.value is not None]
    c5 = sum(values) / len(values) if values else None
    screen = await sources.scalar(queries.screen_calls(window_start, window_end), start, "s")
    ru_rows = await sources.rows(queries.request_units(window_start, window_end), start)
    ru_per_turn = {
        str(row["CollectionName"]): (
            float(row["ruPerTurn"]) if isinstance(row.get("ruPerTurn"), int | float) else None
        )
        for row in ru_rows
    }
    maxima = {name: await sources.azure.max_throughput(cosmos_account, name) for name in CONTAINERS}
    chosen = choose_rate(
        c5=c5,
        screen_calls=screen,
        ru_per_turn={name: ru_per_turn.get(name) for name in CONTAINERS},
        max_throughput=maxima,
        rate_input=rate_input,
    )
    return chosen | {
        "rateInput": rate_input,
        "answerTokens": answer_tokens,
        "tokenDelayMs": token_delay_ms(answer_tokens),
        "calibrationStart": iso(start),
        "measurements": {
            "telemetryArrived": arrived,
            "c5": c5,
            "cpuMinutes": values,
            "screenCallsPerTurn": screen,
            "ruPerTurn": ru_per_turn,
            "maxThroughput": maxima,
        },
    }


def write_rate(out: Path, rate: Mapping[str, Any]) -> Verdict | None:
    """Write `rate.json`; when R is below 5 also the scenario's `inconclusive` verdict."""
    write_json(out, dict(rate))
    if not rate["inconclusive"]:
        return None
    verdict = Verdict("s3", inconclusive=True, cause="dependency_bound")
    verdict.measurements["rate"] = dict(rate)
    verdict.add(
        "S3-rate",
        False,
        f"R = {rate['rate']} turns/s is below the calibration rate of 5 ({rate['boundBy']} bound)",
    )
    return verdict
