from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from docchat_api.http.state import (
    OPS_STATE,
    PROCESS_STATE,
    ApiProcess,
    ProcessNotStartedError,
    current_process,
    request_ops,
)
from docchat_core.opsconfig import OpsConfig
from docchat_testing.fixtures.api import ApiUnderTest


def _request(app_state: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=app_state), state=SimpleNamespace())


def test_current_process_refuses_before_start_up() -> None:
    with pytest.raises(ProcessNotStartedError):
        current_process(_request(SimpleNamespace()))


async def test_the_process_is_kept_on_the_app(started: ApiUnderTest) -> None:
    process = getattr(started.app.state, PROCESS_STATE)

    assert isinstance(process, ApiProcess)
    assert current_process(_request(started.app.state)) is process


async def test_one_snapshot_per_request(started: ApiUnderTest, fake_sql: Any) -> None:
    request = _request(started.app.state)

    first = await request_ops(request)
    second = await request_ops(request)

    assert isinstance(first, OpsConfig)
    assert first is second
    assert getattr(request.state, OPS_STATE) is first
    assert first.api.write_requests_per_minute == 120
