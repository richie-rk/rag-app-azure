"""T-583: S3's rate from recorded metric, KQL and throughput answers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

import pytest

from docchat_testing.clock import FakeClock
from docchat_testing.load.azcli import Azure, MetricPoint
from docchat_testing.load.s3rate import choose_rate, measure, token_delay_ms, write_rate
from docchat_testing.load.verdict import Sources
from docchat_testing.logquery import Rows

T0 = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
MAXIMA = {"conversations": 30000, "memories": 30000, "usage": 30000}


class Recorded(Azure):
    def __init__(self, cpu: Sequence[float | None], arrive_after: int = 0) -> None:
        super().__init__(self._refuse)
        self.cpu = list(cpu)
        self.reads = 0
        self.arrive_after = arrive_after

    @staticmethod
    async def _refuse(arguments: Sequence[str]) -> str:
        raise AssertionError(arguments)

    @override
    async def plan_id(self, app_name: str) -> str:
        return "plan"

    @override
    async def cpu_minutes(self, plan_id: str, start: datetime, end: datetime) -> list[MetricPoint]:
        self.reads += 1
        values = self.cpu if self.reads > self.arrive_after else [None] * len(self.cpu)
        return [
            MetricPoint(T0 + timedelta(minutes=1 + index), value)
            for index, value in enumerate(values)
        ]

    @override
    async def max_throughput(self, account: str, container: str) -> int:
        return MAXIMA[container]


def recorded_queries(screen: float | None, ru: dict[str, float]) -> Any:
    ran: list[str] = []

    async def query(kql: str, timespan: str) -> Rows:
        ran.append(kql)
        if "union AppRequests" in kql:
            return [{"Count": 50}]
        if "docchat.chat.screen" in kql:
            return [{"s": screen}] if screen is not None else []
        if "ruPerTurn" in kql:
            return [{"CollectionName": name, "ruPerTurn": value} for name, value in ru.items()]
        return []

    query.ran = ran  # type: ignore[attr-defined]
    return query


@pytest.mark.parametrize(
    ("c5", "screen", "ru", "rate_input", "expected"),
    [
        (40.0, 2.0, {"conversations": 250.0}, None, (11, "cpu")),
        (5.0, 4.0, {"conversations": 250.0}, None, (20, "promptShields")),
        (5.0, 1.0, {"conversations": 800.0, "usage": 100.0}, None, (30, "cosmos")),
        (5.0, 1.0, {"conversations": 250.0}, None, (60, "max")),
        (40.0, 2.0, {"conversations": 250.0}, 33, (33, "cpu")),
        (40.0, 2.0, {"conversations": 250.0}, 90, (40, "promptShields")),
    ],
    ids=["R_cpu", "R_ps", "R_db", "60", "input replaces R_cpu", "input still capped"],
)
def test_t583_r_is_the_smallest_bound(
    c5: float,
    screen: float,
    ru: dict[str, float],
    rate_input: int | None,
    expected: tuple[int, str],
) -> None:
    chosen = choose_rate(
        c5=c5, screen_calls=screen, ru_per_turn=ru, max_throughput=MAXIMA, rate_input=rate_input
    )
    assert (chosen["rate"], chosen["boundBy"]) == expected
    assert not chosen["inconclusive"]


def test_t583_the_bounds_follow_their_formulas() -> None:
    chosen = choose_rate(
        c5=37.0,
        screen_calls=3.0,
        ru_per_turn={"conversations": 300.0, "memories": 70.0, "usage": None},
        max_throughput=MAXIMA,
        rate_input=None,
    )
    # R_cpu = ceil(5 * 85 / 37) = 12; R_ps = floor(80 / 3) = 26; R_db = floor(30000 / 375) = 80.
    assert chosen["bounds"] == {"cpu": 12, "promptShields": 26, "cosmos": 80, "max": 60}
    assert chosen["cosmosBounds"] == {"conversations": 80, "memories": 342}


def test_t583_r_below_5_ends_s3_dependency_bound() -> None:
    chosen = choose_rate(
        c5=40.0,
        screen_calls=20.0,
        ru_per_turn={"conversations": 250.0},
        max_throughput=MAXIMA,
        rate_input=None,
    )
    assert chosen["rate"] == 4
    assert chosen["inconclusive"]
    assert chosen["cause"] == "dependency_bound"


def test_missing_measurements_dont_bound_r() -> None:
    chosen = choose_rate(
        c5=None, screen_calls=None, ru_per_turn={}, max_throughput=MAXIMA, rate_input=None
    )
    assert chosen["rate"] == 60


def test_the_token_delay_keeps_each_answer_near_800_ms() -> None:
    assert [token_delay_ms(n) for n in (80, 320, 1280, 4000)] == [10, 2, 1, 1]


async def test_t583_measure_reads_calibration_minutes_2_to_4_after_they_arrive() -> None:
    clock = FakeClock(T0 + timedelta(minutes=5))
    azure = Recorded([40.0, 44.0, 42.0], arrive_after=2)
    query = recorded_queries(2.0, {"conversations": 250.0, "memories": 40.0, "usage": 60.0})
    sources = Sources(query=query, azure=azure, sleep=clock.sleep, now=clock.now)

    rate = await measure(
        sources,
        start=T0,
        answer_tokens=80,
        rate_input=None,
        app_name="app",
        cosmos_account="cosmos",
    )

    assert clock.now() == T0 + timedelta(minutes=6)
    assert rate["measurements"]["c5"] == pytest.approx(42.0)
    assert rate["bounds"]["cpu"] == 11
    assert rate["rate"] == 11
    assert rate["measurements"]["maxThroughput"] == MAXIMA
    assert rate["measurements"]["screenCallsPerTurn"] == 2.0
    assert rate["tokenDelayMs"] == 10
    assert rate["calibrationStart"] == "2026-11-03T11:00:00.000Z"
    screen = next(kql for kql in query.ran if "docchat.chat.screen" in kql)
    assert "2026-11-03T11:01:00.000000Z" in screen
    assert "2026-11-03T11:04:00.000000Z" in screen


def test_t583_a_rate_below_5_writes_an_inconclusive_verdict(tmp_path: Path) -> None:
    chosen = choose_rate(
        c5=40.0, screen_calls=40.0, ru_per_turn={}, max_throughput=MAXIMA, rate_input=None
    )
    out = tmp_path / "s3" / "rate.json"
    verdict = write_rate(out, chosen | {"answerTokens": 80})
    assert json.loads(out.read_text("utf-8"))["rate"] == 2
    assert verdict is not None
    assert verdict.status == "inconclusive"
    assert verdict.cause == "dependency_bound"
    fine = choose_rate(
        c5=40.0, screen_calls=1.0, ru_per_turn={}, max_throughput=MAXIMA, rate_input=None
    )
    assert write_rate(out, fine) is None
