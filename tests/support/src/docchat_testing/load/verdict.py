"""`docchat-load verdict` (`84-testing.md` sections 7.5 step 9 and 7.6; T-584).

Every criterion of a scenario is computed here, from the sources section 7.6 names: k6's
thresholds in the `K6_SUMMARY_JSON` marker, the Log Analytics query API, platform metrics, the
Activity Log and, for S2-e, the API itself. The verdict fails when the log holds
`K6_BOOTSTRAP_FAILED` or no `K6_EXIT` line, records k6's exit code (0 every threshold passed, 99
a threshold failed, any other code a k6 error) and `limit_retries`, first waits until telemetry of
the window's last minute has arrived (every 30 s, at most 10 minutes), writes `verdict.json` and
the criteria table to the job summary, and exits 1 on any failed criterion, 0 when every one held
or when S3 came out `inconclusive` (`calibration_miss` or `dependency_bound`).

Common criteria: `C-db` (S1 to S3) fails when a chat-path Cosmos DB request stayed throttled after
the SDK's retries, and names the cause `dependency_capacity` when `CDBDataPlaneRequests` recorded
429s; `C-cap` (every verdict) fails when the workspace reached a daily cap since the run started.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load import queries
from docchat_testing.load.azcli import Azure, MetricPoint
from docchat_testing.load.chat import send_turn
from docchat_testing.load.common import (
    LOAD_USERS,
    UsageError,
    append_summary,
    iso,
    read_json,
    utc_now,
    write_json,
)
from docchat_testing.load.markers import THRESHOLD_FAILED_EXIT, K6Log
from docchat_testing.logquery import Rows
from docchat_testing.tokens import REPOSITORY_ROOT

type QueryRunner = Callable[[str, str], Awaitable[Rows]]
"""Runs one KQL query over an ISO 8601 interval and returns its rows."""
type Sleep = Callable[[float], Awaitable[None]]

PROD_ENV_FILE: Final = REPOSITORY_ROOT / "infra" / "env" / "prod.json"
RU_PER_TURN_LIMIT: Final = 400.0
CONCURRENCY_FLOOR: Final = 95
TELEMETRY_POLL_SECONDS: Final = 30.0
TELEMETRY_LIMIT: Final = timedelta(minutes=10)
EXTRACTION_WAIT: Final = timedelta(minutes=10)
SCALE_UP_EARLIEST: Final = timedelta(minutes=5)
SCALE_UP_LATEST: Final = timedelta(minutes=12)
ACTIVITY_WINDOW: Final = timedelta(minutes=20)
CPU_THRESHOLD: Final = 70.0
S3_START_INSTANCES: Final = 1
S2_FAILURE_GRACE: Final = timedelta(seconds=5)
S2_LATE_FAILURE: Final = timedelta(seconds=60)
S2_SESSION_CHECK: Final = timedelta(seconds=210)
S2_SPREAD_SHARE: Final = 0.3
S2_END_CODES: Final = frozenset({"service_restarting", "turn_abandoned"})
S4_RUN_DOCUMENTS: Final = 100
ANSWER_TOKEN_STEPS: Final = (80, 320, 1280, 4000)
HTTP_OK: Final = 200


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class Verdict:
    """One scenario's criteria, measurements and outcome."""

    scenario: str
    start: datetime | None = None
    end: datetime | None = None
    criteria: list[Criterion] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    k6_exit: int | None = None
    limit_retries: int | None = None
    inconclusive: bool = False
    cause: str | None = None

    def add(self, criterion_id: str, passed: bool, detail: str = "") -> None:
        self.criteria.append(Criterion(criterion_id, passed, detail))

    @property
    def failed(self) -> list[Criterion]:
        return [criterion for criterion in self.criteria if not criterion.passed]

    @property
    def status(self) -> str:
        if self.inconclusive:
            return "inconclusive"
        return "passed" if self.criteria and not self.failed else "failed"

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "inconclusive": self.inconclusive,
            "cause": self.cause,
            "window": {
                "start": iso(self.start) if self.start else None,
                "end": iso(self.end) if self.end else None,
            },
            "k6Exit": self.k6_exit,
            "limitRetries": self.limit_retries,
            "criteria": [
                {"id": item.id, "passed": item.passed, "detail": item.detail}
                for item in self.criteria
            ],
            "measurements": self.measurements,
        }

    def markdown(self) -> str:
        lines = [
            f"## Load verdict: {self.scenario} {self.status}",
            "",
            f"k6 exit: {self.k6_exit if self.k6_exit is not None else '-'}; "
            f"limit_retries: {self.limit_retries if self.limit_retries is not None else '-'}"
            + (f"; cause: {self.cause}" if self.cause else ""),
            "",
            "| Criterion | Result | Detail |",
            "|---|---|---|",
        ]
        lines += [
            f"| {item.id} | {'passed' if item.passed else 'failed'} | "
            f"{item.detail.replace('|', '/')} |"
            for item in self.criteria
        ]
        return "\n".join(lines) + "\n"


@dataclass(slots=True)
class Sources:
    """Where the criteria come from; tests replace each one."""

    query: QueryRunner
    azure: Azure
    sleep: Sleep = asyncio.sleep
    now: Callable[[], datetime] = utc_now

    async def rows(self, kql: str, since: datetime) -> Rows:
        return await self.query(kql, f"{iso(since)}/{iso(self.now())}")

    async def scalar(self, kql: str, since: datetime, column: str) -> float | None:
        found = await self.rows(kql, since)
        value = found[0].get(column) if found else None
        return float(value) if isinstance(value, int | float) else None

    async def sleep_until(self, moment: datetime) -> None:
        remaining = (moment - self.now()).total_seconds()
        if remaining > 0:
            await self.sleep(remaining)


# --- Shared criteria ---------------------------------------------------------------------------


def k6_run(verdict: Verdict, log: K6Log) -> None:
    """The bootstrap and k6's exit: `K6_BOOTSTRAP_FAILED` or no `K6_EXIT` fails the scenario."""
    verdict.k6_exit = log.exit_code
    verdict.limit_retries = log.limit_retries
    if log.bootstrap_failure is not None:
        verdict.add("k6", False, f"K6_BOOTSTRAP_FAILED {log.bootstrap_failure}")
    elif log.exit_code is None:
        verdict.add("k6", False, "the log holds no K6_EXIT line")
    elif log.exit_code not in {0, THRESHOLD_FAILED_EXIT}:
        verdict.add("k6", False, f"k6 error: exit {log.exit_code}")
    else:
        verdict.add("k6", True, f"exit {log.exit_code}")


def thresholds(
    verdict: Verdict, criterion_id: str, log: K6Log, pairs: Sequence[tuple[str, str]]
) -> None:
    """A criterion that is one or more k6 thresholds, each of which must hold."""
    problems: list[str] = []
    for metric, expression in pairs:
        outcome = log.threshold(metric, expression)
        if outcome is None:
            problems.append(f"{metric} {expression} isn't in the summary")
        elif not outcome:
            problems.append(f"{metric} {expression} crossed")
    detail = "; ".join(problems) or ", ".join(f"{metric} {expr}" for metric, expr in pairs)
    verdict.add(criterion_id, not problems, detail)


async def wait_for_telemetry(verdict: Verdict, sources: Sources, end: datetime) -> None:
    """Poll every 30 s, at most 10 minutes, until rows of the window's last minute arrive."""
    deadline = sources.now() + TELEMETRY_LIMIT
    while True:
        count = await sources.scalar(
            queries.telemetry_arrived(end), end - timedelta(minutes=2), "Count"
        )
        if count:
            verdict.add("telemetry", True, "the window's last minute arrived")
            return
        if sources.now() >= deadline:
            verdict.add(
                "telemetry", False, "no telemetry of the window's last minute within 10 min"
            )
            return
        await sources.sleep(TELEMETRY_POLL_SECONDS)


async def c_db(verdict: Verdict, sources: Sources, start: datetime, end: datetime) -> None:
    n = await sources.scalar(queries.c_db(start, end), start, "n") or 0.0
    absorbed = {
        str(row["CollectionName"]): int(row.get("throttled") or 0)
        for row in await sources.rows(queries.c_db_throttled(start, end), start)
    }
    verdict.measurements["cosmosThrottledAbsorbed"] = absorbed
    if n == 0:
        verdict.add("C-db", True, "no chat-path request stayed throttled")
        return
    detail = f"{n:g} chat-path failures of a throttled dependency"
    if sum(absorbed.values()) > 0:
        verdict.cause = "dependency_capacity"
        detail += "; cause dependency_capacity (CDBDataPlaneRequests recorded 429s)"
    verdict.add("C-db", False, detail)


async def c_cap(verdict: Verdict, sources: Sources, run_start: datetime) -> None:
    rows = await sources.rows(queries.c_cap(run_start), run_start)
    count = rows[0].get("Count") if rows else 0
    passed = not count
    verdict.add("C-cap", passed, "no daily cap reached" if passed else f"{count} OverQuota rows")


# --- S1 ----------------------------------------------------------------------------------------


def prod_maxima(path: Path = PROD_ENV_FILE) -> dict[str, float]:
    """`cosmos.maxThroughput` of `infra/env/prod.json` at the checked-out (release) commit."""
    config = read_json(path, "infra/env/prod.json")
    maxima = config.get("cosmos", {}).get("maxThroughput") if isinstance(config, dict) else None
    if not isinstance(maxima, dict):
        raise UsageError("prod_config_invalid", f"{path} has no cosmos.maxThroughput")
    return {str(name): float(value) for name, value in maxima.items()}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def request_unit_criterion(
    verdict: Verdict, rows: Rows, maxima: Mapping[str, float], criterion_id: str = "S1-h"
) -> None:
    """S1-h: every container at most 400 RU per completed turn, and its peak 1-minute
    consumption per second within its `prod` autoscale maximum."""
    containers: dict[str, dict[str, float | None]] = {
        str(row["CollectionName"]): {
            "ruPerTurn": _number(row.get("ruPerTurn")),
            "peakRuPerSecond": _number(row.get("peakRuPerSecond")),
            "throttled": _number(row.get("throttled")),
        }
        for row in rows
    }
    verdict.measurements["requestUnits"] = containers
    if not containers:
        verdict.add(criterion_id, False, "CDBDataPlaneRequests recorded no request charge")
        return
    problems: list[str] = []
    for name, values in sorted(containers.items()):
        per_turn, peak = values["ruPerTurn"], values["peakRuPerSecond"]
        if per_turn is None or per_turn > RU_PER_TURN_LIMIT:
            problems.append(f"{name}: {per_turn} RU per completed turn (limit 400)")
        limit = maxima.get(name)
        if limit is not None and (peak is None or peak > limit):
            problems.append(f"{name}: peak {peak} RU/s above prod's maximum {limit:g}")
    verdict.add(
        criterion_id, not problems, "; ".join(problems) or "within 400 RU and prod's maxima"
    )


async def evaluate_s1(
    log: K6Log,
    *,
    start: datetime,
    end: datetime,
    run_start: datetime,
    sources: Sources,
    maxima: Mapping[str, float],
) -> Verdict:
    verdict = Verdict("s1", start, end)
    k6_run(verdict, log)
    thresholds(verdict, "S1-a", log, [("http_req_failed{scenario:streams}", "rate==0")])
    thresholds(verdict, "S1-b", log, [("checks{scenario:streams}", "rate==1")])
    thresholds(verdict, "S1-c", log, [("http_req_duration{scenario:cheap}", "p(95)<=200")])
    thresholds(verdict, "S1-d", log, [("http_req_failed{scenario:cheap}", "rate==0")])
    thresholds(
        verdict,
        "S1-j",
        log,
        [
            ("http_req_duration{scenario:sessions}", "p(95)<=200"),
            ("http_req_failed{scenario:sessions}", "rate==0"),
        ],
    )
    verdict.measurements["cheapP95Ms"] = log.value("http_req_duration{scenario:cheap}", "p(95)")
    verdict.measurements["sessionsP95Ms"] = log.value(
        "http_req_duration{scenario:sessions}", "p(95)"
    )
    await wait_for_telemetry(verdict, sources, end)

    peak = await sources.scalar(queries.s1_concurrency(start, end), start, "minPeak")
    verdict.measurements["minConcurrentTurns"] = peak
    verdict.add(
        "S1-e",
        peak is not None and peak >= CONCURRENCY_FLOOR,
        f"smallest per-minute peak {peak} (at least 95)",
    )
    errors = await sources.scalar(queries.s1_server_errors(start, end), start, "n") or 0.0
    verdict.add("S1-f", errors == 0, f"{errors:g} 5xx answers")
    instances = await sources.scalar(queries.s1_instances(start, end), start, "instances")
    verdict.add("S1-g", instances == 1, f"{instances} instances served the run")
    request_unit_criterion(
        verdict, await sources.rows(queries.request_units(start, end), start), maxima
    )
    charges = await sources.rows(queries.s1_turn_charges(start, end), start)
    p95 = _number(charges[0].get("p95")) if charges else None
    verdict.measurements["turnChargeP95"] = p95
    verdict.add(
        "S1-i",
        p95 is not None and p95 <= RU_PER_TURN_LIMIT,
        f"p95 {p95} RU per completed turn (limit 400)",
    )
    await c_db(verdict, sources, start, end)
    # Extractions started in the last 5 minutes must have arrived before S1-k counts them.
    await sources.sleep_until(end + EXTRACTION_WAIT)
    missing = await sources.scalar(queries.s1_missing_extractions(start, end), start, "Count")
    verdict.add(
        "S1-k",
        missing == 0,
        f"{missing if missing is not None else 'unknown'} completed turns without an extraction",
    )
    await c_cap(verdict, sources, run_start)
    return verdict


# --- S2 ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionApi:
    """The API as the load users, for S2-e."""

    api_base_url: str
    tokens: Mapping[str, str]
    questions: Sequence[str]
    project_id: str | None = None
    transport: Any = None

    def client(self, alias: str) -> DeployedApiClient:
        token = self.tokens[alias]

        async def bearer() -> str:
            return token

        return DeployedApiClient(self.api_base_url, bearer, transport=self.transport)


async def _owned_session(api: SessionApi, session_id: str) -> tuple[str, dict[str, Any]] | None:
    """The alias that owns the session and the session's detail."""
    for alias in LOAD_USERS:
        if alias not in api.tokens:
            continue
        async with api.client(alias) as client:
            response = await client.request("GET", f"/v1/sessions/{session_id}")
        if response.status_code == HTTP_OK:
            detail: dict[str, Any] = response.json()
            return alias, detail
    return None


async def check_failed_session(api: SessionApi, session_id: str, question: str) -> str | None:
    """S2-e for one session: no turn `streaming`, every failed turn ended by the drain or
    abandoned, and a new turn in the session ends with `done`; the problem, or `None`."""
    owned = await _owned_session(api, session_id)
    if owned is None:
        return f"session {session_id}: getSession found it for no load user"
    alias, detail = owned
    turns = detail.get("turns", [])
    if any(turn.get("status") == "streaming" for turn in turns):
        return f"session {session_id}: a turn is still streaming"
    wrong = [
        str(turn.get("errorCode"))
        for turn in turns
        if turn.get("status") == "failed" and turn.get("errorCode") not in S2_END_CODES
    ]
    if wrong:
        return f"session {session_id}: failed turns with {', '.join(wrong)}"
    async with api.client(alias) as client:
        project_id = api.project_id or str(detail["projectId"])
        result = await send_turn(
            client, project_id=project_id, question=question, session_id=session_id
        )
    if not result.done:
        return f"session {session_id}: the new turn didn't end with done ({result.reason})"
    return None


async def evaluate_s2(
    log: K6Log,
    *,
    start: datetime,
    end: datetime,
    removed_at: datetime,
    run_start: datetime,
    sources: Sources,
    api: SessionApi,
) -> Verdict:
    verdict = Verdict("s2", start, end)
    verdict.measurements["removedAt"] = iso(removed_at)
    k6_run(verdict, log)
    await wait_for_telemetry(verdict, sources, end)

    removed_rows = await sources.rows(queries.s2_removed_instance(start, end, removed_at), start)
    candidates = [str(row["AppRoleInstance"]) for row in removed_rows]
    removed = candidates[0] if len(candidates) == 1 else None
    verdict.measurements["removedInstance"] = removed
    verdict.add(
        "S2-removed",
        removed is not None,
        f"removed instance {removed}" if removed else f"{len(candidates)} candidate instances",
    )
    served: dict[str, str] = {}
    for row in await sources.rows(queries.s2_requests(start, end), start):
        served.setdefault(str(row["OperationId"]), str(row["AppRoleInstance"]))

    failures = log.failed_requests
    failed_traces = {failure.trace_id for failure in failures}
    verdict.measurements["failedRequests"] = len(failures)

    unexplained = [
        failure.trace_id
        for failure in failures
        if not (
            (failure.trace_id in served and served[failure.trace_id] == removed)
            or (
                failure.trace_id not in served
                and failure.started_at < removed_at + S2_FAILURE_GRACE
            )
        )
    ]
    verdict.add(
        "S2-a",
        not unexplained,
        f"{len(unexplained)} failed requests not explained by the removed instance",
    )

    surviving = [
        failure.trace_id
        for failure in failures
        if failure.trace_id in served and served[failure.trace_id] != removed
    ]
    server_failures = (
        await sources.scalar(
            queries.s2_surviving_failures(start, end, removed or ""), start, "Count"
        )
        if removed
        else None
    )
    verdict.add(
        "S2-b",
        server_failures == 0 and not surviving,
        f"{server_failures if server_failures is not None else 'unknown'} server-side failures "
        f"and {len(surviving)} client-side failures on surviving instances",
    )

    late = [failure for failure in failures if failure.started_at >= removed_at + S2_LATE_FAILURE]
    verdict.add("S2-c", not late, f"{len(late)} failed requests started at or after T_r + 60 s")

    multi = [session for session in log.sessions if len(session.trace_ids) >= 2]
    spread = [
        session
        for session in multi
        if len({served[trace] for trace in session.trace_ids if trace in served}) >= 2
        and not failed_traces.intersection(session.trace_ids)
    ]
    share = len(spread) / len(multi) if multi else 0.0
    verdict.measurements["sessionSpreadShare"] = share
    verdict.add(
        "S2-d",
        share >= S2_SPREAD_SHARE,
        f"{len(spread)} of {len(multi)} sessions of two or more turns spread over instances",
    )

    held = sorted(
        {
            session.session_id
            for session in log.sessions
            if session.session_id is not None and failed_traces.intersection(session.trace_ids)
        }
    )
    problems: list[str] = []
    if held:
        await sources.sleep_until(removed_at + S2_SESSION_CHECK)
        questions = list(api.questions) or ["What does the water report cover?"]
        for index, session_id in enumerate(held):
            problem = await check_failed_session(api, session_id, questions[index % len(questions)])
            if problem is not None:
                problems.append(problem)
    verdict.add(
        "S2-e",
        not problems,
        "; ".join(problems) or f"{len(held)} sessions with a failed stream recovered",
    )
    await c_db(verdict, sources, start, end)
    await c_cap(verdict, sources, run_start)
    return verdict


# --- S3 ----------------------------------------------------------------------------------------


def five_minutes_before(points: Iterable[MetricPoint], moment: datetime) -> list[float]:
    """The five 1-minute averages whose minutes end at or before `moment`, latest last."""
    complete = [
        point.value
        for point in points
        if point.value is not None and point.time + timedelta(minutes=1) <= moment
    ]
    return complete[-5:]


def next_answer_tokens(current: int) -> int | None:
    larger = [step for step in ANSWER_TOKEN_STEPS if step > current]
    return larger[0] if larger else None


async def evaluate_s3(
    log: K6Log,
    *,
    start: datetime,
    end: datetime,
    run_start: datetime,
    sources: Sources,
    rate: Mapping[str, Any],
    app_name: str,
) -> Verdict:
    verdict = Verdict("s3", start, end)
    verdict.measurements["rate"] = dict(rate)
    # The count at the end, read before the waits below give autoscale time to scale in.
    instances = await sources.azure.instance_count(app_name)
    plan_id = await sources.azure.plan_id(app_name)
    k6_run(verdict, log)
    thresholds(verdict, "S3-d", log, [("http_req_duration{scenario:cheap}", "p(95)<=200")])
    thresholds(
        verdict,
        "S3-e",
        log,
        [
            ("http_req_failed{scenario:cheap}", "rate==0"),
            ("http_req_failed{scenario:turns}", "rate==0"),
        ],
    )
    verdict.measurements["cheapP95Ms"] = log.value("http_req_duration{scenario:cheap}", "p(95)")
    await wait_for_telemetry(verdict, sources, end)

    scale_ups = await sources.azure.scale_ups(start, start + ACTIVITY_WINDOW)
    first = scale_ups[0] if scale_ups else None
    verdict.measurements["scaleUpAt"] = iso(first) if first else None
    verdict.add(
        "S3-a",
        first is not None and start + SCALE_UP_EARLIEST <= first <= start + SCALE_UP_LATEST,
        f"first scale-up at {iso(first) if first else 'none'} (main minutes 5 to 12)",
    )
    points = await sources.azure.cpu_minutes(plan_id, start - timedelta(minutes=5), end)
    before = five_minutes_before(points, first) if first else []
    mean = sum(before) / len(before) if len(before) == 5 else None
    verdict.measurements["cpuBeforeScaleUp"] = before
    verdict.add(
        "S3-b",
        mean is not None and mean > CPU_THRESHOLD,
        f"mean CPU {mean:.1f}% over the five minutes before the scale-up"
        if mean is not None
        else "no five complete CPU minutes before a scale-up",
    )
    verdict.measurements["instancesAtEnd"] = instances
    verdict.add(
        "S3-c",
        instances >= S3_START_INSTANCES + 1,
        f"{instances} instances at the end (at least {S3_START_INSTANCES + 1})",
    )
    await c_db(verdict, sources, start, end)
    await c_cap(verdict, sources, run_start)

    main_minutes = [
        point.value
        for point in points
        if point.value is not None
        and start <= point.time
        and point.time + timedelta(minutes=1) <= end
    ]
    verdict.measurements["cpuMainPhase"] = main_minutes
    failed = {criterion.id for criterion in verdict.failed}
    if (
        failed
        and failed <= {"S3-a", "S3-b"}
        and all(value < CPU_THRESHOLD for value in main_minutes)
    ):
        r = int(rate["rate"])
        verdict.inconclusive = True
        if r == int(rate["bounds"]["cpu"]):
            verdict.cause = "calibration_miss"
            verdict.measurements["nextRate"] = math.ceil(1.25 * r)
        else:
            verdict.cause = "dependency_bound"
            verdict.measurements["nextAnswerTokens"] = next_answer_tokens(int(rate["answerTokens"]))
    return verdict


# --- S4 ----------------------------------------------------------------------------------------


def _moment(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def set_duration(results: Mapping[str, Any]) -> timedelta | None:
    """The latest `completedAt` minus the earliest `createdAt` of the set's runs."""
    runs = results.get("runs", [])
    if not runs or any(not run.get("completedAt") for run in runs):
        return None
    return max(_moment(run["completedAt"]) for run in runs) - min(
        _moment(run["createdAt"]) for run in runs
    )


async def evaluate_s4(
    set_number: int,
    *,
    results: Mapping[str, Any],
    set1_results: Mapping[str, Any] | None,
    run_start: datetime,
    sources: Sources,
) -> Verdict:
    set_start, set_end = _moment(results["setStart"]), _moment(results["setEnd"])
    verdict = Verdict(f"s4-set{set_number}", set_start, set_end)
    runs = list(results.get("runs", []))
    verdict.measurements["runs"] = runs
    bad = [
        str(run.get("runId"))
        for run in runs
        if run.get("state") != "completed"
        or run.get("counts", {}).get("succeeded") != S4_RUN_DOCUMENTS
        or run.get("counts", {}).get("failed") != 0
    ]
    verdict.add(
        "S4-a",
        len(runs) == 2 and not bad,
        f"{len(runs)} runs; not completed with 100 succeeded and 0 failed: "
        f"{', '.join(bad) or 'none'}",
    )
    duration = set_duration(results)
    verdict.measurements["durationSeconds"] = duration.total_seconds() if duration else None
    if set_number == 2:
        first = set_duration(set1_results) if set1_results is not None else None
        verdict.measurements["set1DurationSeconds"] = first.total_seconds() if first else None
        verdict.add(
            "S4-b",
            duration is not None and first is not None and duration <= first / 2,
            f"set 2 took {duration}, set 1 {first} (at most half)",
        )
    await wait_for_telemetry(verdict, sources, set_end)
    throttled = await sources.scalar(queries.s4_throttled(set_start, set_end), set_start, "n")
    verdict.measurements["throttledCalls"] = throttled
    verdict.add(
        "S4-c",
        throttled is not None and throttled > 0,
        f"{throttled} throttled ingestion model calls retried",
    )
    await c_cap(verdict, sources, run_start)
    return verdict


# --- Output ------------------------------------------------------------------------------------


def finish(verdict: Verdict, out: Path, environ: Mapping[str, str] | None = None) -> int:
    """Write `verdict.json` and the summary table; the command's exit code."""
    write_json(out, verdict.to_json())
    append_summary(verdict.markdown(), environ)
    return 1 if verdict.status == "failed" else 0
