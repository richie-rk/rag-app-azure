"""`python -m docchat_testing.resume_check`: the `dev` half of the deployed crash-resume test
(`84-testing.md` section 6.7; T-587, T-585).

The `resume-restart` job of `deployed-tests.yaml` restarts the ingestion app while run 1's large
item is past Embed and run 2 waits behind it, then runs

    python -m docchat_testing.resume_check --workspace-id <id> --run-ids <id,id> \
        --restarted-at <UTC time>

as the `dev` deploy identity. Every 60 s, for at most 60 minutes, it asks the Log Analytics query
API whether both runs have an `ingestion_run_finished` event (`resume_timeout` otherwise), then
evaluates:

- R-a: both events have `state` `completed` and `itemsSucceeded` equal to `itemsTotal`;
- R-b: no activity that completed successfully before the restart started again after it (the
  query of section 6.7 returns 0);
- R-c: at least one activity of the runs started after the restart, so the restart interrupted
  work still to do.

It exits 0 when all three hold and 1 otherwise, printing one JSON object on standard error that
names each failed criterion with its reading.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from docchat_testing.logquery import Rows, kql_datetime, query_logs

POLL_SECONDS: Final = 60.0
POLL_LIMIT: Final = timedelta(minutes=60)
TIMESPAN: Final = "P1D"
"""Every query reads the last day (`ago(1d)` in section 6.7's query)."""
TIMEOUT_CODE: Final = "resume_timeout"
EXPECTED_RUNS: Final = 2
EXIT_PASSED: Final = 0
EXIT_FAILED: Final = 1

type QueryRunner = Callable[[str, str, str], Awaitable[Rows]]


def _run_list(run_ids: Sequence[str]) -> str:
    return ", ".join(f'"{run_id}"' for run_id in run_ids)


def finished_query(run_ids: Sequence[str]) -> str:
    """Each run's `ingestion_run_finished` event: its state and item counts."""
    return f"""let runs = dynamic([{_run_list(run_ids)}]);
AppEvents
| where TimeGenerated > ago(1d) and Name == "ingestion_run_finished"
| where tostring(Properties["runId"]) in (runs)
| project runId = tostring(Properties["runId"]), state = tostring(Properties["state"]),
          itemsTotal = tolong(Properties["itemsTotal"]),
          itemsSucceeded = tolong(Properties["itemsSucceeded"])"""


def _activities(run_ids: Sequence[str], restarted_at: datetime) -> str:
    return f"""let runs = dynamic([{_run_list(run_ids)}]); let tr = {kql_datetime(restarted_at)};
let a = AppDependencies
  | where TimeGenerated > ago(1d) and AppRoleName == "docchat-ingestion" and Name == "docchat.ingestion.activity"
  | where tostring(Properties["docchat.run_id"]) in (runs)
  | project item = tostring(Properties["docchat.item_id"]), activity = tostring(Properties["docchat.activity_name"]),
            st = TimeGenerated, en = TimeGenerated + DurationMs * 1ms, Success;"""  # noqa: E501 - section 6.7's text


def repeated_query(run_ids: Sequence[str], restarted_at: datetime) -> str:
    """Section 6.7's query: activities that completed before the restart and ran again."""
    return f"""{_activities(run_ids, restarted_at)}
a | where Success == true and en < tr
  | join kind=inner (a | where st > tr) on item, activity
  | count"""


def after_restart_query(run_ids: Sequence[str], restarted_at: datetime) -> str:
    """R-c: the activities that started after the restart."""
    return f"""{_activities(run_ids, restarted_at)}
a | where st > tr | count"""


def _count(rows: Rows) -> int:
    if len(rows) != 1 or "Count" not in rows[0]:
        raise ValueError("a count query must answer one row with a Count column")
    return int(rows[0]["Count"])


@dataclass(slots=True)
class Verdict:
    """The outcome: the failed criteria, each with what was read."""

    failures: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return not self.failures and not self.timed_out

    def report(self) -> dict[str, Any]:
        if self.timed_out:
            return {
                "code": TIMEOUT_CODE,
                "message": "Not every run finished within 60 minutes of the restart.",
                "detail": self.failures,
            }
        return {"failed": sorted(self.failures), "detail": self.failures}


def evaluate_finished(run_ids: Sequence[str], rows: Rows) -> dict[str, Any]:
    """R-a over the finished events; an empty result when it holds."""
    events = {str(row["runId"]): row for row in rows}
    wrong = {
        run_id: {
            "state": events[run_id]["state"],
            "itemsTotal": events[run_id]["itemsTotal"],
            "itemsSucceeded": events[run_id]["itemsSucceeded"],
        }
        for run_id in run_ids
        if events[run_id]["state"] != "completed"
        or events[run_id]["itemsSucceeded"] != events[run_id]["itemsTotal"]
    }
    return {"R-a": wrong} if wrong else {}


def _finished_runs(run_ids: Sequence[str], rows: Rows) -> Rows:
    wanted = set(run_ids)
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if str(row.get("runId")) in wanted:
            latest[str(row["runId"])] = row
    return [dict(row) for row in latest.values()]


async def check_resume(
    workspace_id: str,
    run_ids: Sequence[str],
    restarted_at: datetime,
    *,
    run_query: QueryRunner | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Verdict:
    """Wait for both runs' finished events, then evaluate R-a, R-b and R-c."""

    async def default_query(workspace: str, kql: str, timespan: str) -> Rows:
        return await query_logs(workspace, kql, timespan)

    query = default_query if run_query is None else run_query
    started = monotonic()
    while True:
        finished = _finished_runs(
            run_ids, await query(workspace_id, finished_query(run_ids), TIMESPAN)
        )
        if len(finished) == len(run_ids):
            break
        if monotonic() - started >= POLL_LIMIT.total_seconds():
            missing = sorted(set(run_ids) - {str(row["runId"]) for row in finished})
            return Verdict(failures={"unfinishedRuns": missing}, timed_out=True)
        await sleep(POLL_SECONDS)
    verdict = Verdict(failures=evaluate_finished(run_ids, finished))
    repeated = _count(await query(workspace_id, repeated_query(run_ids, restarted_at), TIMESPAN))
    if repeated != 0:
        verdict.failures["R-b"] = {"repeatedActivities": repeated}
    after = _count(await query(workspace_id, after_restart_query(run_ids, restarted_at), TIMESPAN))
    if after < 1:
        verdict.failures["R-c"] = {"activitiesAfterRestart": after}
    return verdict


def parse_run_ids(value: str) -> list[str]:
    """`--run-ids`: the two run IDs, comma-separated, as canonical lower-case UUIDs."""
    run_ids = [str(UUID(part.strip())) for part in value.split(",") if part.strip()]
    if len(run_ids) != EXPECTED_RUNS or len(set(run_ids)) != EXPECTED_RUNS:
        raise argparse.ArgumentTypeError("--run-ids takes the two run IDs, comma-separated")
    return run_ids


def parse_restarted_at(value: str) -> datetime:
    """`--restarted-at`: an ISO 8601 time; one without an offset is read as UTC."""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m docchat_testing.resume_check")
    parser.add_argument("--workspace-id", required=True, type=lambda value: str(UUID(value)))
    parser.add_argument("--run-ids", required=True, type=parse_run_ids)
    parser.add_argument("--restarted-at", required=True, type=parse_restarted_at)
    return parser


def main(argv: Sequence[str] | None = None, *, run_query: QueryRunner | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    verdict = asyncio.run(
        check_resume(
            arguments.workspace_id,
            arguments.run_ids,
            arguments.restarted_at,
            run_query=run_query,
        )
    )
    if not verdict.passed:
        sys.stderr.write(json.dumps(verdict.report()) + "\n")
        return EXIT_FAILED
    sys.stdout.write(json.dumps({"passed": ["R-a", "R-b", "R-c"]}) + "\n")
    return EXIT_PASSED


if __name__ == "__main__":
    sys.exit(main())
