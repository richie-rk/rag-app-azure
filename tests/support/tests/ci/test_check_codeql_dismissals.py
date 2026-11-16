"""T-528: `check_codeql_dismissals.py` on recorded API answers (84-testing.md section 9.4)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_codeql_dismissals  # noqa: E402

APPROVERS = "approvers: [sec-lead, sec-two]\n"


def alert(
    number: int, severity: str | None, reason: str | None, login: str = "sec-lead"
) -> dict[str, Any]:
    """One entry of GET /repos/{owner}/{repo}/code-scanning/alerts?state=dismissed."""
    return {
        "number": number,
        "state": "dismissed",
        "dismissed_reason": reason,
        "dismissed_by": {"login": login},
        "dismissed_at": "2026-10-29T10:00:00Z",
        "html_url": f"https://github.com/octo/docchat/security/code-scanning/{number}",
        "rule": {
            "id": "py/sql-injection",
            "severity": "error",
            "security_severity_level": severity,
        },
    }


class RecordedGitHub:
    """Serves recorded list pages and records every write."""

    def __init__(self, alerts: list[Any], issues: list[Any] | None = None) -> None:
        self.pages = {"code-scanning/alerts": alerts, "issues": issues or []}
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method != "GET":
            self.writes.append((method, path, body))
            return {}
        page = int(path.rsplit("page=", 1)[1])
        for key, items in self.pages.items():
            if f"/{key}?" in path:
                return items[(page - 1) * 100 : page * 100]
        raise AssertionError(path)


@pytest.fixture
def approvers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/docchat")
    path = tmp_path / "approvers.yaml"
    path.write_text(APPROVERS, encoding="utf-8")
    return path


def _run(monkeypatch: pytest.MonkeyPatch, approvers: Path, github: RecordedGitHub) -> int:
    monkeypatch.setattr(check_codeql_dismissals, "github_request", github)
    return check_codeql_dismissals.main(["--approvers", str(approvers)])


@pytest.mark.parametrize("reason", ["won't fix", "used in tests", "mitigated"])
@pytest.mark.parametrize("severity", ["high", "critical"])
def test_t528_a_high_alert_dismissed_for_another_reason_fails(
    monkeypatch: pytest.MonkeyPatch,
    approvers: Path,
    capsys: pytest.CaptureFixture[str],
    reason: str,
    severity: str,
) -> None:
    github = RecordedGitHub([alert(7, severity, reason)])

    assert _run(monkeypatch, approvers, github) == 1
    assert f"dismissed as {reason!r}, not 'false positive'" in capsys.readouterr().out
    assert github.writes[0][0:2] == ("POST", "/repos/octo/docchat/issues")
    assert github.writes[0][2] is not None
    assert github.writes[0][2]["title"] == "CodeQL dismissal outside policy: alert 7"


def test_t528_a_false_positive_by_an_unlisted_handle_fails(
    monkeypatch: pytest.MonkeyPatch, approvers: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = RecordedGitHub([alert(8, "high", "false positive", login="dev-one")])

    assert _run(monkeypatch, approvers, github) == 1
    assert "dismissed by dev-one, not a listed approver" in capsys.readouterr().out


def test_t528_a_listed_approvers_false_positive_and_a_medium_alert_pass(
    monkeypatch: pytest.MonkeyPatch, approvers: Path
) -> None:
    github = RecordedGitHub(
        [
            alert(9, "high", "false positive", login="sec-two"),
            alert(10, "medium", "won't fix", login="dev-one"),
            alert(11, None, "used in tests", login="dev-one"),
        ]
    )

    assert _run(monkeypatch, approvers, github) == 0
    assert github.writes == []


def test_t528_an_existing_policy_issue_is_updated_and_reopened(
    monkeypatch: pytest.MonkeyPatch, approvers: Path
) -> None:
    issues = [
        {"number": 3, "title": "CodeQL dismissal outside policy: alert 12", "state": "closed"},
        {"number": 4, "title": "CodeQL dismissal outside policy: alert 12", "pull_request": {}},
    ]
    github = RecordedGitHub([alert(12, "critical", "mitigated")], issues)

    assert _run(monkeypatch, approvers, github) == 1
    method, path, body = github.writes[0]
    assert (method, path) == ("PATCH", "/repos/octo/docchat/issues/3")
    assert body is not None
    assert body["state"] == "open"
    assert "dismissed as 'mitigated'" in body["body"]


def test_every_page_of_dismissed_alerts_is_read(
    monkeypatch: pytest.MonkeyPatch, approvers: Path
) -> None:
    alerts = [alert(number, "low", "won't fix") for number in range(150)]
    alerts.append(alert(150, "high", "won't fix"))
    github = RecordedGitHub(alerts)

    assert _run(monkeypatch, approvers, github) == 1
    assert github.writes[0][2] is not None
    assert github.writes[0][2]["title"].endswith("alert 150")


def test_an_api_failure_fails_the_check(monkeypatch: pytest.MonkeyPatch, approvers: Path) -> None:
    def failing(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        raise check_codeql_dismissals.GitHubApiError(f"{method} {path}: HTTP 403")

    monkeypatch.setattr(check_codeql_dismissals, "github_request", failing)

    assert check_codeql_dismissals.main(["--approvers", str(approvers)]) == 1


def test_missing_approvers_fail_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/docchat")

    assert check_codeql_dismissals.main(["--approvers", str(tmp_path / "absent.yaml")]) == 1
