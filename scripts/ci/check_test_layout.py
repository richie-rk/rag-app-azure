"""Fail when a module has no mirrored unit test (01-repository.md section 5.5, 84-testing.md 10.2).

Every module `<member>/src/<package>/<path>/<name>.py` of a workspace member, other than
`__init__.py` and `__main__.py`, needs `<member>/tests/unit/<path>/test_<name>.py`, unless the root
`pyproject.toml` lists the module's repository-relative path in `[tool.docchat.test-layout] exempt`
with a reason. The check proves a test exists for every module; coverage proves its depth.

Usage: uv run --frozen python scripts/ci/check_test_layout.py [--root <repository>]
Exit codes: 0 every module has its test; 1 a module lacks one or the exempt list is malformed.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NOT_MODULES = frozenset({"__init__.py", "__main__.py"})


class LayoutConfigError(ValueError):
    """The workspace or the test-layout table can't be read."""


def workspace_members(root: Path, config: dict[str, Any]) -> list[Path]:
    """Return the member directories that `[tool.uv.workspace] members` names, globs expanded."""
    try:
        patterns = config["tool"]["uv"]["workspace"]["members"]
    except KeyError as error:
        raise LayoutConfigError("pyproject.toml has no [tool.uv.workspace] members") from error
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise LayoutConfigError("[tool.uv.workspace] members must be a list of strings")
    members: list[Path] = []
    for pattern in patterns:
        if any(character in pattern for character in "*?["):
            members.extend(sorted(path for path in root.glob(pattern) if path.is_dir()))
        else:
            members.append(root / pattern)
    return members


def exempt_paths(config: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Return the exempt paths with their reasons, and the problems found in the table."""
    table = config.get("tool", {}).get("docchat", {}).get("test-layout", {})
    entries = table.get("exempt", [])
    problems: list[str] = []
    exempt: dict[str, str] = {}
    if not isinstance(entries, list):
        return exempt, ["[tool.docchat.test-layout] exempt must be a list of tables"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"exempt entry {index} must be a table with path and reason")
            continue
        path = entry.get("path")
        reason = entry.get("reason")
        if not isinstance(path, str) or not path.strip():
            problems.append(f"exempt entry {index} has no path")
            continue
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"exempt entry {path} has no reason")
            continue
        exempt[path.strip()] = reason.strip()
    return exempt, problems


def missing_tests(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (module, expected test) for every module without its test, and config problems."""
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    exempt, problems = exempt_paths(config)
    missing: list[tuple[str, str]] = []
    for member in workspace_members(root, config):
        source = member / "src"
        if not source.is_dir():
            continue
        for package in sorted(path for path in source.iterdir() if path.is_dir()):
            for module in sorted(package.rglob("*.py")):
                if module.name in NOT_MODULES or "__pycache__" in module.parts:
                    continue
                relative = module.relative_to(package)
                expected = member / "tests" / "unit" / relative.parent / f"test_{module.name}"
                module_path = module.relative_to(root).as_posix()
                if expected.is_file() or module_path in exempt:
                    continue
                missing.append((module_path, expected.relative_to(root).as_posix()))
    return missing, problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a module has no mirrored unit test.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    try:
        missing, problems = missing_tests(args.root)
    except (OSError, tomllib.TOMLDecodeError, LayoutConfigError) as error:
        sys.stdout.write(f"check_test_layout: {error}\n")
        return 1
    for problem in problems:
        sys.stdout.write(f"[tool.docchat.test-layout]: {problem}\n")
    for module, expected in missing:
        sys.stdout.write(f"{module}: no unit test; expected {expected}\n")
    if missing or problems:
        sys.stdout.write(
            f"{len(missing)} module(s) without a mirrored unit test; write the test, or list "
            "the module in [tool.docchat.test-layout] exempt with a reason.\n"
        )
        return 1
    sys.stdout.write("every module has its mirrored unit test\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
