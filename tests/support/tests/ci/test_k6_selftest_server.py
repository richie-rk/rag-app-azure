"""The k6 self-test server answers as the API does (T-581's server, 84-testing.md section 6.4)."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import k6_selftest_server  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def port() -> Iterator[int]:
    server = k6_selftest_server.make_server(0)
    # A short poll interval keeps shutdown() from waiting half a second per test.
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    port: int, method: str, path: str, body: bytes | None = None
) -> tuple[int, dict[str, str], Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


@pytest.mark.parametrize("code", ["too_many_requests", "write_rate_limited"])
def test_a_retryable_limit_answers_429_once_then_200(port: int, code: str) -> None:
    status, headers, problem = request(port, "POST", f"/limit/{code}/k1", b'{"a": 1}')

    assert status == 429
    assert headers["Content-Type"] == "application/problem+json"
    assert headers["Retry-After"] == "1"
    assert problem["code"] == code
    assert problem["status"] == 429
    assert problem["type"] == f"urn:docchat:problem:{code}"
    assert problem["retryAfterSeconds"] == 1
    assert len(problem["correlationId"]) == 32
    assert set(problem) == {
        "type",
        "title",
        "status",
        "detail",
        "code",
        "correlationId",
        "retryAfterSeconds",
    }

    status, _, body = request(port, "POST", f"/limit/{code}/k1")
    assert (status, body) == (200, {"key": "k1", "attempt": 2})


def test_times_and_retry_after_shape_the_answers(port: int) -> None:
    path = "/limit/rate_limited/k2?times=2&retryAfter=7"
    first = request(port, "GET", path)
    second = request(port, "GET", path)
    third = request(port, "GET", path)

    assert [first[0], second[0], third[0]] == [429, 429, 200]
    assert first[1]["Retry-After"] == "7"
    assert first[2]["title"] == "Request limit reached"


def test_stats_count_requests_without_counting_themselves(port: int) -> None:
    request(port, "GET", "/limit/upload_busy/k3")
    request(port, "GET", "/ok/k3")
    _, _, stats = request(port, "GET", "/stats/k3")
    _, _, again = request(port, "GET", "/stats/k3")

    assert stats["requests"] == 2
    assert again["requests"] == 2
    assert stats["secondsSinceFirst"][0] == 0


def test_keys_are_independent(port: int) -> None:
    assert request(port, "GET", "/limit/too_many_requests/a")[0] == 429
    assert request(port, "GET", "/limit/too_many_requests/b")[0] == 429
    assert request(port, "GET", "/stats/never")[2] == {
        "key": "never",
        "requests": 0,
        "secondsSinceFirst": [],
    }


@pytest.mark.parametrize(
    "path",
    [
        "/limit/not_a_code/k",
        "/limit/too_many_requests/k?times=0",
        "/limit/too_many_requests/k?retryAfter=121",
        "/limit/too_many_requests/bad key",
        "/elsewhere",
    ],
)
def test_anything_else_is_a_route_not_found_problem(port: int, path: str) -> None:
    status, headers, problem = request(port, "DELETE", path.replace(" ", "%20"))

    assert status == 404
    assert headers["Content-Type"] == "application/problem+json"
    assert problem["code"] == "route_not_found"
    assert "Retry-After" not in headers
