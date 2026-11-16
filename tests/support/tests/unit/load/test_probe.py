from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from docchat_testing.clock import FakeClock
from docchat_testing.load.azcli import Azure
from docchat_testing.load.common import UsageError
from docchat_testing.load.probe import (
    A41_THRESHOLD_MS,
    A42_THRESHOLD_MS,
    DIRECTIVES,
    TURNS,
    chat_deployment,
    check,
    send,
)
from docchat_testing.load.verdict import Sources
from docchat_testing.logquery import Rows

BASE = "https://app-docchat-api-load-abc123.azurewebsites.net"
RUN_START = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


def ticking(start: datetime) -> Iterator[datetime]:
    moment = start
    while True:
        yield moment
        moment += timedelta(seconds=22)


async def test_the_send_phase_runs_25_turns_in_one_session_and_records_the_window(
    tmp_path: Path, chat_api: Any
) -> None:
    times = ticking(datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    tokens = {"load-01": "tok-01", "load-02": "tok-02"}

    window = await send(
        BASE,
        tokens,
        ["Q1?", "Q2?"],
        tmp_path,
        transport=chat_api.transport,
        now=lambda: next(times),
    )

    assert len(chat_api.turns) == TURNS == 25
    sessions = {turn["sessionId"] for turn in chat_api.turns[1:]}
    assert chat_api.turns[0]["sessionId"] is None
    assert len(sessions) == 1
    assert all(turn["token"] == "tok-01" for turn in chat_api.turns)
    assert chat_api.turns[0]["question"] == f"Q1? {DIRECTIVES}"
    assert DIRECTIVES == "[[stub:first_ms=12000@stream]] [[stub:first_ms=9000@json]]"
    assert window["start"] == "2026-11-03T10:00:00.000Z"
    # The last done came at the 26th tick; the window ends 60 s after it.
    assert window["end"] == "2026-11-03T10:10:10.000Z"
    assert window["done"] == 25
    assert json.loads((tmp_path / "window.json").read_text("utf-8")) == window


async def test_a_failed_probe_turn_is_recorded(tmp_path: Path, chat_api: Any) -> None:
    chat_api.outcomes = ["status_503"]
    window = await send(BASE, {"load-01": "t"}, ["Q?"], tmp_path, transport=chat_api.transport)
    assert window["done"] == 24
    assert window["outcomes"][0] == {"done": False, "status": 503, "reason": "status_503"}


async def test_the_send_phase_needs_load_01s_token(tmp_path: Path, chat_api: Any) -> None:
    with pytest.raises(UsageError, match="load-01"):
        await send(BASE, {"load-02": "t"}, ["Q?"], tmp_path, transport=chat_api.transport)


class NoAz(Azure):
    def __init__(self) -> None:
        async def refuse(arguments: object) -> str:
            raise AssertionError(arguments)

        super().__init__(refuse)


def probe_queries(a41: Rows, a42: Rows, cap: int = 0) -> Any:
    ran: list[tuple[str, str]] = []

    async def query(kql: str, timespan: str) -> Rows:
        ran.append((kql, timespan))
        if "docchat.chat.generate" in kql:
            return a41
        if "docchat.openai.duration" in kql:
            return a42
        if "_LogOperation" in kql:
            return [{"Count": cap}]
        return []

    query.ran = ran  # type: ignore[attr-defined]
    return query


def write_window(directory: Path) -> None:
    window = {"start": "2026-11-03T10:00:00.000Z", "end": "2026-11-03T10:10:10.000Z", "done": 25}
    (directory / "window.json").write_text(json.dumps(window), "utf-8")


async def test_the_check_phase_passes_when_a41_and_a42_fire_on_their_signals(
    tmp_path: Path,
) -> None:
    write_window(tmp_path)
    query = probe_queries(
        [{"deployment": "chat", "calls": 25, "p95": 12100.0}],
        [{"deployment": "chat-small", "call": "memory", "calls": 25, "meanMs": 9050.0}],
    )
    clock = FakeClock(datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    sources = Sources(query=query, azure=NoAz(), sleep=clock.sleep, now=clock.now)

    verdict = await check(tmp_path, RUN_START, sources, "chat")

    assert verdict.status == "passed", verdict.failed
    assert [item.id for item in verdict.criteria] == ["P-A41", "P-A42", "C-cap"]
    a41_text, a41_span = query.ran[0]
    assert f"p95 > {A41_THRESHOLD_MS}" in a41_text
    assert a41_span == "2026-11-03T10:00:00.000Z/2026-11-03T10:10:10.000Z"
    assert f"meanMs > {A42_THRESHOLD_MS}" in query.ran[1][0]


@pytest.mark.parametrize(
    ("a41", "a42", "cap", "failing"),
    [
        ([], [{"call": "memory"}], 0, "P-A41"),
        ([{"deployment": "chat-small"}], [{"call": "memory"}], 0, "P-A41"),
        ([{"deployment": "chat"}], [{"call": "rewrite"}], 0, "P-A42"),
        ([{"deployment": "chat"}], [{"call": "memory"}], 2, "C-cap"),
    ],
)
async def test_the_check_phase_fails_without_its_rows_or_on_a_cap(
    tmp_path: Path, a41: Rows, a42: Rows, cap: int, failing: str
) -> None:
    write_window(tmp_path)
    clock = FakeClock(datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    sources = Sources(
        query=probe_queries(a41, a42, cap), azure=NoAz(), sleep=clock.sleep, now=clock.now
    )
    verdict = await check(tmp_path, RUN_START, sources, "chat")
    assert [item.id for item in verdict.failed] == [failing]


def test_the_chat_deployment_comes_from_the_load_environment_file(tmp_path: Path) -> None:
    assert chat_deployment() == "chat"
    broken = tmp_path / "load.json"
    broken.write_text(json.dumps({"openAi": {}}), "utf-8")
    with pytest.raises(UsageError):
        chat_deployment(broken)
