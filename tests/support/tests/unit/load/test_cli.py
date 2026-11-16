from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from docchat_testing import load
from docchat_testing.clock import FakeClock
from docchat_testing.load import cli
from docchat_testing.load.azcli import Azure
from docchat_testing.load.common import LoadError, UsageError
from docchat_testing.load.verdict import Sources
from docchat_testing.logquery import Rows

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_the_console_script_is_the_clis_main() -> None:
    assert load.main is cli.main
    assert set(cli.HANDLERS) == {
        "tokens",
        "aci-params",
        "verdict",
        "s3-rate",
        "probe-alerts",
        "seed",
        "build-corpus",
        "ingest",
    }


def test_the_commands_take_the_arguments_the_workflow_passes() -> None:
    tokens = parse(
        "tokens", "--env", "load", "--aliases", "load-01..load-20,load-admin", "--out", "t.json"
    )
    assert tokens.aliases[-1] == "load-admin"
    assert len(tokens.aliases) == 21
    verdict = parse(
        "verdict",
        "--scenario",
        "s2",
        "--k6-log",
        "load-results/s2/k6.log",
        "--start",
        "2026-11-03T10:00:00Z",
        "--end",
        "2026-11-03T10:09:00Z",
        "--run-start",
        "2026-11-03T08:00:00Z",
        "--removed-at",
        "2026-11-03T10:04:20Z",
        "--out",
        "load-results/s2/verdict.json",
    )
    assert verdict.removed_at == datetime(2026, 11, 3, 10, 4, 20, tzinfo=UTC)
    rate = parse(
        "s3-rate",
        "--start",
        "2026-11-03T10:00:00Z",
        "--answer-tokens",
        "320",
        "--rate",
        "23",
        "--out",
        "r.json",
    )
    assert (rate.answer_tokens, rate.rate) == (320, 23)
    corpus = parse("build-corpus", "--variant", "b", "--count", "200", "--out", "c")
    assert (corpus.variant, corpus.count) == ("b", 200)
    ingest = parse(
        "ingest",
        "--env",
        "load",
        "--variant",
        "a",
        "--index",
        "load-ingest-a",
        "--corpus",
        "c",
        "--out",
        "o",
    )
    assert ingest.index == "load-ingest-a"


@pytest.mark.parametrize(
    "argv",
    [
        ("tokens", "--env", "dev", "--aliases", "load-01", "--out", "t"),
        ("s3-rate", "--start", "2026-11-03T10:00:00Z", "--answer-tokens", "79", "--out", "r"),
        ("s3-rate", "--start", "2026-11-03T10:00:00Z", "--answer-tokens", "4001", "--out", "r"),
        (
            "s3-rate",
            "--start",
            "2026-11-03T10:00:00Z",
            "--answer-tokens",
            "80",
            "--rate",
            "0",
            "--out",
            "r",
        ),
        (
            "aci-params",
            "--scenario",
            "s4",
            "--bootstrap",
            "b",
            "--script",
            "s",
            "--lib",
            "l",
            "--tokens",
            "t",
            "--out",
            "o",
        ),
        ("verdict", "--scenario", "s5", "--run-start", "2026-11-03T10:00:00Z", "--out", "o"),
    ],
)
def test_a_refused_argument_exits_2(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as raised:
        parse(*argv)
    assert raised.value.code == 2


def test_failures_end_as_one_json_line_with_their_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def usage(arguments: argparse.Namespace) -> int:
        raise UsageError("image_invalid", "no digest")

    async def failure(arguments: argparse.Namespace) -> int:
        raise LoadError("seed_failed", "1 session failed")

    async def crash(arguments: argparse.Namespace) -> int:
        raise KeyError("runId")

    argv = ["build-corpus", "--variant", "a", "--count", "10", "--out", "x"]
    assert cli.main(argv, handlers={"build-corpus": usage}) == 2
    assert json.loads(capsys.readouterr().err) == {"code": "image_invalid", "message": "no digest"}
    assert cli.main(argv, handlers={"build-corpus": failure}) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "seed_failed"
    assert cli.main(argv, handlers={"build-corpus": crash}) == 1
    assert json.loads(capsys.readouterr().err) == {
        "code": "internal_error",
        "message": "KeyError: 'runId'",
    }


def test_build_corpus_writes_the_corpus(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli.main(["build-corpus", "--variant", "a", "--count", "10", "--out", str(tmp_path)]) == 0
    )
    assert len(list(tmp_path.glob("load-a-*"))) == 10
    assert "wrote 10 files" in capsys.readouterr().out


def test_aci_params_writes_a_private_parameters_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"tokens": {"load-01": "t"}}), "utf-8")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "7")
    monkeypatch.setenv("AZURE_LOCATION", "eastus2")
    monkeypatch.setenv("API_BASE_URL", "https://api.example")
    out = tmp_path / "loadgen.parameters.json"
    argv = [
        "aci-params",
        "--scenario",
        "s1",
        "--bootstrap",
        str(REPOSITORY_ROOT / "tests/load/aci/run.py"),
    ]
    argv += ["--script", str(REPOSITORY_ROOT / "tests/load/k6/s1-event-loop.js")]
    argv += ["--lib", str(REPOSITORY_ROOT / "tests/load/k6/lib.js"), "--tokens", str(tokens)]
    argv += ["--out", str(out)]
    assert cli.main(argv) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert (
        json.loads(out.read_text("utf-8"))["parameters"]["name"]["value"]
        == "ci-docchat-loadgen-s1-7"
    )


class NoAz(Azure):
    def __init__(self) -> None:
        async def refuse(arguments: object) -> str:
            raise AssertionError(arguments)

        super().__init__(refuse)


def recorded_sources(rows: Mapping[str, Rows]) -> Sources:
    clock = FakeClock(datetime(2026, 11, 3, 18, 0, tzinfo=UTC))

    async def query(kql: str, timespan: str) -> Rows:
        return next((found for marker, found in rows.items() if marker in kql), [])

    return Sources(query=query, azure=NoAz(), sleep=clock.sleep, now=clock.now)


def write_set(directory: Path, created: str, completed: str) -> None:
    directory.mkdir(parents=True)
    runs = [
        {
            "runId": f"r{n}",
            "state": "completed",
            "createdAt": created,
            "completedAt": completed,
            "counts": {"succeeded": 100, "failed": 0},
        }
        for n in (1, 2)
    ]
    result = {"runs": runs, "setStart": created, "setEnd": completed}
    (directory / "ingest.json").write_text(json.dumps(result), "utf-8")


def test_an_s4_verdict_runs_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_set(tmp_path / "s4-set1", "2026-11-03T10:00:00Z", "2026-11-03T14:00:00Z")
    write_set(tmp_path / "s4-set2", "2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z")
    rows: dict[str, Rows] = {
        "union AppRequests": [{"Count": 1}],
        "docchat.ingestion.throttled": [{"n": 12}],
        "_LogOperation": [{"Count": 0}],
    }
    monkeypatch.setattr(cli, "_azd", dict)
    monkeypatch.setattr(cli, "_sources", lambda azd: recorded_sources(rows))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "s4-set2" / "verdict.json"
    argv = ["verdict", "--scenario", "s4", "--set", "2", "--results", str(tmp_path / "s4-set2")]
    argv += ["--set1-results", str(tmp_path / "s4-set1"), "--run-start", "2026-11-03T08:00:00Z"]
    argv += ["--out", str(out)]

    assert cli.main(argv) == 0
    assert json.loads(out.read_text("utf-8"))["status"] == "passed"

    rows["docchat.ingestion.throttled"] = [{"n": 0}]
    assert cli.main(argv) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["verdict", "--scenario", "s4", "--run-start", "2026-11-03T08:00:00Z", "--out", "v.json"],
        ["verdict", "--scenario", "s1", "--run-start", "2026-11-03T08:00:00Z", "--out", "v.json"],
    ],
)
def test_a_verdict_without_its_scenarios_inputs_exits_2(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_azd", dict)
    monkeypatch.setattr(cli, "_sources", lambda azd: recorded_sources({}))
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "argument_missing"


def test_the_probe_phases_need_their_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["probe-alerts", "--phase", "send", "--out", "x"]) == 2
    assert cli.main(["probe-alerts", "--phase", "check", "--out", "x"]) == 2
    errors = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {error["code"] for error in errors} == {"argument_missing"}


def test_sources_query_the_workspace_of_the_azd_file(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, Any]] = []

    async def fake_query(workspace: str, kql: str, timespan: Any) -> Rows:
        seen.append((workspace, kql, timespan))
        return [{"Count": 1}]

    monkeypatch.setattr(cli, "query_logs", fake_query)
    monkeypatch.delenv("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID", raising=False)
    workspace = "11111111-2222-3333-4444-555555555555"
    sources = cli._sources({"DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID": workspace})

    async def ask() -> Rows:
        return await sources.query("AppRequests | count", "a/b")

    assert asyncio.run(ask()) == [{"Count": 1}]
    assert seen == [(workspace, "AppRequests | count", "a/b")]
