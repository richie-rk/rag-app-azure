"""`docchat-load probe-alerts` (`84-testing.md` sections 7.5 and 7.6; `85-observability.md`
section 6.5; T-596).

`--phase send`, before S3's setup: as `docchat-load-01`, 25 `createChatTurn` requests one after
another in one new session, each a load question followed by
`[[stub:first_ms=12000@stream]] [[stub:first_ms=9000@json]]`. Each answer's first chunk arrives
after 12 s (A41's signal) and each structured call waits 9 s, so the memory extractions average
about 9,000 ms (A42's signal). It records the window from the first request to 60 s after the
last `done` in `window.json`.

`--phase check`, after S3's verdict: A41's and A42's queries over the recorded window with the
thresholds 10,000 ms and 8,000 ms. P-A41 passes when A41 returns a row for the chat deployment,
P-A42 when A42 returns a row with `call` `memory`; with `C-cap`, into `verdict.json`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.load import queries
from docchat_testing.load.chat import find_project, send_turn
from docchat_testing.load.common import (
    SESSION_USER,
    UsageError,
    iso,
    parse_time,
    read_json,
    token_of,
    utc_now,
    write_json,
)
from docchat_testing.load.verdict import Sources, Verdict, c_cap
from docchat_testing.tokens import REPOSITORY_ROOT

TURNS: Final = 25
DIRECTIVES: Final = "[[stub:first_ms=12000@stream]] [[stub:first_ms=9000@json]]"
SETTLE: Final = timedelta(seconds=60)
A41_THRESHOLD_MS: Final = 10_000
A42_THRESHOLD_MS: Final = 8_000
LOAD_ENV_FILE: Final = REPOSITORY_ROOT / "infra" / "env" / "load.json"
WINDOW_FILE: Final = "window.json"


def chat_deployment(path: Path = LOAD_ENV_FILE) -> str:
    """The chat deployment's name in `load`, which A41 groups by."""
    config = read_json(path, "infra/env/load.json")
    try:
        return str(config["openAi"]["deployments"]["chat"]["name"])
    except (KeyError, TypeError) as error:
        raise UsageError("load_config_invalid", f"{path} names no chat deployment") from error


async def send(
    api_base_url: str,
    tokens: Mapping[str, str],
    questions: Sequence[str],
    out: Path,
    *,
    transport: Any = None,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """The send phase; returns the window it wrote."""
    token = token_of(tokens, SESSION_USER)

    async def bearer() -> str:
        return token

    outcomes: list[dict[str, Any]] = []
    session_id: str | None = None
    async with DeployedApiClient(api_base_url, bearer, transport=transport) as client:
        project_id = await find_project(client)
        start = now()
        last_done = start
        for number in range(TURNS):
            question = f"{questions[number % len(questions)]} {DIRECTIVES}"
            result = await send_turn(
                client, project_id=project_id, question=question, session_id=session_id
            )
            session_id = session_id or result.session_id
            if result.done:
                last_done = now()
            outcomes.append({"done": result.done, "status": result.status, "reason": result.reason})
    window = {
        "start": iso(start),
        "end": iso(last_done + SETTLE),
        "sessionId": session_id,
        "turns": len(outcomes),
        "done": sum(1 for outcome in outcomes if outcome["done"]),
        "outcomes": outcomes,
    }
    write_json(out / WINDOW_FILE, window)
    return window


async def check(out: Path, run_start: datetime, sources: Sources, deployment: str) -> Verdict:
    """The check phase."""
    window = read_json(out / WINDOW_FILE, "the alert probe's window")
    start, end = parse_time(str(window["start"])), parse_time(str(window["end"]))
    verdict = Verdict("alert-probe", start, end)
    verdict.measurements["turnsDone"] = window.get("done")
    span = f"{iso(start)}/{iso(end)}"
    a41_rows = await sources.query(queries.a41(A41_THRESHOLD_MS), span)
    a42_rows = await sources.query(queries.a42(A42_THRESHOLD_MS), span)
    verdict.measurements["a41"] = a41_rows
    verdict.measurements["a42"] = a42_rows
    verdict.add(
        "P-A41",
        any(row.get("deployment") == deployment for row in a41_rows),
        f"A41 rows for {sorted(str(row.get('deployment')) for row in a41_rows)}",
    )
    verdict.add(
        "P-A42",
        any(row.get("call") == "memory" for row in a42_rows),
        f"A42 rows for calls {sorted(str(row.get('call')) for row in a42_rows)}",
    )
    await c_cap(verdict, sources, run_start)
    return verdict
