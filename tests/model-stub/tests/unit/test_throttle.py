from __future__ import annotations

from docchat_model_stub.throttle import Throttle, retry_headers


def test_every_nth_request_per_deployment() -> None:
    throttle = Throttle(every_n=3, deployments=frozenset())
    assert [throttle.hit("chat") for _ in range(6)] == [False, False, True, False, False, True]
    assert [throttle.hit("embed") for _ in range(3)] == [False, False, True]


def test_only_listed_deployments_are_throttled() -> None:
    throttle = Throttle(every_n=1, deployments=frozenset({"ingest-summary"}))
    assert throttle.hit("ingest-summary")
    assert not throttle.hit("chat")


def test_zero_turns_throttling_off() -> None:
    throttle = Throttle(every_n=0, deployments=frozenset())
    assert not any(throttle.hit("chat") for _ in range(10))


def test_retry_headers_round_seconds_up() -> None:
    assert retry_headers(200) == {"retry-after-ms": "200", "retry-after": "1"}
    assert retry_headers(2001) == {"retry-after-ms": "2001", "retry-after": "3"}
    assert retry_headers(0) == {"retry-after-ms": "0", "retry-after": "0"}
