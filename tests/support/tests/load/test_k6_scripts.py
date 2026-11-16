"""The k6 scripts, their lock and the load questions (`84-testing.md` sections 1.5, 7.1 and 7.6;
T-581).

Without k6 the tests read the files: each scenario imports only `./lib.js`, only the library prints,
and the lock is the one section 7.1 pins. With k6 (on `PATH`, or the binary named by
`DOCCHAT_K6`), `k6 inspect` reads each scenario's options, which must be section 7.6's arrival
profiles and thresholds, and the library self-test runs against
`scripts/ci/k6_selftest_server.py`, as the lint job runs both from the locked k6.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
K6_DIRECTORY = REPOSITORY_ROOT / "tests" / "load" / "k6"
LOCK = REPOSITORY_ROOT / "tests" / "load" / "k6.lock.json"
QUESTIONS = REPOSITORY_ROOT / "tests" / "fixtures" / "questions" / "load.json"
TESTING_SPEC = REPOSITORY_ROOT / "docs" / "spec" / "84-testing.md"
SELFTEST_SERVER = REPOSITORY_ROOT / "scripts" / "ci" / "k6_selftest_server.py"
SCENARIOS = ("s1-event-loop", "s2-stateless", "s3-calibrate", "s3-scaleout")
IMPORT = re.compile(r"^import\s.*?from\s+'([^']+)';", re.MULTILINE | re.DOTALL)
K6_MODULES = {"k6", "k6/http", "k6/execution", "k6/metrics"}
LONGEST_SUFFIX = len(" [[stub:first_ms=12000@stream]] [[stub:first_ms=9000@json]]")


def script(name: str) -> str:
    return (K6_DIRECTORY / f"{name}.js").read_text(encoding="utf-8")


def k6_binary() -> str:
    found = os.environ.get("DOCCHAT_K6") or shutil.which("k6")
    if not found:
        pytest.skip("k6 isn't installed on this machine; the lint job installs it from the lock")
    return found


def inspect(name: str) -> dict[str, Any]:
    done = subprocess.run(  # noqa: S603 - the locked k6 binary on a repository script
        [k6_binary(), "inspect", str(K6_DIRECTORY / f"{name}.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )
    assert done.returncode == 0, done.stderr
    options: dict[str, Any] = json.loads(done.stdout)
    return options


@pytest.mark.parametrize("name", [*SCENARIOS, "selftest"])
def test_each_script_imports_only_the_library(name: str) -> None:
    assert set(IMPORT.findall(script(name))) <= {"./lib.js", *K6_MODULES}
    if name in SCENARIOS:
        assert set(IMPORT.findall(script(name))) == {"./lib.js"}


def test_the_library_imports_only_k6_and_prints_only_markers() -> None:
    library = script("lib")
    assert set(IMPORT.findall(library)) <= K6_MODULES
    assert library.count("console.log(") == 1
    assert "console.log(`${name} ${JSON.stringify(payload)}`)" in library
    for name in SCENARIOS:
        assert "console." not in script(name)
    for marker in ("K6_SUMMARY_JSON", "K6_FAILED_REQUEST", "K6_SESSION"):
        assert marker in library
    # A marker carries trace IDs and times, never the request's headers or token.
    failure = library.split("export function reportFailure")[1].split("\n}\n")[0]
    assert "Authorization" not in failure
    assert "token" not in failure


def test_the_lock_is_the_release_section_7_1_pins() -> None:
    pinned = re.search(
        r"pinned in `tests/load/k6\.lock\.json`: `(\{.*?\})`", TESTING_SPEC.read_text("utf-8")
    )
    assert pinned is not None
    assert json.loads(LOCK.read_text(encoding="utf-8")) == json.loads(pinned.group(1))


def test_the_load_questions_fit_a_turn_with_any_directive() -> None:
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    assert isinstance(questions, list)
    assert len(questions) >= 10
    assert len(set(questions)) == len(questions)
    assert all(isinstance(q, str) and 0 < len(q) <= 4000 - LONGEST_SUFFIX for q in questions)


def test_s1_has_the_arrival_profile_and_thresholds_of_section_7_6() -> None:
    options = inspect("s1-event-loop")
    streams, cheap, sessions = (
        options["scenarios"][key] for key in ("streams", "cheap", "sessions")
    )
    assert streams["executor"] == "ramping-vus"
    assert streams["startVUs"] == 0
    assert streams["stages"] == [
        {"duration": "1m0s", "target": 100},
        {"duration": "9m0s", "target": 100},
    ]
    assert streams["gracefulRampDown"] == "30s"
    assert (cheap["executor"], cheap["rate"], cheap["timeUnit"]) == (
        "constant-arrival-rate",
        20,
        "1s",
    )
    assert (cheap["startTime"], cheap["duration"]) == ("1m0s", "9m0s")
    assert (cheap["preAllocatedVUs"], cheap["maxVUs"]) == (20, 60)
    assert (sessions["rate"], sessions["startTime"], sessions["duration"]) == (1, "2m0s", "7m0s")
    assert options["thresholds"] == {
        "http_req_failed{scenario:streams}": ["rate==0"],
        "checks{scenario:streams}": ["rate==1"],
        "http_req_duration{scenario:cheap}": ["p(95)<=200"],
        "http_req_failed{scenario:cheap}": ["rate==0"],
        "http_req_duration{scenario:sessions}": ["p(95)<=200"],
        "http_req_failed{scenario:sessions}": ["rate==0"],
    }


def test_s2_ramps_to_60_streams_with_10_cheap_requests_a_second() -> None:
    options = inspect("s2-stateless")
    streams, cheap = options["scenarios"]["streams"], options["scenarios"]["cheap"]
    assert streams["stages"] == [
        {"duration": "1m0s", "target": 60},
        {"duration": "8m0s", "target": 60},
    ]
    assert (cheap["rate"], cheap["duration"]) == (10, "9m0s")
    assert not options.get("thresholds")


def test_s3_calibrates_at_5_turns_and_runs_its_main_phase_for_15_minutes() -> None:
    calibration = inspect("s3-calibrate")["scenarios"]
    assert (calibration["turns"]["rate"], calibration["turns"]["duration"]) == (5, "4m0s")
    assert (calibration["turns"]["preAllocatedVUs"], calibration["turns"]["maxVUs"]) == (10, 30)
    assert (calibration["cheap"]["rate"], calibration["cheap"]["duration"]) == (10, "4m0s")
    main = inspect("s3-scaleout")
    assert main["scenarios"]["turns"]["duration"] == "15m0s"
    assert (main["scenarios"]["cheap"]["rate"], main["scenarios"]["cheap"]["duration"]) == (
        20,
        "15m0s",
    )
    assert main["thresholds"] == {
        "http_req_duration{scenario:cheap}": ["p(95)<=200"],
        "http_req_failed{scenario:cheap}": ["rate==0"],
        "http_req_failed{scenario:turns}": ["rate==0"],
    }


def test_t581_the_library_retries_only_the_two_limit_codes_once() -> None:
    binary = k6_binary()
    server = subprocess.Popen(  # noqa: S603 - the repository's own self-test server
        [sys.executable, str(SELFTEST_SERVER)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert server.stdout is not None
        ready = server.stdout.readline().split()
        assert ready[0] == "K6_SELFTEST_SERVER_READY"
        done = subprocess.run(  # noqa: S603 - the locked k6 binary on the self-test
            [binary, "run", "--quiet", str(K6_DIRECTORY / "selftest.js")],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "K6_SELFTEST_URL": ready[1],
            },
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
        if server.stdout is not None:
            server.stdout.close()
    assert done.returncode == 0, done.stdout + done.stderr
    summary_line = next(
        line for line in done.stdout.splitlines() if line.startswith("K6_SUMMARY_JSON ")
    )
    summary = json.loads(summary_line.split(" ", 1)[1])
    assert summary["limitRetries"] == 3
    assert all(all(outcomes.values()) for outcomes in summary["thresholds"].values())
