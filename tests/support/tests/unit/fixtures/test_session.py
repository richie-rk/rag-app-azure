from __future__ import annotations

import asyncio
import threading

import pytest

from docchat_testing.fixtures.session import (
    SESSION_ID_PATTERN,
    run_on_private_loop,
    session_id_of,
)


def test_session_id_is_eight_hex_and_stable(pytestconfig: pytest.Config) -> None:
    value = session_id_of(pytestconfig)
    assert SESSION_ID_PATTERN.fullmatch(value)
    assert session_id_of(pytestconfig) == value


def test_private_loop_runs_in_its_own_thread() -> None:
    caller = threading.get_ident()

    async def work() -> tuple[int, bool]:
        await asyncio.sleep(0)
        return threading.get_ident(), asyncio.get_running_loop().is_running()

    thread, running = run_on_private_loop(work)
    assert running
    assert thread != caller


def test_private_loop_passes_failures_on() -> None:
    async def fail() -> None:
        raise LookupError("emulator answered 503")

    with pytest.raises(LookupError, match="503"):
        run_on_private_loop(fail)


async def test_private_loop_works_beside_a_running_test_loop() -> None:
    async def work() -> str:
        return "done"

    # A session fixture is set up while a test's loop exists; the private loop leaves it alone.
    loop = asyncio.get_running_loop()
    assert run_on_private_loop(work) == "done"
    assert asyncio.get_running_loop() is loop


def test_private_loop_bounds_the_work_by_its_timeout() -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError):
        run_on_private_loop(slow, timeout=0.05)
