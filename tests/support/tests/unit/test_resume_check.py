from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import pytest

from docchat_testing import resume_check
from docchat_testing.logquery import Rows
from docchat_testing.resume_check import (
    POLL_SECONDS,
    after_restart_query,
    check_resume,
    finished_query,
    main,
    parse_restarted_at,
    parse_run_ids,
    repeated_query,
)

WORKSPACE = "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
RUN_1 = "0b5f1c7e-2d3a-4e5f-8a9b-0c1d2e3f4a5b"
RUN_2 = "9e8d7c6b-5a4f-4e3d-9c2b-1a0f9e8d7c6b"
RUNS = [RUN_1, RUN_2]
RESTARTED = datetime(2026, 10, 30, 2, 41, 7, tzinfo=UTC)


def event(
    run_id: str, state: str = "completed", total: int = 4, succeeded: int = 4
) -> dict[str, object]:
    return {"runId": run_id, "state": state, "itemsTotal": total, "itemsSucceeded": succeeded}


class RecordedLogs:
    """Answers each of the three queries from recorded rows, polls of the finished events in
    turn, and records the waits instead of sleeping."""

    def __init__(self, finished: list[Rows], repeated: int = 0, after: int = 3) -> None:
        self.finished = finished
        self.answers = {
            repeated_query(RUNS, RESTARTED): [{"Count": repeated}],
            after_restart_query(RUNS, RESTARTED): [{"Count": after}],
        }
        self.queries: list[str] = []
        self.waits: list[float] = []
        self.now = 0.0

    async def run_query(self, workspace: str, kql: str, timespan: str) -> Rows:
        assert workspace == WORKSPACE
        assert timespan == "P1D"
        self.queries.append(kql)
        if kql == finished_query(RUNS):
            return self.finished.pop(0) if len(self.finished) > 1 else self.finished[0]
        return self.answers[kql]

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


async def run(logs: RecordedLogs) -> resume_check.Verdict:
    return await check_resume(
        WORKSPACE,
        RUNS,
        RESTARTED,
        run_query=logs.run_query,
        sleep=logs.sleep,
        monotonic=logs.monotonic,
    )


async def test_t585_passes_when_no_finished_activity_ran_again() -> None:
    logs = RecordedLogs([[], [event(RUN_1)], [event(RUN_1), event(RUN_2, total=1, succeeded=1)]])
    verdict = await run(logs)
    assert verdict.passed
    assert logs.waits == [POLL_SECONDS, POLL_SECONDS]
    assert logs.queries[-2:] == [
        repeated_query(RUNS, RESTARTED),
        after_restart_query(RUNS, RESTARTED),
    ]


@pytest.mark.parametrize(
    "second",
    [
        event(RUN_2, state="completed_with_errors", succeeded=3),
        event(RUN_2, state="failed", succeeded=0),
        event(RUN_2, state="completed", total=4, succeeded=3),
    ],
)
async def test_t585_fails_r_a_unless_both_runs_complete_every_item(
    second: dict[str, object],
) -> None:
    verdict = await run(RecordedLogs([[event(RUN_1), second]]))
    assert not verdict.passed
    assert set(verdict.failures) == {"R-a"}
    assert set(verdict.failures["R-a"]) == {RUN_2}
    assert verdict.report()["failed"] == ["R-a"]


async def test_t585_fails_r_b_when_a_finished_activity_ran_again() -> None:
    verdict = await run(RecordedLogs([[event(RUN_1), event(RUN_2)]], repeated=2))
    assert verdict.failures == {"R-b": {"repeatedActivities": 2}}


async def test_t585_fails_r_c_when_nothing_started_after_the_restart() -> None:
    verdict = await run(RecordedLogs([[event(RUN_1), event(RUN_2)]], after=0))
    assert verdict.failures == {"R-c": {"activitiesAfterRestart": 0}}


async def test_t585_fails_with_resume_timeout_when_a_run_never_finishes() -> None:
    logs = RecordedLogs([[event(RUN_1)]])
    verdict = await run(logs)
    assert verdict.timed_out
    assert not verdict.passed
    assert verdict.report()["code"] == "resume_timeout"
    assert verdict.failures == {"unfinishedRuns": [RUN_2]}
    assert len(logs.waits) == 60
    assert all(kql == finished_query(RUNS) for kql in logs.queries)


def test_the_queries_follow_section_6_7() -> None:
    repeated = repeated_query(RUNS, RESTARTED)
    assert f'let runs = dynamic(["{RUN_1}", "{RUN_2}"]);' in repeated
    assert "let tr = datetime(2026-10-30T02:41:07.000000Z);" in repeated
    assert 'AppRoleName == "docchat-ingestion" and Name == "docchat.ingestion.activity"' in repeated
    assert "| join kind=inner (a | where st > tr) on item, activity" in repeated
    assert after_restart_query(RUNS, RESTARTED).endswith("a | where st > tr | count")
    assert 'Name == "ingestion_run_finished"' in finished_query(RUNS)


def test_arguments_take_comma_separated_run_ids() -> None:
    assert parse_run_ids(f"{RUN_1.upper()}, {RUN_2}") == RUNS
    for value in (RUN_1, f"{RUN_1},{RUN_1}", f"{RUN_1},{RUN_2},{RUN_1}", "a,b"):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            parse_run_ids(value)
    assert parse_restarted_at("2026-10-30T02:41:07Z") == RESTARTED
    assert parse_restarted_at("2026-10-30T02:41:07") == RESTARTED


def test_main_exits_0_or_1_with_a_report(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--workspace-id",
        WORKSPACE,
        "--run-ids",
        f"{RUN_1},{RUN_2}",
        "--restarted-at",
        "2026-10-30T02:41:07Z",
    ]
    passing = RecordedLogs([[event(RUN_1), event(RUN_2)]])
    assert main(argv, run_query=passing.run_query) == 0
    assert json.loads(capsys.readouterr().out) == {"passed": ["R-a", "R-b", "R-c"]}
    failing = RecordedLogs([[event(RUN_1), event(RUN_2)]], repeated=1, after=0)
    assert main(argv, run_query=failing.run_query) == 1
    report = json.loads(capsys.readouterr().err)
    assert report["failed"] == ["R-b", "R-c"]
