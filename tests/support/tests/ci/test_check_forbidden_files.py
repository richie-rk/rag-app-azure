"""T-516: `check_forbidden_files.py` against temporary repositories (01-repository.md 1.12)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_forbidden_files  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterable

FORBIDDEN = {
    "archives/source.zip": "*.zip",
    "image.tar": "*.tar",
    "web/pkg.tgz": "*.tgz",
    "logs/data.gz": "*.gz",
    "backup.7z": "*.7z",
    "old.rar": "*.rar",
    "lib/tool.jar": "*.jar",
    "wheels/pkg-1.0-py3-none-any.whl": "*.whl",
    "apps/web/assets/app.js.map": "*.map",
    "certs/site.pfx": "*.pfx",
    "certs/site.p12": "*.p12",
    "apps/web/dist/index.html": "**/dist/**",
    "build/audit/report.json": "**/build/**",
    "tests/e2e/node_modules/x/index.js": "**/node_modules/**",
    ".env": "a final component exactly .env",
    "apps/api/.env": "a final component exactly .env",
    ".azure/dev/.env.json": ".azure/**",
    "apps/web/config.local.json": "apps/web/config.local.json",
}
ALLOWED = [
    ".env.example",
    "docs/distribution.md",
    "builder/tool.py",
    "apps/web/config.example.json",
    "src/zipfile_helpers.py",
    "tests/fixtures/documents/tables.pdf",
    "apps/web/src/dist.ts",
]


def _git(repository: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repository)}
    subprocess.run(  # noqa: S603
        [executable, *arguments], cwd=repository, check=True, capture_output=True, env=environment
    )


def _repository(root: Path, paths: Iterable[str], *, gitignore: str = "") -> Path:
    _git(root, "init", "-q")
    if gitignore:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
        _git(root, "add", ".gitignore")
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        # -f: a file forced past .gitignore must still fail the check.
        _git(root, "add", "-f", path)
    return root


@pytest.mark.parametrize(("path", "pattern"), sorted(FORBIDDEN.items()))
def test_t516_each_forbidden_pattern_fails_naming_the_path(
    tmp_path: Path, path: str, pattern: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path, ["README.md", path])

    assert check_forbidden_files.main(["--root", str(repository)]) == 1
    output = capsys.readouterr().out
    assert f"forbidden tracked file: {path} (matches {pattern})" in output
    assert "README.md" not in output


def test_t516_env_forced_past_gitignore_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path, [".env", "apps/web/dist/app.js"], gitignore=".env\ndist/\n")

    assert check_forbidden_files.main(["--root", str(repository)]) == 1
    output = capsys.readouterr().out
    assert "forbidden tracked file: .env " in output
    assert "forbidden tracked file: apps/web/dist/app.js " in output


def test_t516_every_forbidden_path_is_named_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path, [*FORBIDDEN, *ALLOWED])

    assert check_forbidden_files.main(["--root", str(repository)]) == 1
    output = capsys.readouterr().out
    for path in FORBIDDEN:
        assert f"forbidden tracked file: {path} " in output
    for path in ALLOWED:
        assert f"forbidden tracked file: {path} " not in output
    assert f"{len(FORBIDDEN)} tracked file(s) match" in output


def test_t516_a_clean_repository_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository = _repository(tmp_path, ALLOWED)

    assert check_forbidden_files.main(["--root", str(repository)]) == 0
    assert f"among {len(ALLOWED)} tracked paths" in capsys.readouterr().out


@pytest.mark.parametrize("path", ["Archive.ZIP", "keys/Site.PFX"])
def test_suffixes_match_whatever_their_case(path: str) -> None:
    assert check_forbidden_files.forbidden_pattern(path) is not None


@pytest.mark.parametrize("path", ALLOWED)
def test_allowed_paths_match_no_pattern(path: str) -> None:
    assert check_forbidden_files.forbidden_pattern(path) is None


def test_a_directory_that_is_not_a_repository_is_a_tool_failure(tmp_path: Path) -> None:
    assert check_forbidden_files.main(["--root", str(tmp_path)]) == 2
