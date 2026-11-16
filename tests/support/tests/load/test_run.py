"""T-579, the load generator bootstrap (`tests/load/aci/run.py`; `84-testing.md` section 7.2).

The bootstrap always exits 0, prints `K6_EXIT <code>` after running the k6 double, prints
`K6_BOOTSTRAP_FAILED <reason>` for a download, checksum or archive failure, and writes nothing but
its one binary: no other archive member reaches the disk, whatever its path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import stat
import sys
import tarfile
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RUN_PY = REPOSITORY_ROOT / "tests" / "load" / "aci" / "run.py"
URL = "https://github.com/grafana/k6/releases/download/v2.3.0/k6-v2.3.0-linux-amd64.tar.gz"
MEMBER = "k6-v2.3.0-linux-amd64/k6"


def _load() -> ModuleType:
    # run.py belongs to no package (01-repository.md section 1.7), so it is loaded by path.
    spec = importlib.util.spec_from_file_location("loadgen_run", RUN_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run = _load()


def tarball(members: Mapping[str, bytes], directories: Sequence[str] = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def environment(data: bytes, *, sha256: str | None = None, member: str = MEMBER) -> dict[str, str]:
    return {
        "K6_URL": URL,
        "K6_SHA256": sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
        "K6_MEMBER": member,
        "DOCCHAT_LOAD_TOKENS_FILE": "/work/tokens.json",
    }


type Runner = Callable[[Sequence[str], Mapping[str, str]], int]


class Calls:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def runner(self, code: int) -> Runner:
        def record(command: Sequence[str], env: Mapping[str, str]) -> int:
            self.commands.append(list(command))
            self.environments.append(dict(env))
            return code

        return record


def files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_t579_the_k6_double_exiting_99_prints_k6_exit_99_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    double = b"#!/bin/sh\nexit 99\n"
    data = tarball({MEMBER: double})
    binary = tmp_path / "bin" / "k6"
    binary.parent.mkdir()

    code = run.main(environment(data), fetcher=lambda url: data, binary=binary)

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["K6_EXIT 99"]
    assert binary.read_bytes() == double
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755


def test_t579_k6_runs_the_scenario_quietly_with_raw_console_output(tmp_path: Path) -> None:
    data = tarball({MEMBER: b"binary"})
    calls = Calls()
    binary = tmp_path / "k6"

    run.main(environment(data), fetcher=lambda url: data, runner=calls.runner(0), binary=binary)

    assert calls.commands == [[str(binary), "run", "--quiet", "/work/scenario.js"]]
    passed = calls.environments[0]
    assert passed["K6_LOG_OUTPUT"] == "stdout"
    assert passed["K6_LOG_FORMAT"] == "raw"
    assert passed["DOCCHAT_LOAD_TOKENS_FILE"] == "/work/tokens.json"


def test_t579_a_tarball_whose_sha256_differs_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tarball({MEMBER: b"binary"})
    calls = Calls()

    code = run.main(
        environment(data, sha256="0" * 64),
        fetcher=lambda url: data,
        runner=calls.runner(0),
        binary=tmp_path / "k6",
    )

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["K6_BOOTSTRAP_FAILED checksum_mismatch"]
    assert calls.commands == []
    assert files_under(tmp_path) == []


@pytest.mark.parametrize(
    ("members", "directories"),
    [
        ({"k6-v2.3.0-linux-amd64/README.md": b"readme"}, ()),
        ({}, (MEMBER,)),
    ],
    ids=["absent", "a directory"],
)
def test_t579_a_tarball_without_the_member_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    members: dict[str, bytes],
    directories: tuple[str, ...],
) -> None:
    data = tarball(members, directories)

    code = run.main(environment(data), fetcher=lambda url: data, binary=tmp_path / "k6")

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["K6_BOOTSTRAP_FAILED member_missing"]
    assert files_under(tmp_path) == []


def test_t579_a_download_failing_three_times_15_s_apart_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = Calls()
    attempts: list[str] = []

    def failing(url: str) -> bytes:
        attempts.append(url)
        raise urllib.error.URLError("unreachable")

    code = run.main(environment(b""), fetcher=failing, sleep=calls.sleep, binary=tmp_path / "k6")

    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["K6_BOOTSTRAP_FAILED download_failed"]
    assert attempts == [URL, URL, URL]
    assert calls.sleeps == [15.0, 15.0]


def test_t579_a_download_succeeding_on_the_third_attempt_runs_k6(tmp_path: Path) -> None:
    data = tarball({MEMBER: b"binary"})
    outcomes: list[bytes | Exception] = [TimeoutError(), OSError("reset"), data]
    calls = Calls()

    def flaky(url: str) -> bytes:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run.main(
        environment(data),
        fetcher=flaky,
        sleep=calls.sleep,
        runner=calls.runner(0),
        binary=tmp_path / "k6",
    )

    assert len(calls.commands) == 1


def test_t579_only_the_release_host_is_downloaded_from(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(run.BootstrapError, match="download_failed"):
        run.fetch("https://example.com/k6.tar.gz")
    env = environment(b"") | {"K6_URL": "file:///etc/passwd"}

    assert run.main(env, sleep=lambda seconds: None) == 0
    assert capsys.readouterr().out.splitlines() == ["K6_BOOTSTRAP_FAILED download_failed"]


def test_t579_nothing_but_the_binary_is_written_whatever_the_archive_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tarball(
        {
            MEMBER: b"binary",
            "../escaped.txt": b"outside",
            "/tmp/absolute.txt": b"absolute",  # noqa: S108 - a hostile member path
            "k6-v2.3.0-linux-amd64/LICENSE": b"license",
        }
    )
    working = tmp_path / "work"
    working.mkdir()
    monkeypatch.chdir(working)
    binary = tmp_path / "out" / "k6"
    binary.parent.mkdir()

    run.main(environment(data), fetcher=lambda url: data, runner=Calls().runner(0), binary=binary)

    assert files_under(tmp_path) == [binary]
    assert not Path("/tmp/absolute.txt").exists()  # noqa: S108 - the member's own path


def test_t579_a_binary_that_cannot_start_is_reported_as_an_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tarball({MEMBER: b"not an executable format"})

    def refuse(command: Sequence[str], env: Mapping[str, str]) -> int:
        raise OSError("exec format error")

    assert (
        run.main(environment(data), fetcher=lambda url: data, runner=refuse, binary=tmp_path / "k6")
        == 0
    )
    assert capsys.readouterr().out.splitlines() == [f"K6_EXIT {run.EXIT_NOT_STARTED}"]


def test_t579_the_binary_replaces_an_earlier_file_and_is_written_at_tmp_k6(tmp_path: Path) -> None:
    binary = tmp_path / "k6"
    binary.write_bytes(b"old content that is longer")
    binary.chmod(0o600)

    run.write_binary(b"new", binary)

    assert binary.read_bytes() == b"new"
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    expected = Path("/tmp/k6")  # noqa: S108 - the spec's path
    assert expected == run.K6_PATH
