"""The event-loop guard: its parts, and T-561, the loop guard meta-test (`84-testing.md` section
2.8), which runs nested sessions with pytester and watches their tests fail."""

from __future__ import annotations

import asyncio
import gc
import logging
import shlex
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from docchat_testing.loopguard import (
    SLOW_CALLBACK_SECONDS,
    BlockedLoopRecords,
    describe,
    failure_message,
    guarded_event_loop,
)

pytest_plugins = ("pytester",)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _repository_ini() -> str:
    """The repository's own pytest configuration, so nested sessions run as real ones do."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options: dict[str, Any] = pyproject["tool"]["pytest"]["ini_options"]
    lines = ["[pytest]"]
    for key, value in options.items():
        if key == "testpaths":
            continue
        if key == "addopts":
            lines.append("addopts = " + " ".join(shlex.quote(option) for option in value))
        elif isinstance(value, list):
            lines.append(f"{key} =")
            lines.extend(f"    {entry}" for entry in value)
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _record(message: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("asyncio", level, __file__, 1, message, (), None)


# The parts


def test_the_guarded_loop_reports_callbacks_over_100_ms() -> None:
    loop = guarded_event_loop()
    try:
        assert loop.slow_callback_duration == SLOW_CALLBACK_SECONDS == 0.1
    finally:
        loop.close()


def test_a_slow_callback_record_becomes_the_guard_line() -> None:
    line = describe("Executing <Handle sleepy() at x.py:3> took 0.151 seconds")

    assert line == "Event loop blocked > 100 ms: <Handle sleepy() at x.py:3> took 0.151 s"


def test_the_task_records_are_kept_as_they_are() -> None:
    destroyed = "Task was destroyed but it is pending!\ntask: <Task pending name='Task-1'>"
    lost = "Task exception was never retrieved\nfuture: <Task finished name='Task-2'>"

    assert describe(destroyed) == destroyed
    assert describe(lost) == lost


def test_other_asyncio_records_are_ignored() -> None:
    assert describe("Using selector: EpollSelector") is None
    assert describe("poll 999.000 ms took 1000.123 ms: timeout") is None


def test_the_handler_collects_every_occurrence_in_order() -> None:
    handler = BlockedLoopRecords()

    handler.emit(_record("Executing <Task a> took 0.200 seconds"))
    handler.emit(_record("Using selector: EpollSelector", logging.DEBUG))
    handler.emit(_record("Task was destroyed but it is pending!", logging.ERROR))
    handler.emit(_record("Executing <Task b> took 0.300 seconds"))

    assert handler.occurrences == [
        "Event loop blocked > 100 ms: <Task a> took 0.200 s",
        "Task was destroyed but it is pending!",
        "Event loop blocked > 100 ms: <Task b> took 0.300 s",
    ]
    assert failure_message(handler.occurrences).splitlines() == handler.occurrences


def test_only_the_debug_warning_counts_as_a_slow_callback() -> None:
    handler = BlockedLoopRecords()

    handler.emit(_record("Executing <Task a> took 0.200 seconds", logging.INFO))

    assert handler.occurrences == []


@pytest.mark.allow_blocking(reason="the guard's own meta-test: proves the marker exempts a test")
async def test_the_marker_exempts_one_test() -> None:
    time.sleep(0.15)  # noqa: ASYNC251 - the exempted blocking call itself


# T-561

_GUARD_CASES = """
import asyncio
import gc
import time

import pytest


async def test_sleep_150_ms() -> None:
    time.sleep(0.15)


async def test_sleep_50_ms() -> None:
    time.sleep(0.05)


async def test_await_sleep_half_a_second() -> None:
    await asyncio.sleep(0.5)


async def test_task_destroyed_while_pending() -> None:
    loop = asyncio.get_running_loop()
    never = loop.create_future()

    async def wait() -> None:
        await never

    task = loop.create_task(wait())
    await asyncio.sleep(0)
    del task, never
    gc.collect()


@pytest.mark.allow_blocking(reason="exempt in the guard's own directory")
async def test_exempt_here() -> None:
    time.sleep(0.15)
"""

_OUTSIDE = """
import pytest


@pytest.mark.allow_blocking(reason="not the guard's directory")
def test_marked_outside() -> None:
    pass
"""


@pytest.fixture
def guard_session(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(_repository_ini())
    pytester.mkdir("tests")
    (pytester.mkdir("tests/support") / "tests").mkdir()
    (pytester.path / "tests" / "support" / "tests" / "test_guard_cases.py").write_text(
        _GUARD_CASES, encoding="utf-8"
    )
    pytester.mkdir("tests/elsewhere")
    (pytester.path / "tests" / "elsewhere" / "test_outside.py").write_text(
        _OUTSIDE, encoding="utf-8"
    )
    return pytester


def _outcomes(reports: list[pytest.TestReport]) -> dict[str, set[str]]:
    outcomes: dict[str, set[str]] = {}
    for report in reports:
        name = report.nodeid.rsplit("::", 1)[-1]
        if report.failed:
            outcomes.setdefault(name, set()).add(f"{report.when} failed")
        elif report.when == "call" and report.passed:
            outcomes.setdefault(name, set()).add("passed")
    return outcomes


def _longrepr(reports: list[pytest.TestReport], name: str) -> str:
    return "\n".join(str(r.longrepr) for r in reports if r.failed and r.nodeid.endswith(name))


@pytest.mark.allow_blocking(reason="watches the nested session's tests block the shared loop")
def test_t561_the_loop_guard_fails_blocking_tests_and_passes_the_others(
    guard_session: pytest.Pytester,
) -> None:
    recorder = guard_session.inline_run()
    reports = recorder.getreports("pytest_runtest_logreport")
    outcomes = _outcomes(list(reports))

    assert outcomes["test_sleep_150_ms"] == {"passed", "teardown failed"}
    assert outcomes["test_sleep_50_ms"] == {"passed"}
    assert outcomes["test_await_sleep_half_a_second"] == {"passed"}
    assert outcomes["test_task_destroyed_while_pending"] == {"passed", "teardown failed"}
    assert outcomes["test_exempt_here"] == {"passed"}
    assert outcomes["test_marked_outside"] == {"setup failed"}
    assert "Event loop blocked > 100 ms: " in _longrepr(list(reports), "test_sleep_150_ms")
    assert " took 0.1" in _longrepr(list(reports), "test_sleep_150_ms")
    assert "Task was destroyed but it is pending" in _longrepr(
        list(reports), "test_task_destroyed_while_pending"
    )
    assert "allowed only in tests/support/tests" in _longrepr(list(reports), "test_marked_outside")


@pytest.mark.allow_blocking(reason="watches the nested session's test block the shared loop")
def test_t561_allow_blocking_needs_a_reason(pytester: pytest.Pytester) -> None:
    pytester.makeini(_repository_ini())
    directory = pytester.path / "tests" / "support" / "tests"
    directory.mkdir(parents=True)
    (directory / "test_no_reason.py").write_text(
        "import pytest\n\n\n@pytest.mark.allow_blocking\ndef test_bare() -> None:\n    pass\n",
        encoding="utf-8",
    )

    recorder = pytester.inline_run()

    (failed,) = [r for r in recorder.getreports("pytest_runtest_logreport") if r.failed]
    assert failed.when == "setup"
    assert "allow_blocking needs a reason" in str(failed.longrepr)


@pytest.mark.allow_blocking(reason="provokes the record the guard fails a test for")
async def test_the_handler_sees_a_task_exception_nobody_read() -> None:
    handler = BlockedLoopRecords()
    logger = logging.getLogger("asyncio")
    logger.addHandler(handler)
    try:

        async def fail() -> None:
            raise LookupError

        task = asyncio.get_running_loop().create_task(fail())
        await asyncio.sleep(0)
        del task
        gc.collect()
    finally:
        logger.removeHandler(handler)

    (occurrence,) = handler.occurrences
    assert occurrence.startswith("Task exception was never retrieved")
