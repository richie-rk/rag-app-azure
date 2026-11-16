"""T-580: `check_k6_lock.py` against a recorded checksums file (84-testing.md section 7.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_k6_lock  # noqa: E402

SHA256 = "39c3117b6af817592dcd0ce4242105c0a7af10948c2a425306f0be8f7a8a8ab1"
OTHER = "0" * 64
LOCK = {
    "version": "2.3.0",
    "url": "https://github.com/grafana/k6/releases/download/v2.3.0/k6-v2.3.0-linux-amd64.tar.gz",
    "sha256": SHA256,
    "member": "k6-v2.3.0-linux-amd64/k6",
}
# The shape of a k6 release's k6-v<version>-checksums.txt: `sha256sum` lines, one per artifact.
CHECKSUMS = f"""\
{"1" * 64}  k6-v2.3.0-linux-arm64.tar.gz
{SHA256}  k6-v2.3.0-linux-amd64.tar.gz
{"2" * 64}  k6-v2.3.0-macos-arm64.zip
{"3" * 64}  k6-v2.3.0-windows-amd64.zip
"""
CHECKSUMS_URL = "https://github.com/grafana/k6/releases/download/v2.3.0/k6-v2.3.0-checksums.txt"


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: dict[str, Any], checksums: str
) -> tuple[int, list[str]]:
    requested: list[str] = []

    def recorded(url: str) -> str:
        requested.append(url)
        return checksums

    monkeypatch.setattr(check_k6_lock, "fetch_checksums", recorded)
    path = tmp_path / "k6.lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    return check_k6_lock.main(["--lock", str(path)]), requested


def test_t580_a_lock_matching_its_checksums_file_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, requested = _run(tmp_path, monkeypatch, LOCK, CHECKSUMS)

    assert code == 0
    assert requested == [CHECKSUMS_URL]


def test_t580_a_sha256_that_differs_from_the_checksums_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(tmp_path, monkeypatch, {**LOCK, "sha256": OTHER}, CHECKSUMS)

    assert code == 1
    assert f"sha256 {OTHER} differs from {SHA256}" in capsys.readouterr().out


def test_t580_a_checksums_file_without_the_tarball_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(tmp_path, monkeypatch, LOCK, f"{SHA256}  k6-v2.3.0-linux-arm64.tar.gz\n")

    assert code == 1
    assert "lists no k6-v2.3.0-linux-amd64.tar.gz" in capsys.readouterr().out


@pytest.mark.parametrize(
    "case",
    [
        ("url", "https://example.com/k6.tar.gz", "url is 'https://example.com/k6.tar.gz'"),
        ("member", "k6", "member is 'k6'"),
        ("version", "latest", "version 'latest' is not a release version"),
        ("sha256", SHA256.upper(), "sha256 must be 64 lower-case hexadecimal characters"),
    ],
)
def test_a_lock_that_its_version_does_not_explain_fails_before_any_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str, str],
) -> None:
    field, value, message = case
    code, requested = _run(tmp_path, monkeypatch, {**LOCK, field: value}, CHECKSUMS)

    assert code == 1
    assert requested == []
    assert message in capsys.readouterr().out


def test_an_unreachable_checksums_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreachable(url: str) -> str:
        raise check_k6_lock.ChecksumsUnavailableError(f"{url}: timed out")

    monkeypatch.setattr(check_k6_lock, "fetch_checksums", unreachable)
    path = tmp_path / "k6.lock.json"
    path.write_text(json.dumps(LOCK), encoding="utf-8")

    assert check_k6_lock.main(["--lock", str(path)]) == 1
    assert "can't read the release checksums file" in capsys.readouterr().out


def test_fetching_refuses_a_url_outside_the_k6_releases() -> None:
    with pytest.raises(check_k6_lock.ChecksumsUnavailableError):
        check_k6_lock.fetch_checksums("https://example.com/k6-v2.3.0-checksums.txt")


def test_a_missing_lock_fails(tmp_path: Path) -> None:
    assert check_k6_lock.main(["--lock", str(tmp_path / "absent.json")]) == 1
