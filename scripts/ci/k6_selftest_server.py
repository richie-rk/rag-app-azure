"""Local HTTP server for the k6 library self-test (T-581, 84-testing.md section 7.1, ADR 0135).

`k6 run tests/load/k6/selftest.js` exercises the request helper of `tests/load/k6/lib.js` against
this server on 127.0.0.1: a `429` whose problem `code` is `too_many_requests` or
`write_rate_limited` must be retried once after `retryAfterSeconds`, and a `429` with any other code
must reach the script unretried (84-testing.md section 6.4 item 7). The server answers each `429`
exactly as the API does: an RFC 9457 problem (`application/problem+json`) with `type`, `title`,
`status`, `detail`, `code`, `correlationId` and `retryAfterSeconds`, and a `Retry-After` header with
the same integer (11-api.md section 1.5).

Routes (any method; a request body is read and ignored):

- `/limit/<code>/<key>[?times=<n>&retryAfter=<s>]`: the first `n` requests for `<key>` (default 1,
  1 to 10) answer `429` with problem `<code>` (one of the API's `429` codes) and `retryAfterSeconds`
  `s` (default 1, 1 to 120); every later request answers `200` `{"key", "attempt"}`.
- `/ok/<key>`: always `200` `{"key", "attempt"}`.
- `/stats/<key>`: `200` `{"key", "requests", "secondsSinceFirst"}`, the number of requests `<key>`
  received and, for each, the seconds since its first one, so the self-test can assert one retry
  after `retryAfterSeconds` or none at all. A stats read isn't counted.
- anything else: `404` problem `route_not_found`.

Usage: python scripts/ci/k6_selftest_server.py [--port <n>]
It binds 127.0.0.1 only (port 0, the default, picks a free port) and, once listening, prints
`K6_SELFTEST_SERVER_READY http://127.0.0.1:<port>` on standard output; it runs until interrupted.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, override
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from collections.abc import Sequence

HOST = "127.0.0.1"
KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_TIMES = 10
MAX_RETRY_AFTER = 120
MAX_BODY_BYTES = 1_048_576
KEY_ROUTE_PARTS = 2  # /ok/<key>, /stats/<key>
LIMIT_ROUTE_PARTS = 3  # /limit/<code>/<key>

# The API's 429 codes with their one title and detail (11-api.md section 2).
PROBLEMS_429 = {
    "too_many_requests": (
        "Too many requests",
        "You're sending requests too quickly. Wait a moment, then retry.",
    ),
    "write_rate_limited": (
        "Too many changes",
        "You're making changes too quickly. Wait a moment, then retry.",
    ),
    "rate_limited": (
        "Request limit reached",
        "You're sending requests too quickly. Wait a moment, then retry.",
    ),
    "too_many_streams": (
        "Too many answers in progress",
        "You already have answers being written. Wait for one to finish, then retry.",
    ),
    "token_quota_exceeded": (
        "Usage limit reached",
        "You've reached your usage limit for now. You can ask more after the time shown.",
    ),
    "too_many_downloads": (
        "Too many sources loading",
        "Too many sources are loading at once. Wait for one to finish, then retry.",
    ),
    "upload_busy": (
        "Too many uploads",
        "Too many uploads are running. Retrying in a few seconds.",
    ),
}
NOT_FOUND = ("Not found", "The requested resource doesn't exist.")


class RequestLog:
    """Arrival times per key, shared by the handler threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrivals: dict[str, list[float]] = {}

    def record(self, key: str) -> int:
        """Record one request for `key` and return its 1-based attempt number."""
        with self._lock:
            arrivals = self._arrivals.setdefault(key, [])
            arrivals.append(time.monotonic())
            return len(arrivals)

    def stats(self, key: str) -> dict[str, object]:
        with self._lock:
            arrivals = list(self._arrivals.get(key, []))
        first = arrivals[0] if arrivals else 0.0
        return {
            "key": key,
            "requests": len(arrivals),
            "secondsSinceFirst": [round(arrival - first, 3) for arrival in arrivals],
        }


def _bounded_int(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int | None:
    values = query.get(name)
    if not values:
        return default
    if not values[0].isdigit():
        return None
    value = int(values[0])
    return value if 1 <= value <= maximum else None


class SelfTestHandler(BaseHTTPRequestHandler):
    """Answers the self-test routes; `log` is set by `make_server`."""

    log: ClassVar[RequestLog]
    server_version = "docchat-k6-selftest"
    sys_version = ""

    def _send_json(
        self, status: int, body: dict[str, object], content_type: str, extra: dict[str, str]
    ) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _problem(self, status: int, code: str, title: str, detail: str, retry_after: int) -> None:
        body: dict[str, object] = {
            "type": f"urn:docchat:problem:{code}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "correlationId": secrets.token_hex(16),
        }
        headers: dict[str, str] = {}
        if retry_after:
            body["retryAfterSeconds"] = retry_after
            headers["Retry-After"] = str(retry_after)
        self._send_json(status, body, "application/problem+json", headers)

    def _not_found(self) -> None:
        self._problem(404, "route_not_found", NOT_FOUND[0], NOT_FOUND[1], 0)

    def _drain_body(self) -> None:
        length = self.headers.get("Content-Length")
        if length and length.isdigit() and int(length) <= MAX_BODY_BYTES:
            self.rfile.read(int(length))

    def _handle(self) -> None:
        self._drain_body()
        url = urlsplit(self.path)
        parts = [part for part in url.path.split("/") if part]
        query = parse_qs(url.query)
        if len(parts) == KEY_ROUTE_PARTS and parts[0] in {"ok", "stats"} and KEY.match(parts[1]):
            if parts[0] == "stats":
                self._send_json(200, self.log.stats(parts[1]), "application/json", {})
                return
            attempt = self.log.record(parts[1])
            self._send_json(200, {"key": parts[1], "attempt": attempt}, "application/json", {})
            return
        is_limit = len(parts) == LIMIT_ROUTE_PARTS and parts[0] == "limit"
        if not is_limit or parts[1] not in PROBLEMS_429 or not KEY.match(parts[2]):
            self._not_found()
            return
        times = _bounded_int(query, "times", 1, MAX_TIMES)
        retry_after = _bounded_int(query, "retryAfter", 1, MAX_RETRY_AFTER)
        if times is None or retry_after is None:
            self._not_found()
            return
        attempt = self.log.record(parts[2])
        if attempt <= times:
            title, detail = PROBLEMS_429[parts[1]]
            self._problem(429, parts[1], title, detail, retry_after)
            return
        self._send_json(200, {"key": parts[2], "attempt": attempt}, "application/json", {})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    @override
    def log_message(self, format: str, *args: object) -> None:
        # Request lines would interleave with k6's output in the CI log; the stats route is the
        # record the self-test reads.
        return


def make_server(port: int = 0) -> ThreadingHTTPServer:
    """Create the server bound to 127.0.0.1 with a fresh request log; the caller serves it."""
    handler = type("BoundSelfTestHandler", (SelfTestHandler,), {"log": RequestLog()})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the k6 library self-test routes.")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    with make_server(args.port) as server:
        sys.stdout.write(f"K6_SELFTEST_SERVER_READY http://{HOST}:{server.server_address[1]}\n")
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
