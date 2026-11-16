"""Unit tests of `scripts/deploy/ingestion_modes.py`: T-467 for `restart-ingestion`, and the
`reset-task-hub` mode (`83-infrastructure.md` section 10.13; ADR 0054)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
APP = "func-docchat-ingest-dev-abc123"
RELEASE = "89abcdef0123456789abcdef0123456789abcdef"
OTHER_RELEASE = "0123456789abcdef0123456789abcdef01234567"
WORKSPACE = "11111111-2222-3333-4444-555555555555"
HOST_ID = (
    "/subscriptions/s/resourceGroups/rg-docchat-dev/providers/Microsoft.Storage"
    "/storageAccounts/stdocchathostdevabc123"
)
RESTART = datetime(2026, 10, 31, 12, 0, 0, 500000, tzinfo=UTC)
COLUMNS = [
    {"name": "TimeGenerated", "type": "datetime"},
    {"name": "Name", "type": "string"},
    {"name": "AppRoleName", "type": "string"},
    {"name": "AppVersion", "type": "string"},
]


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


modes = _load_script("scripts/deploy/ingestion_modes.py", "docchat_script_ingestion_modes")


def _row(moment: str, version: str = RELEASE) -> list[Any]:
    return [moment, "ingestion_worker_started", "docchat-ingestion", version]


class FakeAzure:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.clock = 0.0
        self.tagged = {
            "azd-service-name=ingestion": APP,
            "azd-service-name=api": RELEASE,
            "resource-usage=azure-functions": HOST_ID,
        }
        self.failing: set[str] = set()
        # One Logs API answer per poll; the last one repeats.
        self.polls: list[list[list[Any]]] = [[_row("2026-10-31T12:01:10.1234567Z")]]
        self.queries: list[dict[str, Any]] = []
        self.storage: dict[str, list[list[str]]] = {
            "blobServices/default/containers": [
                ["azure-webjobs-hosts", "azure-webjobs-secrets", "docchatdev-leases"],
                ["docchatdev-largemessages", "docchatdev-applease"],
            ],
            "queueServices/default/queues": [
                ["docchatdev-control-00", "docchatdev-workitems", "other-queue"]
            ],
            "tableServices/default/tables": [["DocChatDevHistory", "docchatdevInstances", "Audit"]],
        }

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        self.calls.append(words)
        joined = " ".join(words)
        for marker in self.failing:
            if marker in joined:
                return modes.CommandResult(1, "", f"ERROR: {marker} refused")
        routes: list[tuple[str, Callable[[], str]]] = [
            ("az resource list", lambda: self.tagged.get(words[words.index("--tag") + 1], "")),
            ("az resource show", lambda: WORKSPACE),
            ("az functionapp", lambda: ""),
            ("az rest --method post", lambda: self._logs(words)),
            ("az rest --method get", lambda: json.dumps(self._page(words))),
            ("az rest --method delete", lambda: ""),
        ]
        for prefix, answer in routes:
            if joined.startswith(prefix):
                return modes.CommandResult(0, answer() + "\n", "")
        raise AssertionError(f"unexpected call: {joined}")

    def _logs(self, words: list[str]) -> str:
        body = words[words.index("--body") + 1].removeprefix("@")
        self.queries.append(json.loads(Path(body).read_text(encoding="utf-8")))
        rows = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return json.dumps({"tables": [{"name": "PrimaryResult", "columns": COLUMNS, "rows": rows}]})

    def _page(self, words: list[str]) -> dict[str, Any]:
        url = words[words.index("--url") + 1]
        service = next(service for service in self.storage if f"/{service}?" in url)
        pages = self.storage[service]
        index = int(url.rsplit("page=", 1)[1]) if "page=" in url else 0
        page: dict[str, Any] = {"value": [{"name": name} for name in pages[index]]}
        if index + 1 < len(pages):
            page["nextLink"] = f"{url.split('&page=')[0]}&page={index + 1}"
        return page

    def commands(self, prefix: str) -> list[list[str]]:
        return [call for call in self.calls if " ".join(call).startswith(prefix)]


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAzure:
    fake = FakeAzure()

    def sleep(seconds: float) -> None:
        fake.clock += seconds

    monkeypatch.setattr(modes, "run_command", fake)
    monkeypatch.setattr(modes, "sleep", sleep)
    monkeypatch.setattr(modes, "monotonic", lambda: fake.clock)
    monkeypatch.setattr(modes, "now", lambda: RESTART)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    line: dict[str, Any] = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    return line


# --- restart-ingestion (T-467) ------------------------------------------------------------------


def test_t467_restart_finds_the_app_by_its_tag_and_passes_on_a_later_start(
    azure: FakeAzure, tmp_path: Path
) -> None:
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 0
    (lookup, *_) = azure.commands("az resource list")
    assert lookup[lookup.index("--tag") + 1] == "azd-service-name=ingestion"
    assert lookup[lookup.index("--resource-group") + 1] == "rg-docchat-dev"
    (restart,) = azure.commands("az functionapp restart")
    assert restart[restart.index("--name") + 1] == APP
    (query,) = azure.queries
    assert query["timespan"] == "PT1H"
    assert f"datetime({modes.iso(RESTART)})" in query["query"]
    assert f"AppVersion == '{RELEASE}'" in query["query"]
    assert "Name == 'ingestion_worker_started'" in query["query"]
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert APP in summary
    assert "2026-10-31T12:00:00.500000Z" in summary
    assert "2026-10-31T12:01:10.123456Z" in summary


def test_t467_an_earlier_start_of_the_release_is_ignored(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    # A worker that started before the restart, even within the same second, proves nothing.
    azure.polls = [[_row("2026-10-31T11:59:30Z"), _row("2026-10-31T12:00:00.2Z")]]
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "ingestion_start_failed"
    # Polled at once and then every 30 s until 10 minutes have passed.
    assert len(azure.queries) == 21


def test_t467_waits_until_the_later_start_arrives(azure: FakeAzure, tmp_path: Path) -> None:
    azure.polls = [
        [_row("2026-10-31T11:59:30Z")],
        [_row("2026-10-31T11:59:30Z")],
        [_row("2026-10-31T11:59:30Z"), _row("2026-10-31T12:02:00Z")],
    ]
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 0
    assert len(azure.queries) == 3
    assert "2026-10-31T12:02:00.000000Z" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_t467_a_start_of_another_release_is_ignored(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.polls = [[_row("2026-10-31T12:01:00Z", OTHER_RELEASE)]]
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "ingestion_start_failed"


def test_t467_a_refused_restart_changes_nothing_else(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.failing = {"functionapp restart"}
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 2
    failure = _failure(capsys)
    assert failure["code"] == "ingestion_restart_failed"
    assert failure["message"] == modes.MESSAGES["ingestion_restart_failed"]
    assert azure.queries == []


def test_t467_no_tagged_app_fails_before_any_restart(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.tagged.pop("azd-service-name=ingestion")
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "ingestion_restart_failed"
    assert azure.commands("az functionapp") == []


def test_restart_needs_the_api_site_release_tag(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.tagged["azd-service-name=api"] = "local"
    assert modes.main(["restart-ingestion", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "ingestion_start_failed"
    assert azure.queries == []


def test_parse_time_reads_seven_digit_fractions() -> None:
    assert modes.parse_time("2026-10-31T12:01:10.1234567Z") == datetime(
        2026, 10, 31, 12, 1, 10, 123456, tzinfo=UTC
    )
    assert modes.parse_time("2026-10-31T12:01:10Z") == datetime(2026, 10, 31, 12, 1, 10, tzinfo=UTC)


# --- reset-task-hub -----------------------------------------------------------------------------


def _deleted(azure: FakeAzure) -> list[str]:
    return [
        call[call.index("--url") + 1].split("?")[0].rsplit("/", 2)[-1]
        for call in azure.commands("az rest --method delete")
    ]


def test_reset_deletes_only_the_hub_storage_between_stop_and_start(
    azure: FakeAzure, tmp_path: Path
) -> None:
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 0
    assert _deleted(azure) == [
        "docchatdev-leases",
        "docchatdev-largemessages",
        "docchatdev-applease",
        "docchatdev-control-00",
        "docchatdev-workitems",
        "DocChatDevHistory",
        "docchatdevInstances",
    ]
    order = [" ".join(call[:3]) for call in azure.calls if call[1] in {"functionapp", "rest"}]
    assert order[0] == "az functionapp stop"
    assert order[-1] == "az functionapp start"
    for call in azure.commands("az functionapp"):
        assert call[call.index("--name") + 1] == APP
    for call in azure.commands("az rest"):
        url = call[call.index("--url") + 1]
        assert url.startswith(f"https://management.azure.com{HOST_ID}/")
        assert "api-version=2025-06-01" in url
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| containers/docchatdev-leases |" in summary
    assert "| tables/DocChatDevHistory |" in summary
    assert "azure-webjobs-hosts" not in summary


def test_reset_finds_the_host_account_by_its_tag(azure: FakeAzure) -> None:
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 0
    tags = [call[call.index("--tag") + 1] for call in azure.commands("az resource list")]
    assert tags == ["azd-service-name=ingestion", "resource-usage=azure-functions"]


def test_a_failed_deletion_leaves_the_app_stopped(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.failing = {"queues/docchatdev-workitems"}
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 2
    failure = _failure(capsys)
    assert failure["code"] == "task_hub_reset_failed"
    assert "containers/docchatdev-leases" in failure["detail"]
    assert azure.commands("az functionapp start") == []
    assert len(azure.commands("az functionapp stop")) == 1


def test_a_failed_listing_leaves_the_app_stopped(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.failing = {"tableServices/default/tables?"}
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "task_hub_reset_failed"
    assert azure.commands("az functionapp start") == []


def test_a_refused_stop_deletes_nothing(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.failing = {"functionapp stop"}
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 2
    assert _failure(capsys)["code"] == "task_hub_reset_failed"
    assert azure.commands("az rest") == []


def test_an_empty_hub_still_restarts_the_app(azure: FakeAzure, tmp_path: Path) -> None:
    azure.storage = {service: [["azure-webjobs-hosts"]] for service in azure.storage}
    assert modes.main(["reset-task-hub", "--env", "dev"]) == 0
    assert _deleted(azure) == []
    assert len(azure.commands("az functionapp start")) == 1
    assert "the hub held no storage" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_load_is_not_a_dispatch_environment(azure: FakeAzure) -> None:
    with pytest.raises(SystemExit):
        modes.main(["reset-task-hub", "--env", "load"])
    assert azure.calls == []
