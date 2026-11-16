"""The load verdicts on recorded query, metric and Activity Log answers (T-584, T-579)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

import pytest

from docchat_testing.clock import FakeClock
from docchat_testing.load.azcli import Azure, MetricPoint
from docchat_testing.load.markers import read_log
from docchat_testing.load.verdict import (
    PROD_ENV_FILE,
    SessionApi,
    Sources,
    Verdict,
    evaluate_s1,
    evaluate_s2,
    evaluate_s3,
    evaluate_s4,
    finish,
    five_minutes_before,
    next_answer_tokens,
    prod_maxima,
    set_duration,
)
from docchat_testing.logquery import Rows

START = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=10)
RUN_START = START - timedelta(hours=2)
TRACE = "0af7651916cd43dd8448eb211c80319c"
MAXIMA = {"conversations": 4000.0, "memories": 1000.0, "usage": 1000.0}

S1_THRESHOLDS = {
    "http_req_failed{scenario:streams}": {"rate==0": True},
    "checks{scenario:streams}": {"rate==1": True},
    "http_req_duration{scenario:cheap}": {"p(95)<=200": True},
    "http_req_failed{scenario:cheap}": {"rate==0": True},
    "http_req_duration{scenario:sessions}": {"p(95)<=200": True},
    "http_req_failed{scenario:sessions}": {"rate==0": True},
}


def k6_log(thresholds: dict[str, dict[str, bool]], *lines: str, exit_code: int = 0) -> str:
    summary = {"thresholds": thresholds, "values": {}, "limitRetries": 2}
    return "\n".join([*lines, f"K6_SUMMARY_JSON {json.dumps(summary)}", f"K6_EXIT {exit_code}"])


class Queries:
    """Answers each query by the first marker its text contains, and records what ran."""

    def __init__(self, answers: Sequence[tuple[str, Rows]]) -> None:
        self.answers = list(answers)
        self.ran: list[tuple[str, str]] = []

    def answer(self, marker: str, rows: Rows) -> None:
        self.answers.insert(0, (marker, rows))

    async def __call__(self, kql: str, timespan: str) -> Rows:
        self.ran.append((kql, timespan))
        for marker, rows in self.answers:
            if marker in kql:
                return rows
        return []


def s1_answers() -> list[tuple[str, Rows]]:
    return [
        ("union AppRequests, AppDependencies, AppMetrics", [{"Count": 120}]),
        ("row_cumsum", [{"minPeak": 98}]),
        ("toint(ResultCode) >= 500 | summarize n", [{"n": 0}]),
        ("dcount(AppRoleInstance)", [{"instances": 1}]),
        (
            "ruPerTurn",
            [
                {"CollectionName": "conversations", "ruPerTurn": 250.0, "peakRuPerSecond": 2100.5},
                {"CollectionName": "memories", "ruPerTurn": 40.0, "peakRuPerSecond": 300.0},
                {"CollectionName": "usage", "ruPerTurn": 60.0, "peakRuPerSecond": 450.0},
            ],
        ),
        ("percentile(ru, 95)", [{"p95": 330.0, "turns": 5000}]),
        ("docchat.memory.extract", [{"Count": 0}]),
        ("docchat.api.problems", [{"n": 0}]),
        ("countif(StatusCode == 429) by CollectionName", []),
        ("_LogOperation", [{"Count": 0}]),
    ]


class FakeAzure(Azure):
    def __init__(
        self,
        *,
        instances: int = 2,
        scale_ups: Sequence[datetime] = (),
        cpu: Sequence[MetricPoint] = (),
        maxima: dict[str, int] | None = None,
    ) -> None:
        super().__init__(self._refuse)
        self.instances = instances
        self.ups = list(scale_ups)
        self.cpu = list(cpu)
        self.maxima = maxima or {}

    @staticmethod
    async def _refuse(arguments: Sequence[str]) -> str:
        raise AssertionError(f"unexpected az {arguments}")

    @override
    async def plan_id(self, app_name: str) -> str:
        return (
            "/subscriptions/s/resourceGroups/rg-docchat-load/providers/Microsoft.Web/serverfarms/a"
        )

    @override
    async def instance_count(self, app_name: str) -> int:
        return self.instances

    @override
    async def cpu_minutes(self, plan_id: str, start: datetime, end: datetime) -> list[MetricPoint]:
        return [point for point in self.cpu if start <= point.time < end]

    @override
    async def scale_ups(self, start: datetime, end: datetime) -> list[datetime]:
        return sorted(moment for moment in self.ups if start <= moment <= end)

    @override
    async def max_throughput(self, account: str, container: str) -> int:
        return self.maxima[container]


def sources(queries: Queries, clock: FakeClock, azure: Azure | None = None) -> Sources:
    return Sources(query=queries, azure=azure or FakeAzure(), sleep=clock.sleep, now=clock.now)


def criterion(verdict: Verdict, criterion_id: str) -> bool:
    [found] = [item for item in verdict.criteria if item.id == criterion_id]
    return found.passed


async def run_s1(queries: Queries, log_text: str, clock: FakeClock | None = None) -> Verdict:
    return await evaluate_s1(
        read_log(log_text),
        start=START,
        end=END,
        run_start=RUN_START,
        sources=sources(queries, clock or FakeClock(END + timedelta(minutes=1))),
        maxima=MAXIMA,
    )


# --- S1 and the common criteria ----------------------------------------------------------------


async def test_a_clean_s1_run_passes_every_criterion() -> None:
    queries = Queries(s1_answers())
    clock = FakeClock(END + timedelta(minutes=1))

    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS), clock)

    assert verdict.status == "passed", verdict.failed
    ids = [item.id for item in verdict.criteria]
    expected = ["k6", "S1-a", "S1-b", "S1-c", "S1-d", "S1-j", "telemetry", "S1-e", "S1-f"]
    expected += ["S1-g", "S1-h", "S1-i", "C-db", "S1-k", "C-cap"]
    assert ids == expected
    assert verdict.k6_exit == 0
    assert verdict.limit_retries == 2
    assert verdict.measurements["requestUnits"]["conversations"]["ruPerTurn"] == 250.0
    assert verdict.measurements["turnChargeP95"] == 330.0
    # S1-k ran only once extractions of the window's last 5 minutes could have arrived.
    extraction = next(span for kql, span in queries.ran if "docchat.memory.extract" in kql)
    assert clock.now() >= END + timedelta(minutes=10)
    assert extraction.endswith("2026-11-03T10:20:00.000Z")


@pytest.mark.parametrize(
    "log_text",
    [
        "K6_BOOTSTRAP_FAILED download_failed\n",
        k6_log(S1_THRESHOLDS).replace("K6_EXIT 0", ""),
        k6_log(S1_THRESHOLDS, exit_code=107),
    ],
    ids=["bootstrap failed", "no K6_EXIT", "k6 error"],
)
async def test_t579_a_log_without_a_k6_result_fails_the_scenario(log_text: str) -> None:
    verdict = await run_s1(Queries(s1_answers()), log_text)
    assert verdict.status == "failed"
    assert not criterion(verdict, "k6")


async def test_a_crossed_threshold_fails_its_criterion() -> None:
    thresholds = S1_THRESHOLDS | {"http_req_duration{scenario:cheap}": {"p(95)<=200": False}}
    verdict = await run_s1(Queries(s1_answers()), k6_log(thresholds, exit_code=99))
    assert criterion(verdict, "k6")
    assert not criterion(verdict, "S1-c")
    assert [item.id for item in verdict.failed] == ["S1-c"]


async def test_t584_c_db_fails_on_a_throttled_dependency_and_names_dependency_capacity() -> None:
    queries = Queries(s1_answers())
    queries.answer("docchat.api.problems", [{"n": 3}])
    queries.answer(
        "summarize throttled = countif(StatusCode == 429) by CollectionName",
        [{"CollectionName": "conversations", "throttled": 41}],
    )

    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))

    assert not criterion(verdict, "C-db")
    assert verdict.cause == "dependency_capacity"
    assert verdict.measurements["cosmosThrottledAbsorbed"] == {"conversations": 41}


async def test_t584_c_db_fails_without_a_cause_when_no_429_was_recorded() -> None:
    queries = Queries(s1_answers())
    queries.answer("docchat.api.problems", [{"n": 1}])
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))
    assert not criterion(verdict, "C-db")
    assert verdict.cause is None


async def test_t584_c_db_reads_the_three_failure_signals() -> None:
    queries = Queries(s1_answers())
    await run_s1(queries, k6_log(S1_THRESHOLDS))
    c_db = next(kql for kql, _ in queries.ran if "docchat.api.problems" in kql)
    for signal in (
        '"dependency_unavailable"',
        '("conversation_unavailable", "turn_save_failed")',
        '"docchat.usage.record_failures"',
    ):
        assert signal in c_db


async def test_t584_c_cap_fails_on_an_overquota_row() -> None:
    queries = Queries(s1_answers())
    queries.answer("_LogOperation", [{"Count": 1}])
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))
    assert not criterion(verdict, "C-cap")
    cap = next(span for kql, span in queries.ran if "_LogOperation" in kql)
    assert cap.startswith("2026-11-03T08:00:00.000Z/")


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (
            {"CollectionName": "conversations", "ruPerTurn": 401.0, "peakRuPerSecond": 100.0},
            "RU per completed turn",
        ),
        (
            {"CollectionName": "memories", "ruPerTurn": 30.0, "peakRuPerSecond": 1000.5},
            "above prod's maximum",
        ),
        ({"CollectionName": "usage", "ruPerTurn": None, "peakRuPerSecond": 1.0}, "RU per"),
    ],
)
async def test_t584_s1_h_fails_above_400_ru_per_turn_or_prods_maximum(
    row: dict[str, Any], problem: str
) -> None:
    queries = Queries(s1_answers())
    queries.answer("ruPerTurn", [row])
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))
    [s1h] = [item for item in verdict.criteria if item.id == "S1-h"]
    assert not s1h.passed
    assert problem in s1h.detail


async def test_t584_s1_h_fails_without_any_request_charge() -> None:
    queries = Queries(s1_answers())
    queries.answer("ruPerTurn", [])
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))
    assert not criterion(verdict, "S1-h")


@pytest.mark.parametrize(
    ("marker", "rows", "failing"),
    [
        ("row_cumsum", [{"minPeak": 94}], "S1-e"),
        ("row_cumsum", [], "S1-e"),
        ("toint(ResultCode) >= 500 | summarize n", [{"n": 2}], "S1-f"),
        ("dcount(AppRoleInstance)", [{"instances": 2}], "S1-g"),
        ("percentile(ru, 95)", [{"p95": 400.5, "turns": 10}], "S1-i"),
        ("docchat.memory.extract", [{"Count": 3}], "S1-k"),
    ],
)
async def test_each_server_side_criterion_of_s1_fails_alone(
    marker: str, rows: Rows, failing: str
) -> None:
    queries = Queries(s1_answers())
    queries.answer(marker, rows)
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS))
    assert [item.id for item in verdict.failed] == [failing]


async def test_telemetry_that_never_arrives_fails_after_10_minutes_of_polls() -> None:
    queries = Queries(s1_answers())
    queries.answer("union AppRequests, AppDependencies, AppMetrics", [{"Count": 0}])
    clock = FakeClock(END + timedelta(minutes=1))
    verdict = await run_s1(queries, k6_log(S1_THRESHOLDS), clock)
    assert not criterion(verdict, "telemetry")
    polls = [kql for kql, _ in queries.ran if "union AppRequests" in kql]
    assert len(polls) == 21


def test_prods_maxima_come_from_its_environment_file() -> None:
    maxima = prod_maxima()
    config = json.loads(PROD_ENV_FILE.read_text("utf-8"))
    assert maxima == {k: float(v) for k, v in config["cosmos"]["maxThroughput"].items()}


# --- S2 ----------------------------------------------------------------------------------------

REMOVED_AT = START + timedelta(minutes=4, seconds=20)
S2_END = START + timedelta(minutes=9)


def trace(n: int) -> str:
    return f"{n:032x}"


def failed_line(trace_id: str, started: datetime) -> str:
    payload = {
        "traceId": trace_id,
        "startedAt": started.isoformat(),
        "endedAt": (started + timedelta(seconds=3)).isoformat(),
        "status": 0,
        "reason": "status_0",
    }
    return f"K6_FAILED_REQUEST {json.dumps(payload)}"


def session_line(session_id: str | None, traces: Sequence[str]) -> str:
    return f"K6_SESSION {json.dumps({'sessionId': session_id, 'traceIds': list(traces)})}"


def s2_answers(served: dict[str, str]) -> list[tuple[str, Rows]]:
    return [
        ("union AppRequests, AppDependencies, AppMetrics", [{"Count": 9}]),
        ("where before > 0 and after == 0", [{"AppRoleInstance": "inst-c"}]),
        ("AppRoleInstance !=", [{"Count": 0}]),
        (
            "project OperationId, AppRoleInstance",
            [{"OperationId": op, "AppRoleInstance": inst} for op, inst in served.items()],
        ),
        ("docchat.api.problems", [{"n": 0}]),
        ("_LogOperation", [{"Count": 0}]),
    ]


def session_api(chat_api: Any) -> SessionApi:
    tokens = {f"load-{n:02d}": f"tok-{n:02d}" for n in range(1, 21)}
    return SessionApi(
        api_base_url="https://api.example",
        tokens=tokens,
        questions=["What is new?"],
        transport=chat_api.transport,
    )


async def run_s2(queries: Queries, log_text: str, chat_api: Any, clock: FakeClock) -> Verdict:
    return await evaluate_s2(
        read_log(log_text),
        start=START,
        end=S2_END,
        removed_at=REMOVED_AT,
        run_start=RUN_START,
        sources=sources(queries, clock),
        api=session_api(chat_api),
    )


async def test_an_s2_run_whose_only_failure_was_on_the_removed_instance_passes(
    chat_api: Any,
) -> None:
    session = "3b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"
    chat_api.owners[session] = "tok-07"
    chat_api.session_turns[session] = [
        {"status": "completed", "errorCode": None},
        {"status": "failed", "errorCode": "service_restarting"},
    ]
    served = {trace(1): "inst-a", trace(2): "inst-c", trace(3): "inst-a", trace(4): "inst-b"}
    served |= {trace(5): "inst-b", trace(6): "inst-a"}
    log_text = k6_log(
        {},
        failed_line(trace(2), REMOVED_AT - timedelta(seconds=30)),
        session_line(session, [trace(1), trace(2)]),
        session_line("s-2", [trace(3), trace(4)]),
        session_line("s-3", [trace(5), trace(6)]),
    )
    clock = FakeClock(S2_END + timedelta(minutes=1))

    verdict = await run_s2(Queries(s2_answers(served)), log_text, chat_api, clock)

    assert verdict.status == "passed", verdict.failed
    assert verdict.measurements["removedInstance"] == "inst-c"
    assert verdict.measurements["sessionSpreadShare"] == pytest.approx(2 / 3)
    # S2-e sent a new turn in the session that held the failed stream, as its owner.
    [new_turn] = chat_api.turns
    assert new_turn["sessionId"] == session
    assert new_turn["token"] == "tok-07"


async def test_s2_failures_on_a_surviving_instance_or_late_fail(chat_api: Any) -> None:
    served = {trace(1): "inst-a", trace(2): "inst-a"}
    log_text = k6_log(
        {},
        failed_line(trace(1), REMOVED_AT - timedelta(seconds=10)),
        failed_line(trace(9), REMOVED_AT + timedelta(seconds=61)),
        session_line(None, [trace(9)]),
    )
    queries = Queries(s2_answers(served))
    queries.answer("AppRoleInstance !=", [{"Count": 2}])
    verdict = await run_s2(queries, log_text, chat_api, FakeClock(S2_END))
    assert {item.id for item in verdict.failed} == {"S2-a", "S2-b", "S2-c", "S2-d"}


async def test_s2_e_fails_for_a_turn_still_streaming_or_ended_otherwise(chat_api: Any) -> None:
    streaming, other = (
        "4c0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
        "5d0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    )
    chat_api.owners |= {streaming: "tok-01", other: "tok-02"}
    chat_api.session_turns[streaming] = [{"status": "streaming", "errorCode": None}]
    chat_api.session_turns[other] = [{"status": "failed", "errorCode": "internal_error"}]
    served = {trace(1): "inst-c", trace(2): "inst-c"}
    log_text = k6_log(
        {},
        failed_line(trace(1), REMOVED_AT - timedelta(seconds=5)),
        failed_line(trace(2), REMOVED_AT - timedelta(seconds=5)),
        session_line(streaming, [trace(1)]),
        session_line(other, [trace(2)]),
        session_line("lost", [trace(3)]),
    )
    clock = FakeClock(REMOVED_AT + timedelta(seconds=30))
    verdict = await run_s2(Queries(s2_answers(served)), log_text, chat_api, clock)
    [s2e] = [item for item in verdict.criteria if item.id == "S2-e"]
    assert not s2e.passed
    assert "still streaming" in s2e.detail
    assert "internal_error" in s2e.detail
    # The check ran no sooner than T_r + 210 s.
    assert clock.now() >= REMOVED_AT + timedelta(seconds=210)


async def test_s2_e_fails_when_the_new_turn_doesnt_end_with_done(chat_api: Any) -> None:
    session = "6e0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"
    chat_api.owners[session] = "tok-04"
    chat_api.session_turns[session] = [{"status": "failed", "errorCode": "turn_abandoned"}]
    chat_api.outcomes = ["error"]
    log_text = k6_log(
        {},
        failed_line(trace(1), REMOVED_AT - timedelta(seconds=1)),
        session_line(session, [trace(1)]),
    )
    verdict = await run_s2(Queries(s2_answers({})), log_text, chat_api, FakeClock(S2_END))
    [s2e] = [item for item in verdict.criteria if item.id == "S2-e"]
    assert not s2e.passed
    assert "error_event" in s2e.detail


async def test_s2_without_one_removed_instance_fails(chat_api: Any) -> None:
    queries = Queries(s2_answers({}))
    queries.answer("where before > 0 and after == 0", [])
    verdict = await run_s2(queries, k6_log({}), chat_api, FakeClock(S2_END))
    assert not criterion(verdict, "S2-removed")
    assert not criterion(verdict, "S2-b")


# --- S3 ----------------------------------------------------------------------------------------

MAIN = START
MAIN_END = MAIN + timedelta(minutes=15)
RATE: dict[str, Any] = {
    "rate": 18,
    "bounds": {"cpu": 18, "promptShields": 40, "cosmos": 55, "max": 60},
    "answerTokens": 80,
}
S3_THRESHOLDS = {
    "http_req_duration{scenario:cheap}": {"p(95)<=200": True},
    "http_req_failed{scenario:cheap}": {"rate==0": True},
    "http_req_failed{scenario:turns}": {"rate==0": True},
}


def cpu_series(values: Sequence[float]) -> list[MetricPoint]:
    return [
        MetricPoint(MAIN + timedelta(minutes=index - 5), value)
        for index, value in enumerate(values)
    ]


def s3_answers() -> list[tuple[str, Rows]]:
    return [
        ("union AppRequests, AppDependencies, AppMetrics", [{"Count": 9}]),
        ("docchat.api.problems", [{"n": 0}]),
        ("_LogOperation", [{"Count": 0}]),
    ]


async def run_s3(azure: FakeAzure, rate: dict[str, Any] = RATE) -> Verdict:
    return await evaluate_s3(
        read_log(k6_log(S3_THRESHOLDS)),
        start=MAIN,
        end=MAIN_END,
        run_start=RUN_START,
        sources=sources(Queries(s3_answers()), FakeClock(MAIN_END + timedelta(minutes=1)), azure),
        rate=rate,
        app_name="app-docchat-api-load-abc",
    )


async def test_an_s3_run_that_scaled_out_after_5_minutes_above_70_passes() -> None:
    # Five idle minutes before the main phase, then CPU above 70 from main minute 1.
    cpu = cpu_series([10] * 5 + [50, 75, 78, 80, 82, 84, 85, 60, 55, 50, 45, 40, 40, 40, 40])
    azure = FakeAzure(instances=2, scale_ups=[MAIN + timedelta(minutes=6, seconds=10)], cpu=cpu)
    verdict = await run_s3(azure)
    assert verdict.status == "passed", verdict.failed
    assert verdict.measurements["cpuBeforeScaleUp"] == [75, 78, 80, 82, 84]
    assert verdict.measurements["rate"] == RATE


@pytest.mark.parametrize(
    ("scale_up", "failing"),
    [
        (MAIN + timedelta(minutes=4, seconds=59), "S3-a"),
        (MAIN + timedelta(minutes=12, seconds=1), "S3-a"),
    ],
)
async def test_a_scale_up_outside_minutes_5_to_12_fails_s3_a(
    scale_up: datetime, failing: str
) -> None:
    cpu = cpu_series([10] * 5 + [90] * 15)
    verdict = await run_s3(FakeAzure(instances=2, scale_ups=[scale_up], cpu=cpu))
    assert failing in {item.id for item in verdict.failed}
    assert not verdict.inconclusive


async def test_a_miss_below_70_at_r_cpu_is_inconclusive_calibration_miss() -> None:
    cpu = cpu_series([10] * 5 + [60] * 15)
    verdict = await run_s3(FakeAzure(instances=1, cpu=cpu))
    assert {item.id for item in verdict.failed} == {"S3-a", "S3-b", "S3-c"}
    assert not verdict.inconclusive

    verdict = await run_s3(FakeAzure(instances=2, cpu=cpu))
    assert {item.id for item in verdict.failed} == {"S3-a", "S3-b"}
    assert verdict.status == "inconclusive"
    assert verdict.cause == "calibration_miss"
    assert verdict.measurements["nextRate"] == 23


async def test_a_miss_below_70_under_a_dependency_bound_is_dependency_bound() -> None:
    cpu = cpu_series([10] * 5 + [55] * 15)
    rate = RATE | {"rate": 12, "bounds": RATE["bounds"] | {"promptShields": 12}}
    verdict = await run_s3(FakeAzure(instances=2, cpu=cpu), rate)
    assert verdict.status == "inconclusive"
    assert verdict.cause == "dependency_bound"
    assert verdict.measurements["nextAnswerTokens"] == 320


def test_the_answer_lengths_step_by_four() -> None:
    assert [next_answer_tokens(n) for n in (80, 320, 1280, 4000)] == [320, 1280, 4000, None]


def test_the_five_minutes_before_a_moment_are_the_last_complete_ones() -> None:
    points = cpu_series([1, 2, 3, 4, 5, 6, 7, None])  # type: ignore[list-item]
    assert five_minutes_before(points, MAIN + timedelta(minutes=2, seconds=30)) == [3, 4, 5, 6, 7]
    assert five_minutes_before(points, MAIN) == [1, 2, 3, 4, 5]


# --- S4 ----------------------------------------------------------------------------------------


def run_entry(
    created: str, completed: str, *, succeeded: int = 100, failed: int = 0
) -> dict[str, Any]:
    return {
        "runId": f"run-{created}",
        "state": "completed",
        "createdAt": created,
        "completedAt": completed,
        "counts": {"succeeded": succeeded, "failed": failed, "total": 100},
    }


SET1 = {
    "runs": [
        run_entry("2026-11-03T10:00:00Z", "2026-11-03T13:00:00Z"),
        run_entry("2026-11-03T10:00:03Z", "2026-11-03T14:00:00Z"),
    ],
    "setStart": "2026-11-03T09:55:00Z",
    "setEnd": "2026-11-03T14:00:30Z",
}
SET2 = {
    "runs": [
        run_entry("2026-11-03T15:00:00Z", "2026-11-03T16:00:00Z"),
        run_entry("2026-11-03T15:00:02Z", "2026-11-03T17:00:00Z"),
    ],
    "setStart": "2026-11-03T14:55:00Z",
    "setEnd": "2026-11-03T17:00:30Z",
}


def s4_answers(throttled: float = 37) -> list[tuple[str, Rows]]:
    return [
        ("union AppRequests, AppDependencies, AppMetrics", [{"Count": 3}]),
        ("docchat.ingestion.throttled", [{"n": throttled}]),
        ("_LogOperation", [{"Count": 0}]),
    ]


async def run_s4(
    set_number: int, results: dict[str, Any], first: dict[str, Any] | None, throttled: float = 37
) -> Verdict:
    return await evaluate_s4(
        set_number,
        results=results,
        set1_results=first,
        run_start=RUN_START,
        sources=sources(
            Queries(s4_answers(throttled)), FakeClock(datetime(2026, 11, 3, 18, tzinfo=UTC))
        ),
    )


async def test_both_s4_sets_pass_when_set_2_takes_at_most_half() -> None:
    first = await run_s4(1, SET1, None)
    second = await run_s4(2, SET2, SET1)
    assert first.status == "passed", first.failed
    assert [item.id for item in first.criteria] == ["S4-a", "telemetry", "S4-c", "C-cap"]
    assert second.status == "passed", second.failed
    assert second.measurements["durationSeconds"] == 7200.0
    assert second.measurements["set1DurationSeconds"] == 14400.0


async def test_s4_fails_on_a_failed_document_a_slow_set_or_no_throttling() -> None:
    failing = SET1 | {
        "runs": [
            SET1["runs"][0],
            run_entry("2026-11-03T10:00:03Z", "2026-11-03T12:00:00Z", succeeded=99, failed=1),
        ]
    }
    assert not criterion(await run_s4(1, failing, None), "S4-a")
    slow = SET2 | {"runs": [run_entry("2026-11-03T15:00:00Z", "2026-11-03T17:00:01Z")] * 2}
    assert not criterion(await run_s4(2, slow, SET1), "S4-b")
    assert not criterion(await run_s4(1, SET1, None, throttled=0), "S4-c")


def test_a_set_with_an_unfinished_run_has_no_duration() -> None:
    assert (
        set_duration({"runs": [{"createdAt": "2026-11-03T10:00:00Z", "completedAt": None}]}) is None
    )
    assert set_duration({"runs": []}) is None


# --- Output ------------------------------------------------------------------------------------


def test_the_verdict_file_and_summary_table_are_written(tmp_path: Path) -> None:
    verdict = Verdict("s1", START, END, k6_exit=0, limit_retries=1)
    verdict.add("S1-a", True, "ok")
    summary = tmp_path / "summary.md"
    out = tmp_path / "s1" / "verdict.json"
    assert finish(verdict, out, {"GITHUB_STEP_SUMMARY": str(summary)}) == 0
    written = json.loads(out.read_text("utf-8"))
    assert written["status"] == "passed"
    assert written["window"] == {
        "start": "2026-11-03T10:00:00.000Z",
        "end": "2026-11-03T10:10:00.000Z",
    }
    assert "| S1-a | passed | ok |" in summary.read_text("utf-8")

    verdict.add("S1-b", False, "a | b")
    assert finish(verdict, out, {}) == 1
    verdict.inconclusive, verdict.cause = True, "calibration_miss"
    assert finish(verdict, out, {}) == 0
    assert json.loads(out.read_text("utf-8"))["status"] == "inconclusive"
    assert Verdict("s9").status == "failed"
