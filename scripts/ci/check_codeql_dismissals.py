"""Check every dismissed CodeQL alert against the dismissal policy (84-testing.md 9.4, ADR 0136).

A high or critical code-scanning alert may be dismissed only with the reason `false positive`, by
a handle listed in `.github/security/approvers.yaml`: a real high or critical finding has no
dismissal path. Run nightly by `nightly.yaml` job `codeql-dismissals` (`permissions {contents:
read, security-events: read, issues: write}`), the script lists every dismissed alert
(`GET /repos/{owner}/{repo}/code-scanning/alerts?state=dismissed&per_page=100`, all pages) and
fails when an alert whose `rule.security_severity_level` is `high` or `critical` has a
`dismissed_reason` other than `false positive` or a `dismissed_by.login` not in the approvers. For
each such alert it creates, or updates and reopens, the issue
`CodeQL dismissal outside policy: alert <number>`; a `docchat-security` member then reopens the
alert or confirms the dismissal by repeating it.

Environment: `GITHUB_REPOSITORY` (`<owner>/<repo>`), `GITHUB_TOKEN` or `GH_TOKEN`, and optionally
`GITHUB_API_URL`. Exit codes: 0 every dismissal follows the policy; 1 an alert breaks it, or the
alerts, the approvers or the issues can't be read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from security_gate import YamlSubsetError, load_approvers

if TYPE_CHECKING:
    from collections.abc import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
PER_PAGE = 100
MAX_PAGES = 100
POLICED_SEVERITIES = frozenset({"high", "critical"})
ALLOWED_REASON = "false positive"
ISSUE_TITLE = "CodeQL dismissal outside policy: alert {number}"


class GitHubApiError(RuntimeError):
    """A GitHub REST call failed."""


def github_request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Send one GitHub REST request with the workflow token and return the parsed JSON body."""
    api = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/")
    if not api.startswith("https://"):
        raise GitHubApiError("GITHUB_API_URL must be an https URL")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    # The host comes from GITHUB_API_URL, checked above to be https.
    request = urllib.request.Request(  # noqa: S310
        f"{api}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise GitHubApiError(f"{method} {path}: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise GitHubApiError(f"{method} {path}: {error}") from error


def get_all(path: str) -> list[Any]:
    """GET every page of a list endpoint, page by page until a short page."""
    items: list[Any] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        batch = github_request("GET", f"{path}{separator}per_page={PER_PAGE}&page={page}")
        if not isinstance(batch, list):
            raise GitHubApiError(f"GET {path}: expected a list, page {page}")
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
    raise GitHubApiError(f"GET {path}: more than {MAX_PAGES} pages")


@dataclass(frozen=True)
class Breach:
    number: int
    rule: str
    severity: str
    reason: str
    dismissed_by: str
    dismissed_at: str
    url: str


def find_breaches(
    alerts: Sequence[Any], approvers: Sequence[str]
) -> list[tuple[Breach, list[str]]]:
    """Return each dismissed high or critical alert that breaks the policy, with its problems."""
    breaches: list[tuple[Breach, list[str]]] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        rule: dict[str, Any] = alert["rule"] if isinstance(alert.get("rule"), dict) else {}
        severity = str(rule.get("security_severity_level") or "").lower()
        if severity not in POLICED_SEVERITIES:
            continue
        dismissed_by = alert.get("dismissed_by")
        login = str(dismissed_by.get("login", "")) if isinstance(dismissed_by, dict) else ""
        breach = Breach(
            number=int(alert.get("number", 0)),
            rule=str(rule.get("id", "")),
            severity=severity,
            reason=str(alert.get("dismissed_reason") or ""),
            dismissed_by=login,
            dismissed_at=str(alert.get("dismissed_at") or ""),
            url=str(alert.get("html_url") or ""),
        )
        problems: list[str] = []
        if breach.reason != ALLOWED_REASON:
            problems.append(f"dismissed as {breach.reason!r}, not 'false positive'")
        if login not in approvers:
            problems.append(f"dismissed by {login or 'an unknown account'}, not a listed approver")
        if problems:
            breaches.append((breach, problems))
    return breaches


def issue_body(breach: Breach, problems: Sequence[str]) -> str:
    heading = (
        f"CodeQL alert {breach.number} ({breach.rule}, security severity {breach.severity}) "
        "was dismissed outside the policy of 84-testing.md section 9.4:"
    )
    procedure = (
        "A high or critical alert may be dismissed only as `false positive`, by a handle in "
        "`.github/security/approvers.yaml`. A `docchat-security` member reopens the alert, or "
        "confirms the dismissal by dismissing it again as `false positive`."
    )
    dismissed = f"Dismissed at {breach.dismissed_at or 'an unknown time'}: {breach.url}"
    return "\n".join(
        [heading, "", *(f"- {problem}" for problem in problems), "", dismissed, "", procedure]
    )


def record_issues(repository: str, breaches: Sequence[tuple[Breach, list[str]]]) -> None:
    """Create the policy issue of each breach, or update and reopen the existing one."""
    existing: dict[str, int] = {}
    for issue in get_all(f"/repos/{repository}/issues?state=all"):
        if isinstance(issue, dict) and "pull_request" not in issue:
            existing.setdefault(str(issue.get("title", "")), int(issue.get("number", 0)))
    for breach, problems in breaches:
        title = ISSUE_TITLE.format(number=breach.number)
        body = issue_body(breach, problems)
        if title in existing:
            github_request(
                "PATCH",
                f"/repos/{repository}/issues/{existing[title]}",
                {"body": body, "state": "open"},
            )
        else:
            github_request("POST", f"/repos/{repository}/issues", {"title": title, "body": body})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check dismissed CodeQL alerts.")
    parser.add_argument(
        "--approvers", type=Path, default=REPOSITORY_ROOT / ".github/security/approvers.yaml"
    )
    args = parser.parse_args(argv)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if repository.count("/") != 1:
        sys.stdout.write("::error::GITHUB_REPOSITORY must name the repository as <owner>/<repo>\n")
        return 1
    try:
        approvers = load_approvers(args.approvers)
    except (OSError, YamlSubsetError) as error:
        sys.stdout.write(f"::error::can't read {args.approvers.name}: {error}\n")
        return 1
    try:
        alerts = get_all(f"/repos/{repository}/code-scanning/alerts?state=dismissed")
        breaches = find_breaches(alerts, approvers)
        if breaches:
            record_issues(repository, breaches)
    except GitHubApiError as error:
        sys.stdout.write(f"::error::{error}\n")
        return 1
    for breach, problems in breaches:
        sys.stdout.write(
            f"::error::CodeQL alert {breach.number} ({breach.rule}, {breach.severity}): "
            f"{'; '.join(problems)}\n"
        )
    if breaches:
        return 1
    sys.stdout.write(
        f"{len(alerts)} dismissed alert(s); every high or critical dismissal follows the policy\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
