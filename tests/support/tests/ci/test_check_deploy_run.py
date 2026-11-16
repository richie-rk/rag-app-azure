"""T-533: `check_deploy_run.py` on recorded workflow-run answers (84-testing.md section 7.4)."""

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

import check_deploy_run  # noqa: E402

# The fields GET /repos/{owner}/{repo}/actions/runs/{run_id} returns that the check reads.
RUN = {
    "id": 4242,
    "name": "deploy",
    "path": ".github/workflows/deploy.yaml",
    "head_branch": "main",
    "head_sha": "0123456789abcdef0123456789abcdef01234567",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
}


@pytest.fixture(autouse=True)
def summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/docchat")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    return path


def _recorded(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> list[str]:
    requested: list[str] = []

    def github_get(path: str) -> Any:
        requested.append(path)
        return answer

    monkeypatch.setattr(check_deploy_run, "github_get", github_get)
    return requested


def _error(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1
    result: dict[str, str] = json.loads(lines[0])
    return result


def test_t533_a_successful_main_run_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = _recorded(monkeypatch, RUN)

    assert check_deploy_run.main(["4242"]) == 0
    assert requested == ["/repos/octo/docchat/actions/runs/4242"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", ".github/workflows/ci.yaml"),
        ("head_branch", "feature/load"),
        ("conclusion", "failure"),
        ("conclusion", None),
    ],
)
def test_t533_a_run_that_differs_fails_with_deploy_run_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    summary: Path,
    field: str,
    value: str | None,
) -> None:
    _recorded(monkeypatch, {**RUN, field: value})

    assert check_deploy_run.main(["4242"]) == 2
    error = _error(capsys)
    assert error["code"] == "deploy_run_invalid"
    assert f"{field} is {value!r}" in error["detail"]
    assert "deploy_run_invalid" in summary.read_text(encoding="utf-8")


def test_t533_an_id_that_is_not_a_run_id_fails_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = _recorded(monkeypatch, RUN)

    assert check_deploy_run.main(["42; rm -rf /"]) == 2
    assert requested == []
    assert _error(capsys)["code"] == "deploy_run_invalid"


def test_a_run_the_api_cannot_find_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def not_found(path: str) -> Any:
        raise check_deploy_run.GitHubApiError(path, 404, "Not Found")

    monkeypatch.setattr(check_deploy_run, "github_get", not_found)

    assert check_deploy_run.main(["4242"]) == 2
    assert "couldn't be read" in _error(capsys)["detail"]
