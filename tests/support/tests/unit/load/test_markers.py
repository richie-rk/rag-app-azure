from __future__ import annotations

import json
from datetime import UTC, datetime

from docchat_testing.load.markers import marker_text, parse_log, read_log

TRACE = "0af7651916cd43dd8448eb211c80319c"
SUMMARY = {
    "thresholds": {
        "http_req_failed{scenario:streams}": {"rate==0": True},
        "http_req_duration{scenario:cheap}": {"p(95)<=200": False},
    },
    "values": {"http_req_duration{scenario:cheap}": {"p(95)": 212.5}},
    "limitRetries": 4,
}
FAILED = {
    "traceId": TRACE,
    "startedAt": "2026-11-03T10:00:00.000Z",
    "endedAt": "2026-11-03T10:00:11.500Z",
    "status": 502,
    "reason": "status_502",
}


def test_a_complete_log_yields_every_marker() -> None:
    text = "\n".join(
        [
            "Status: Downloaded newer image",
            f"K6_FAILED_REQUEST {json.dumps(FAILED)}",
            f"K6_SESSION {json.dumps({'sessionId': 's-1', 'traceIds': [TRACE, 'b' * 32]})}",
            f"K6_SUMMARY_JSON {json.dumps(SUMMARY)}",
            "K6_EXIT 99",
        ]
    )
    log = read_log(text)

    assert log.ran
    assert log.exit_code == 99
    assert log.bootstrap_failure is None
    assert log.limit_retries == 4
    assert log.threshold("http_req_failed{scenario:streams}", "rate==0") is True
    assert log.threshold("http_req_duration{scenario:cheap}", "p(95)<=200") is False
    assert log.threshold("checks", "rate==1") is None
    assert log.value("http_req_duration{scenario:cheap}", "p(95)") == 212.5
    assert log.value("http_req_duration{scenario:cheap}", "p(99)") is None
    [failure] = log.failed_requests
    assert failure.trace_id == TRACE
    assert failure.started_at == datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert (failure.status, failure.reason) == (502, "status_502")
    [session] = log.sessions
    assert session.session_id == "s-1"
    assert session.trace_ids == (TRACE, "b" * 32)


def test_t579_a_bootstrap_failure_or_a_missing_exit_means_k6_never_ran() -> None:
    failed = read_log("K6_BOOTSTRAP_FAILED checksum_mismatch\n")
    assert failed.bootstrap_failure == "checksum_mismatch"
    assert not failed.ran
    silent = read_log("some output\nK6_SUMMARY_JSON {}\n")
    assert silent.exit_code is None
    assert not silent.ran


def test_markers_inside_k6s_default_log_format_are_read() -> None:
    payload = json.dumps({"sessionId": None, "traceIds": [TRACE]})
    escaped = payload.replace('"', '\\"')
    line = f'time="2026-11-03T10:00:00Z" level=info msg="K6_SESSION {escaped}" source=console'
    assert marker_text(line) == f"K6_SESSION {payload}"
    [session] = parse_log([line]).sessions
    assert session.session_id is None
    assert marker_text('time="x" level=info msg="hello" source=console') is None
    assert marker_text("plain line") is None


def test_unreadable_markers_are_counted_not_raised() -> None:
    log = parse_log(
        [
            "K6_EXIT many",
            "K6_FAILED_REQUEST {not json",
            f"K6_FAILED_REQUEST {json.dumps(FAILED | {'traceId': 'short'})}",
            'K6_SESSION {"sessionId": 5, "traceIds": []}',
            'K6_SESSION {"sessionId": "s", "traceIds": [1]}',
            "K6_SUMMARY_JSON [1]",
        ]
    )
    assert log.unreadable == 6
    assert log.exit_code is None
    assert log.summary is None
    assert log.limit_retries is None
    assert log.value("x", "y") is None


def test_a_summary_without_thresholds_names_none() -> None:
    log = read_log('K6_SUMMARY_JSON {"thresholds": [], "limitRetries": "x"}\nK6_EXIT 0')
    assert log.threshold("checks", "rate==1") is None
    assert log.limit_retries is None
