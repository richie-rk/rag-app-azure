"""Guard the one untyped-imports override of mypy (01-repository.md section 5.3).

The root `pyproject.toml` has exactly one `[[tool.mypy.overrides]]` block that lets mypy follow
untyped imports, listing only third-party modules that mypy reports as `import-untyped`. The check
fails when that block names a first-party package (`docchat_*`), or when a second override block
sets `follow_untyped_imports` or `ignore_missing_imports`, so type checking can't be switched off
for first-party code or widened quietly.

Usage: uv run --frozen python scripts/ci/check_mypy_untyped.py [--pyproject <path>]
Exit codes: 0 the overrides hold to the rule; 1 they don't, or the file can't be read.
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
UNTYPED_KEYS = ("follow_untyped_imports", "ignore_missing_imports")
FIRST_PARTY_PREFIX = "docchat_"


def _modules(block: dict[str, Any]) -> list[str]:
    value = block.get("module", [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def problems_in(config: dict[str, Any]) -> list[str]:
    """Return every breach of the untyped-imports rule in a parsed `pyproject.toml`."""
    overrides = config.get("tool", {}).get("mypy", {}).get("overrides", [])
    if not isinstance(overrides, list):
        return ["[[tool.mypy.overrides]] must be an array of tables"]
    problems: list[str] = []
    untyped_blocks = 0
    for index, block in enumerate(overrides, start=1):
        if not isinstance(block, dict):
            problems.append(f"override block {index} is not a table")
            continue
        keys = [key for key in UNTYPED_KEYS if block.get(key) is True]
        if not keys:
            continue
        untyped_blocks += 1
        modules = _modules(block)
        if untyped_blocks > 1:
            problems.append(
                f"override block {index} ({', '.join(modules) or 'no module'}) sets "
                f"{' and '.join(keys)}; only one block may relax untyped imports"
            )
        problems.extend(
            f"override block {index} names the first-party module {module}; "
            "first-party code is always type-checked"
            for module in modules
            if module.split(".", 1)[0].startswith(FIRST_PARTY_PREFIX)
        )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard the untyped-imports override of mypy.")
    parser.add_argument("--pyproject", type=Path, default=REPOSITORY_ROOT / "pyproject.toml")
    args = parser.parse_args(argv)
    try:
        config = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        sys.stdout.write(f"check_mypy_untyped: can't read {args.pyproject}: {error}\n")
        return 1
    problems = problems_in(config)
    for problem in problems:
        sys.stdout.write(f"{args.pyproject.name}: {problem}\n")
    if problems:
        return 1
    sys.stdout.write("the mypy untyped-imports override holds to the rule\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
