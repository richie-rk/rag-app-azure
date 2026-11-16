"""T-501: the alert query unit tests (85-observability.md section 6.4 and acceptance criterion 18).

Each rate rule's KQL, as the compiled `alerts.bicep` deploys it in dev, runs over constant tables
at and around its minimum-volume guard and its threshold, both read from the rule's row of section
6.3 ("> <p>% of …, with ≥ <n> …"); A58's KQL runs over constant `ingestion_item_failed` events.
Every input also holds a row the query must ignore (another role, operation, metric or reason)
that would change the answer if it were counted. The queries run on `kql_subset`, an offline
evaluator of the part of KQL these rules use, because Azure Monitor's engine isn't available to a
unit test; the query check of section 5.11 (T-499) runs the same queries against the workspace.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
from compiled_support import REPO_ROOT, MainFactory, of_type
from kql_subset import Row, run

OBSERVABILITY = REPO_ROOT / "docs" / "spec" / "85-observability.md"
Tables = dict[str, list[Row]]


def _rate_rules() -> dict[str, tuple[int, int]]:
    """Section 6.3's rate rules: alert ID to (minimum volume, k), where the rule fires when more
    than one k-th of the volume is bad, that is when bad * k > volume."""
    text = OBSERVABILITY.read_text(encoding="utf-8")
    rules: dict[str, tuple[int, int]] = {}
    for alert_id, signal in re.findall(
        r"^\| (A\d\d) \| `[a-z0-9-]+`[^|]* \| ([^|]+) \|", text, re.M
    ):
        share = re.search(r"> (\d+)% of", signal)
        guard = re.search(r"with ≥ (\d+)", signal)
        if share and guard:
            assert 100 % int(share.group(1)) == 0, alert_id
            rules[alert_id] = (int(guard.group(1)), 100 // int(share.group(1)))
    return rules


RATE_RULES = _rate_rules()


def _request(name: str, code: str, count: int, role: str = "docchat-api") -> Row:
    return {"AppRoleName": role, "Name": name, "ResultCode": code, "ItemCount": count}


def _dependency(name: str, count: int, properties: dict[str, str]) -> Row:
    return {
        "AppRoleName": "docchat-api",
        "Name": name,
        "ItemCount": count,
        "Properties": properties,
    }


def _metric(name: str, value: int, *, item_count: int = 1, **properties: str) -> Row:
    return {
        "AppRoleName": "docchat-api",
        "Name": name,
        "Sum": value,
        "ItemCount": item_count,
        "Properties": properties,
    }


def _api_failures(volume: int, bad: int) -> Tables:
    return {
        "AppRequests": [
            _request("GET /v1/me", "200", volume - bad),
            _request("POST /v1/chat/turns", "503", bad),
            # Another role's failures aren't the API's.
            _request("POST /v1/uploads", "500", 1000, role="docchat-ingestion"),
        ]
    }


def _provisioning(volume: int, bad: int) -> Tables:
    outcome = "docchat.users.outcome"
    return {
        "AppRequests": [
            _request("GET /v1/me", "200", volume),
            # Health probes don't count as API requests.
            _request("GET /healthz", "200", 1000),
        ],
        "AppDependencies": [
            _dependency("docchat.users.ensure", volume - bad, {outcome: "touched"}),
            _dependency("docchat.users.ensure", bad, {outcome: "failed"}),
        ],
    }


def _failed_turns(volume: int, bad: int) -> Tables:
    return {
        "AppMetrics": [
            _metric("docchat.chat.turns", volume - bad, outcome="completed"),
            _metric("docchat.chat.turns", bad, outcome="failed"),
            _metric("docchat.summary.rebuilds", 1000, outcome="failed"),
        ]
    }


def _throttling(volume: int, bad: int) -> Tables:
    return {
        "AppMetrics": [
            # The calls are the duration histogram's count; its sum is milliseconds.
            _metric("docchat.openai.duration", 900 * volume, item_count=volume, outcome="ok"),
            _metric("docchat.openai.throttled", bad, deployment="chat"),
            _metric("docchat.ingestion.throttled", 1000, deployment="embed-ingest-large"),
        ]
    }


def _screening(volume: int, bad: int) -> Tables:
    return {
        "AppMetrics": [
            _metric("docchat.chat.turns", volume, outcome="completed"),
            _metric("docchat.chat.screening.failures", bad),
            _metric("docchat.chat.screening.flagged_sources", 1000),
        ]
    }


def _rate_rejections(volume: int, bad: int) -> Tables:
    return {
        "AppRequests": [
            _request("POST /v1/chat/turns", "200", volume),
            _request("GET /v1/me", "200", 1000),
        ],
        "AppMetrics": [
            _metric("docchat.ratelimit.rejections", bad - bad // 2, reason="rate"),
            _metric("docchat.ratelimit.rejections", bad // 2, reason="concurrency"),
            _metric("docchat.ratelimit.rejections", 1000, reason="quota_day"),
        ],
    }


def _outcomes(name: str, good: str, other: str) -> Callable[[int, int], Tables]:
    def tables(volume: int, bad: int) -> Tables:
        return {
            "AppMetrics": [
                _metric(name, volume - bad, outcome=good),
                _metric(name, bad, outcome="failed"),
                _metric(other, 1000, outcome="failed"),
            ]
        }

    return tables


def _abandoned(volume: int, bad: int) -> Tables:
    return {
        "AppMetrics": [
            _metric(
                "docchat.conversations.turns_finalized",
                volume,
                status="completed",
                error_code="none",
            ),
            _metric("docchat.conversations.abandoned", bad, source="takeover"),
            _metric("docchat.conversations.duplicates", 1000, outcome="conflict"),
        ]
    }


def _invalid_tokens(volume: int, bad: int) -> Tables:
    return {
        "AppMetrics": [
            _metric("docchat.auth.validations", volume - bad, outcome="ok", reason="none"),
            _metric("docchat.auth.validations", bad, outcome="invalid", reason="expired"),
            _metric("docchat.chat.turns", 1000, outcome="invalid"),
        ]
    }


# Each rate rule's input: `volume` units of its denominator, of which `bad` count against it.
RATE_INPUTS: dict[str, Callable[[int, int], Tables]] = {
    "A03": _api_failures,
    "A04": _api_failures,
    "A05": _provisioning,
    "A07": _failed_turns,
    "A08": _throttling,
    "A09": _screening,
    "A13": _rate_rejections,
    "A25": _outcomes("docchat.summary.rebuilds", "succeeded", "docchat.memory.extractions"),
    "A26": _outcomes("docchat.memory.extractions", "done", "docchat.summary.rebuilds"),
    "A27": _abandoned,
    "A38": _invalid_tokens,
}


class _Case(NamedTuple):
    name: str
    volume: int
    bad: int
    fires: bool


def _cases(guard: int, k: int) -> list[_Case]:
    """At and around the guard and the threshold."""
    return [
        _Case("below the guard, all bad", guard - 1, guard - 1, False),
        _Case("at the guard, at the threshold", guard, guard // k, False),
        _Case("at the guard, above the threshold", guard, guard // k + 1, True),
        _Case("ten times the guard, none bad", 10 * guard, 0, False),
        _Case("ten times the guard, at the threshold", 10 * guard, 10 * guard // k, False),
        _Case("ten times the guard, above the threshold", 10 * guard, 10 * guard // k + 1, True),
    ]


@pytest.fixture(scope="module")
def queries(main_deployment: MainFactory) -> dict[str, str]:
    """Each log search rule's KQL as the compiled template deploys it in dev."""
    rules = of_type(main_deployment("dev"), "Microsoft.Insights/scheduledQueryRules")
    found: dict[str, str] = {}
    for rule in rules:
        properties: dict[str, Any] = rule.properties
        found[properties["description"].split(" ", 1)[0]] = properties["criteria"]["allOf"][0][
            "query"
        ]
    return found


def test_t501_every_rate_rule_has_its_inputs() -> None:
    assert set(RATE_INPUTS) == set(RATE_RULES)
    assert RATE_RULES["A27"] == (20, 20)


@pytest.mark.parametrize(
    ("alert_id", "case"),
    [
        pytest.param(alert_id, case, id=f"{alert_id}-{case.name}")
        for alert_id, (guard, k) in sorted(RATE_RULES.items())
        for case in _cases(guard, k)
    ],
)
def test_t501_rate_rule_fires_only_above_its_guard_and_threshold(
    queries: dict[str, str], alert_id: str, case: _Case
) -> None:
    rows = run(queries[alert_id], RATE_INPUTS[alert_id](case.volume, case.bad))
    assert len(rows) == (1 if case.fires else 0)


@pytest.mark.parametrize(
    ("abandoned", "finalized", "fires"), [(1, 19, False), (1, 20, False), (2, 20, True)]
)
def test_t501_abandoned_turns_need_twenty_finalised_turns(
    queries: dict[str, str], abandoned: int, finalized: int, fires: bool
) -> None:
    rows = run(queries["A27"], _abandoned(finalized, abandoned))
    assert bool(rows) is fires


def _failed_item(time: str, operation: str, outcome_code: str, name: str) -> Row:
    return {
        "TimeGenerated": time,
        "Name": name,
        "Properties": {
            "runId": f"run-{time}",
            "itemId": f"item-{time}",
            "operation": operation,
            "stage": "render",
            "outcomeCode": outcome_code,
        },
    }


def test_t501_rerender_failures(queries: dict[str, str]) -> None:
    events = [
        _failed_item("t1", "rerender", "render_failed", "ingestion_item_failed"),
        _failed_item("t2", "rerender", "render_too_large", "ingestion_item_failed"),
        _failed_item("t3", "ingest", "render_failed", "ingestion_item_failed"),
        _failed_item("t4", "rerender", "storage_unavailable", "ingestion_item_failed"),
        _failed_item("t5", "rerender", "render_failed", "ingestion_run_finished"),
    ]
    rows = run(queries["A58"], {"AppEvents": events})
    assert rows == [
        {
            "TimeGenerated": "t1",
            "runId": "run-t1",
            "itemId": "item-t1",
            "outcomeCode": "render_failed",
        },
        {
            "TimeGenerated": "t2",
            "runId": "run-t2",
            "itemId": "item-t2",
            "outcomeCode": "render_too_large",
        },
    ]
