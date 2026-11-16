"""`trace show` (T-541; `86-operations.md` section 2.8.9; `85-observability.md` section 2.11)."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from docchat_ops.commands import trace
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.tests.unit.conftest import OpsHarness, Outcome

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ARGS = ("--ticket", "INC-7", "--reason", "user reported a failure")
QUERY_URL = "https://api.loganalytics.io/v1/workspaces/"
REFERENCE_COLUMNS = [
    "TimeGenerated",
    "Type",
    "Name",
    "Message",
    "DurationMs",
    "ResultCode",
    "Success",
    "AppRoleName",
    "Properties",
]
LINK_COLUMNS = ["TimeGenerated", "Name", "DurationMs", "Properties"]


def _answer(ops: OpsHarness, direct: list[list[object]], linked: list[list[object]]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if query.startswith("union"):
            return httpx.Response(200, json=ops.logs_body(REFERENCE_COLUMNS, direct))
        return httpx.Response(200, json=ops.logs_body(LINK_COLUMNS, linked))

    ops.http.route("POST", QUERY_URL, respond)


def _show(ops: OpsHarness, *extra: str) -> Outcome:
    return ops.run("--env", "dev", "trace", "show", TRACE_ID, *ARGS, *extra)


def test_queries_are_the_spec_ones(spec_text: Callable[[str], str]) -> None:
    text = spec_text("85-observability.md")
    assert f"```kusto\n{trace.REFERENCE_QUERY}\n```" in text
    assert f"```kusto\n{trace.LINK_QUERY}\n```" in text


def test_logs_client_timeouts() -> None:
    assert trace.TIMEOUT.connect == 5.0
    assert trace.TIMEOUT.read == 30.0


@pytest.mark.parametrize(
    "value", ["", "4BF92F3577B34DA6A3CE929D0E0E4736", "4bf92f35", TRACE_ID + "0", "g" * 32]
)
def test_t541_trace_id_must_be_32_lower_case_hex(ops: OpsHarness, value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        trace.trace_id(value)
    outcome = ops.run("--env", "dev", "trace", "show", value, *ARGS)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0
    assert ops.http.requests == []


def test_t541_prints_only_the_four_properties(ops: OpsHarness) -> None:
    properties = json.dumps(
        {
            "docchat.error_code": "dependency_unavailable",
            "docchat.session_id": "s-1",
            "docchat.turn_id": "t-1",
            "enduser.id": ops.member_oid,
            "docchat.question": ops.sentinel,
            "nested": {"x": ops.sentinel},
        }
    )
    direct: list[list[object]] = [
        [
            "2026-10-30T10:00:02Z",
            "AppDependencies",
            "docchat.chat.turn",
            ops.sentinel,
            812.5,
            "0",
            True,
            "docchat-api",
            properties,
        ],
        [
            "2026-10-30T10:00:01Z",
            "AppRequests",
            "POST /v1/chat",
            ops.sentinel,
            900,
            "200",
            True,
            "docchat-api",
            {"enduser.id": ops.member_oid, "body": ops.sentinel},
        ],
    ]
    linked: list[list[object]] = [
        [
            "2026-10-30T10:00:03Z",
            "docchat.ingestion.dispatcher.claim",
            12,
            json.dumps({"_MS.links": f'[{{"operation_Id":"{TRACE_ID}"}}]'}),
        ],
    ]
    _answer(ops, direct, linked)
    outcome = _show(ops, "--json")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ops.sentinel not in outcome.stdout
    assert ops.sentinel not in outcome.stderr
    records = outcome.json()["records"]
    assert [record["time"] for record in records] == [
        "2026-10-30T10:00:01Z",
        "2026-10-30T10:00:02Z",
        "2026-10-30T10:00:03Z",
    ]
    assert records[0]["properties"] == {"enduser.id": ops.member_oid}
    assert records[1] == {
        "time": "2026-10-30T10:00:02Z",
        "type": "AppDependencies",
        "name": "docchat.chat.turn",
        "durationMs": 812.5,
        "success": True,
        "resultCode": "0",
        "roleName": "docchat-api",
        "properties": {
            "docchat.error_code": "dependency_unavailable",
            "docchat.session_id": "s-1",
            "docchat.turn_id": "t-1",
            "enduser.id": ops.member_oid,
        },
    }
    assert records[2]["type"] == "AppDependencies"
    assert records[2]["properties"] == {}
    assert outcome.json()["traceId"] == TRACE_ID


def test_runs_both_queries_against_the_workspace(ops: OpsHarness) -> None:
    _answer(ops, [["2026-10-30T10:00:00Z", "AppTraces", "x", "m", None, None, None, "r", None]], [])
    outcome = _show(ops)
    assert outcome.code == ExitCode.SUCCESS
    sent = ops.http.sent("POST", QUERY_URL)
    assert [str(request.url) for request in sent] == [f"{QUERY_URL}{ops.workspace_id}/query"] * 2
    bodies = [json.loads(request.content) for request in sent]
    assert bodies[0] == {
        "query": trace.REFERENCE_QUERY.replace("<trace ID>", TRACE_ID),
        "timespan": "P30D",
    }
    assert bodies[1] == {
        "query": trace.LINK_QUERY.replace("<trace ID>", TRACE_ID),
        "timespan": "P30D",
    }
    assert all(
        request.headers["Authorization"] == "Bearer token-for-https://api.loganalytics.io/.default"
        for request in sent
    )
    assert ops.credential.closed
    assert "AppTraces" in outcome.stdout
    assert outcome.stdout.splitlines()[2].split() == list(trace.COLUMNS)


def test_t541_no_record_exits_4(ops: OpsHarness) -> None:
    _answer(ops, [], [])
    outcome = _show(ops)
    assert outcome.code == ExitCode.NOT_FOUND
    assert "no record of trace" in outcome.failure


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ExitCode.ACCESS_DENIED),
        (403, ExitCode.ACCESS_DENIED),
        (404, ExitCode.NOT_FOUND),
        (429, ExitCode.UNREACHABLE),
        (503, ExitCode.UNREACHABLE),
        (400, ExitCode.UNEXPECTED),
    ],
)
def test_store_answers_map_to_exit_codes(ops: OpsHarness, status: int, code: ExitCode) -> None:
    ops.http.answer("POST", QUERY_URL, status, {"error": {"message": ops.sentinel}})
    outcome = _show(ops)
    assert outcome.code == code
    assert ops.sentinel not in outcome.stderr


def test_an_unreachable_api_exits_7(ops: OpsHarness) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    ops.http.route("POST", QUERY_URL, refuse)
    assert _show(ops).code == ExitCode.UNREACHABLE


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"tables": []}),
        httpx.Response(200, json={"tables": ["x"]}),
    ],
)
def test_malformed_answers_exit_1(ops: OpsHarness, response: httpx.Response) -> None:
    ops.http.route("POST", QUERY_URL, lambda request: response)
    assert _show(ops).code == ExitCode.UNEXPECTED


def test_workspace_id_must_be_a_uuid(ops: OpsHarness) -> None:
    ops.write_azd("dev", DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID="log-docchat-dev")
    outcome = _show(ops)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0


def test_missing_azd_file_exits_2(ops: OpsHarness) -> None:
    (ops.root / ".azure" / "dev" / ".env").unlink()
    outcome = _show(ops)
    assert outcome.code == ExitCode.USAGE
    assert "run azd env refresh -e dev" in outcome.failure


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("not json", {}),
        ("[1, 2]", {}),
        (None, {}),
        ({"enduser.id": "x", "docchat.turn_id": ["list"]}, {"enduser.id": "x"}),
    ],
)
def test_properties_keep_only_scalar_listed_attributes(
    value: object, expected: dict[str, object]
) -> None:
    assert trace.record({"Properties": value})["properties"] == expected


def test_non_scalar_columns_are_printed_as_text() -> None:
    assert trace.record({"Name": ["a"]})["name"] == "['a']"
