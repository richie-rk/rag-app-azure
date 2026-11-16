"""Unit tests of the azd `postdeploy` hook (T-461, `83-infrastructure.md` section 9.3)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[5]
SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
RELEASE = "89abcdef0123456789abcdef0123456789abcdef"
API = f"crdocchatabc123.azurecr.io/docchat/api@sha256:{SHA}"
INGESTION = f"crdocchatabc123.azurecr.io/docchat/ingestion@sha256:{SHA}"
API_ID = "/subscriptions/s/resourceGroups/rg-docchat-dev/providers/Microsoft.Web/sites/app"
INGESTION_ID = "/subscriptions/s/resourceGroups/rg-docchat-dev/providers/Microsoft.Web/sites/func"
WORKSPACE = "11111111-2222-3333-4444-555555555555"
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_script("scripts/deploy/hooks/postdeploy.py", "docchat_script_postdeploy")


class FakeAzure:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.modified = {API_ID: "2026-10-30T12:00:05.1234567", INGESTION_ID: "2026-10-30T11:00:00"}
        self.live = {API_ID: f"DOCKER|{API}", INGESTION_ID: f"DOCKER|{INGESTION}"}
        self.health: list[float | None] = [100.0]
        self.rows: list[list[list[Any]]] = [[["ingestion_worker_started"]]]
        self.queries: list[dict[str, Any]] = []
        self.tags: list[str] = []

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        self.calls.append(words)
        joined = " ".join(words)
        routes = [
            ("az resource list", self._sites),
            ("az resource show --ids", lambda w: self._value(self.modified, w)),
            ("az resource show --resource-group", lambda w: WORKSPACE + "\n"),
            ("az webapp config show", lambda w: self._value(self.live, w)),
            ("az monitor metrics list", self._metrics),
            ("az rest --method post", self._query),
            ("az tag update", self._tag),
        ]
        for prefix, answer in routes:
            if joined.startswith(prefix):
                return hook.CommandResult(0, answer(words), "")
        raise AssertionError(f"unexpected command {joined}")

    @staticmethod
    def _sites(words: list[str]) -> str:
        service = words[words.index("--tag") + 1].removeprefix("azd-service-name=")
        return json.dumps([{"api": API_ID, "ingestion": INGESTION_ID}[service]])

    @staticmethod
    def _value(table: dict[str, str], words: list[str]) -> str:
        return table[words[words.index("--ids") + 1]] + "\n"

    def _metrics(self, words: list[str]) -> str:
        value = self.health.pop(0) if len(self.health) > 1 else self.health[0]
        data = [{"timeStamp": "2026-10-30T12:01:00Z", "average": value}]
        return json.dumps({"value": [{"timeseries": [{"data": data}]}]})

    def _query(self, words: list[str]) -> str:
        body = words[words.index("--body") + 1]
        self.queries.append(json.loads(Path(body.removeprefix("@")).read_text()))
        rows = self.rows.pop(0) if len(self.rows) > 1 else self.rows[0]
        return json.dumps({"tables": [{"name": "PrimaryResult", "rows": rows}]})

    def _tag(self, words: list[str]) -> str:
        self.tags.append(words[words.index("--tags") + 1])
        return ""


class Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(hook, "sleep", fake.sleep)
    monkeypatch.setattr(hook, "monotonic", lambda: fake.elapsed)
    monkeypatch.setattr(hook, "now", lambda: START + timedelta(seconds=fake.elapsed))
    return fake


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> FakeAzure:
    fake = FakeAzure()
    monkeypatch.setattr(hook, "run_command", fake)
    for name, value in {
        "AZURE_ENV_NAME": "dev",
        "DOCCHAT_RELEASE_SHA": RELEASE,
        "DOCCHAT_API_IMAGE": API,
        "DOCCHAT_INGESTION_IMAGE": INGESTION,
        "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID": WORKSPACE,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return fake


def _after_ingestion_deploy(azure: FakeAzure) -> None:
    azure.modified[INGESTION_ID] = "2026-10-30T12:03:00.5000000"


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(capsys.readouterr().err.strip())
    return parsed


def test_after_azd_deploy_api_the_hook_checks_the_api_and_tags_nothing(
    azure: FakeAzure, clock: Clock
) -> None:
    azure.health = [0.0, 0.0, 100.0]

    assert hook.main() == 0

    assert clock.sleeps == [30.0, 30.0]
    assert azure.tags == []
    assert azure.queries == []
    metrics = next(call for call in azure.calls if call[:3] == ["az", "monitor", "metrics"])
    assert metrics[metrics.index("--metric") + 1] == "HealthCheckStatus"
    assert metrics[metrics.index("--start-time") + 1] == "2026-10-30T12:00:05Z"


def test_t461_postdeploy_fails_a_mismatched_image(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.live[API_ID] = f"DOCKER|crdocchatabc123.azurecr.io/docchat/api@sha256:{'f' * 64}"

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "deploy_verification_failed"
    assert azure.tags == []


def test_t461_postdeploy_fails_a_mismatched_ingestion_image(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    _after_ingestion_deploy(azure)
    azure.live[INGESTION_ID] = "DOCKER|mcr.microsoft.com/azure-functions/python:4"

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "deploy_verification_failed"
    assert azure.tags == []


def test_t461_postdeploy_fails_an_app_not_healthy_within_10_minutes(
    azure: FakeAzure, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.health = [0.0]

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "health_check_failed"
    assert clock.sleeps == [30.0] * 20
    assert clock.elapsed == 600.0


def test_postdeploy_treats_a_missing_metric_as_not_yet_healthy(
    azure: FakeAzure, clock: Clock
) -> None:
    azure.health = [None, 100.0]

    assert hook.main() == 0

    assert clock.sleeps == [30.0]


def test_after_azd_deploy_ingestion_the_hook_waits_for_the_worker_and_tags_the_release(
    azure: FakeAzure, clock: Clock
) -> None:
    _after_ingestion_deploy(azure)
    azure.rows = [[], [], [["ingestion_worker_started"]]]

    assert hook.main() == 0

    assert clock.sleeps == [30.0, 30.0]
    assert azure.tags == [f"docchat-release-sha={RELEASE}"]
    tag = next(call for call in azure.calls if call[:3] == ["az", "tag", "update"])
    assert tag[tag.index("--resource-id") + 1] == API_ID
    assert tag[tag.index("--operation") + 1] == "Merge"
    query = azure.queries[0]
    assert query["timespan"] == "PT1H"
    assert "TimeGenerated > datetime(2026-10-30T12:03:00Z)" in query["query"]
    assert f"AppVersion == '{RELEASE}'" in query["query"]
    assert "Name == 'ingestion_worker_started'" in query["query"]
    post = next(call for call in azure.calls if call[:3] == ["az", "rest", "--method"])
    assert post[post.index("--uri") + 1] == (
        f"https://api.loganalytics.io/v1/workspaces/{WORKSPACE}/query"
    )
    assert post[post.index("--resource") + 1] == "https://api.loganalytics.io"


def test_t461_postdeploy_fails_with_ingestion_start_failed_after_10_minutes(
    azure: FakeAzure, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    _after_ingestion_deploy(azure)
    azure.rows = [[]]

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "ingestion_start_failed"
    assert clock.elapsed == 600.0
    assert len(azure.queries) == 21
    assert azure.tags == []


def test_postdeploy_reads_the_workspace_when_the_outputs_are_absent(
    monkeypatch: pytest.MonkeyPatch, azure: FakeAzure
) -> None:
    monkeypatch.delenv("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID")
    _after_ingestion_deploy(azure)

    assert hook.main() == 0

    show = next(call for call in azure.calls if "--resource-type" in call)
    assert show[show.index("--name") + 1] == "log-docchat-dev"
    assert show[show.index("--query") + 1] == "properties.customerId"


@pytest.mark.parametrize("release", ["local", "", "ABCDEF" * 7])
def test_postdeploy_needs_a_release_commit(
    monkeypatch: pytest.MonkeyPatch,
    azure: FakeAzure,
    capsys: pytest.CaptureFixture[str],
    release: str,
) -> None:
    monkeypatch.setenv("DOCCHAT_RELEASE_SHA", release)

    assert hook.main() == 2

    assert _failure(capsys)["code"] == "deploy_verification_failed"
    assert azure.calls == []
