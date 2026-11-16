"""Check the `deploy_run_id` input of `load-test.yaml` (84-testing.md section 7.4, ADR 0126).

Job `manifest` runs `python scripts/ci/check_deploy_run.py <deploy_run_id>` before anything is
created. It reads the run through the GitHub REST API
(`GET /repos/{owner}/{repo}/actions/runs/{id}`, token from `GITHUB_TOKEN` or `GH_TOKEN`,
repository from `GITHUB_REPOSITORY`) and fails with
`deploy_run_invalid` unless its `path` is `.github/workflows/deploy.yaml`, its `head_branch` is
`main` and its `conclusion` is `success`, so a load run only ever tests digests that `deploy.yaml`
built and verified on `main`.

A failure prints one JSON line `{"code", "message", "detail"}` to standard error, writes the same
to the job summary and exits 2, the convention of 83-infrastructure.md section 10.18.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CODE = "deploy_run_invalid"
MESSAGE = "The deploy run isn't a successful deploy.yaml run on main; nothing was created."
EXPECTED = {"path": ".github/workflows/deploy.yaml", "head_branch": "main", "conclusion": "success"}
DEFAULT_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
EXIT_INVALID = 2


class GitHubApiError(RuntimeError):
    """A GitHub REST call failed; `status` is the HTTP status, or None without an answer."""

    def __init__(self, path: str, status: int | None, reason: str) -> None:
        super().__init__(f"GET {path} failed: {status or reason}")
        self.status = status


def github_get(path: str) -> Any:
    """GET a GitHub REST path with the workflow token and return the parsed JSON body."""
    api = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/")
    if not api.startswith("https://"):
        raise GitHubApiError(path, None, "GITHUB_API_URL must be an https URL")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # The host comes from GITHUB_API_URL, checked above to be https.
    request = urllib.request.Request(f"{api}{path}", headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise GitHubApiError(path, error.code, str(error.reason)) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise GitHubApiError(path, None, str(error)) from error


def mismatches(run: Mapping[str, Any]) -> list[str]:
    """Name each field of the run that differs from a successful deploy.yaml run on main."""
    return [
        f"{field} is {run.get(field)!r}, not {expected!r}"
        for field, expected in EXPECTED.items()
        if run.get(field) != expected
    ]


def fail(detail: str) -> int:
    line = json.dumps({"code": CODE, "message": MESSAGE, "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(f"```json\n{line}\n```\n")
    return EXIT_INVALID


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the deploy run a load test uses.")
    parser.add_argument("deploy_run_id")
    args = parser.parse_args(argv)
    run_id: str = args.deploy_run_id.strip()
    if not run_id.isdigit():
        return fail(f"deploy_run_id {args.deploy_run_id!r} is not a workflow run ID")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if repository.count("/") != 1:
        return fail("GITHUB_REPOSITORY must name the repository as <owner>/<repo>")
    try:
        run = github_get(f"/repos/{repository}/actions/runs/{run_id}")
    except GitHubApiError as error:
        return fail(f"run {run_id} couldn't be read: {error}")
    if not isinstance(run, dict):
        return fail(f"run {run_id}: the API answer isn't a workflow run")
    problems = mismatches(run)
    if problems:
        return fail(f"run {run_id}: {'; '.join(problems)}")
    sys.stdout.write(
        f"run {run_id} is a successful deploy.yaml run on main for commit {run.get('head_sha')}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
