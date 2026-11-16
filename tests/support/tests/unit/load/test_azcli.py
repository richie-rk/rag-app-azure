from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docchat_testing.load import azcli
from docchat_testing.load.azcli import SCALE_UP_OPERATION, AzError, Azure, MetricPoint, run_az

START = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
END = datetime(2026, 11, 3, 10, 15, tzinfo=UTC)
PLAN = "/subscriptions/s/resourceGroups/rg-docchat-load/providers/Microsoft.Web/serverfarms/asp"


class FakeAz:
    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    async def __call__(self, arguments: Sequence[str]) -> str:
        self.calls.append(list(arguments))
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


async def test_the_plan_and_the_instances_come_from_the_site() -> None:
    fake = FakeAz(json.dumps(PLAN), 2)
    azure = Azure(fake)
    assert await azure.plan_id("app-docchat-api-load-abc") == PLAN
    assert await azure.instance_count("app-docchat-api-load-abc") == 2
    assert fake.calls[0][:2] == ["webapp", "show"]
    assert fake.calls[0][-4:] == ["--query", "serverFarmId", "-o", "json"]
    assert fake.calls[1][:2] == ["webapp", "list-instances"]
    assert "length(@)" in fake.calls[1]
    assert "rg-docchat-load" in fake.calls[1]


async def test_cpu_minutes_are_read_as_1_minute_averages() -> None:
    body = {
        "value": [
            {
                "timeseries": [
                    {
                        "data": [
                            {"timeStamp": "2026-11-03T10:01:00Z", "average": 75.5},
                            {"timeStamp": "2026-11-03T10:00:00Z", "average": 70},
                            {"timeStamp": "2026-11-03T10:02:00Z"},
                        ]
                    }
                ]
            }
        ]
    }
    fake = FakeAz(body)
    points = await Azure(fake).cpu_minutes(PLAN, START, END)
    assert points == [
        MetricPoint(datetime(2026, 11, 3, 10, 0, tzinfo=UTC), 70.0),
        MetricPoint(datetime(2026, 11, 3, 10, 1, tzinfo=UTC), 75.5),
        MetricPoint(datetime(2026, 11, 3, 10, 2, tzinfo=UTC), None),
    ]
    call = fake.calls[0]
    for expected in ("CpuPercentage", "PT1M", "Average", "2026-11-03T10:00:00.000Z"):
        assert expected in call


async def test_scale_ups_are_the_successful_autoscale_events() -> None:
    fake = FakeAz(["2026-11-03T10:08:00Z", "2026-11-03T10:07:30Z"])
    found = await Azure(fake).scale_ups(START, END)
    assert found == [
        datetime(2026, 11, 3, 10, 7, 30, tzinfo=UTC),
        datetime(2026, 11, 3, 10, 8, tzinfo=UTC),
    ]
    query = fake.calls[0][fake.calls[0].index("--query") + 1]
    assert SCALE_UP_OPERATION in query
    assert "Succeeded" in query
    assert await Azure(FakeAz("")).scale_ups(START, END) == []


async def test_a_containers_autoscale_maximum_is_read() -> None:
    fake = FakeAz(30000)
    assert await Azure(fake).max_throughput("cosmos-docchat-load-abc", "usage") == 30000
    assert fake.calls[0][:5] == ["cosmosdb", "sql", "container", "throughput", "show"]
    assert "resource.autoscaleSettings.maxThroughput" in fake.calls[0]


@pytest.mark.parametrize(
    ("method", "arguments", "answer"),
    [
        ("plan_id", ("app",), ""),
        ("instance_count", ("app",), "two"),
        ("max_throughput", ("account", "usage"), None),
    ],
)
async def test_an_unexpected_answer_is_an_az_error(
    method: str, arguments: tuple[str, ...], answer: object
) -> None:
    fake = FakeAz(json.dumps(answer) if answer is not None else "null")
    with pytest.raises(AzError):
        await getattr(Azure(fake), method)(*arguments)
    with pytest.raises(AzError, match="didn't answer JSON"):
        await Azure(FakeAz("not json")).plan_id("app")


@pytest.mark.allow_blocking("starts a child process")
async def test_run_az_returns_standard_output_and_names_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_az = tmp_path / "az"
    fake_az.write_text(
        '#!/bin/sh\nif [ "$1" = fail ]; then echo "ERROR: denied" >&2; exit 3; fi\necho "$@"\n',
        encoding="utf-8",
    )
    fake_az.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert (await run_az(["webapp", "show"])).strip() == "webapp show"
    with pytest.raises(AzError, match="exited 3: ERROR: denied"):
        await run_az(["fail", "now"])


async def test_run_az_needs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(AzError, match="isn't on PATH"):
        await azcli.run_az(["version"])
