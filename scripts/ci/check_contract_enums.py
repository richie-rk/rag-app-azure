"""Response enumerations evolve client first (01-repository.md section 7.5, ADR 0013 item 7).

A new value of a response enumeration ships in this order: the API model adds it and the
committed document carries it; the web release that understands it deploys; only then does the
API return it. The `contract` job runs this check after the drift check. It compares the pull
request's `apps/api/openapi.json` with the base branch's and fails when a response enumeration
gains a value while the same pull request adds a code path returning it, unless the pull request
carries the label `enum-already-released`.

- A response enumeration is an `enum` (or a single `const`) in a schema reachable from an
  operation's response bodies, `$ref`s followed. A value is gained when an enumeration present in
  both documents, at the same location, holds it only in the pull request's document; a brand-new
  enumeration gains nothing, because the client drops members it doesn't know.
- A code path returning a gained value is a line the pull request adds to production source
  (`apps/<app>/src`, `packages/<package>/src`) that holds the value as a string literal, or names
  the enum member whose value it is (`TurnStatus.NEW_VALUE`), outside the declarations that only
  add it to the model: an enum member's own assignment, a `Literal[...]` and a docstring.

The base is `--base`, else the pull request's base commit from `GITHUB_EVENT_PATH`, else
`origin/$GITHUB_BASE_REF`; when that commit isn't in a shallow checkout it is fetched with
`git fetch --depth=1 origin <sha>`. The labels come from the event payload, or `--label`.
Exit codes: 0 no gained value has a code path, or the label is present; 1 otherwise, or the check
can't run.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = "apps/api/openapi.json"
LABEL = "enum-already-released"
OPERATIONS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})
NOT_SCHEMA_KEYS = frozenset({"enum", "const", "default", "example", "examples", "description"})
PRODUCTION_SOURCE = re.compile(r"^(?:apps|packages)/[^/]+/src/.+\.py$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SCHEMA_REF = "#/components/schemas/"


# --- Response enumerations -----------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _pointer_escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


class _EnumCollector:
    def __init__(self, document: dict[str, Any]) -> None:
        components = document.get("components", {})
        self.schemas: dict[str, Any] = components.get("schemas", {}) if components else {}
        self.responses: dict[str, Any] = components.get("responses", {}) if components else {}
        self.enums: dict[str, dict[str, Any]] = {}
        self.visited: set[str] = set()

    def walk(self, node: Any, location: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                self.walk(item, f"{location}/{index}")
            return
        if not isinstance(node, dict):
            return
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith(SCHEMA_REF):
            name = reference[len(SCHEMA_REF) :]
            if name not in self.visited and name in self.schemas:
                self.visited.add(name)
                self.walk(self.schemas[name], reference)
        if isinstance(node.get("enum"), list):
            self.enums[location] = {_canonical(value): value for value in node["enum"]}
        elif "const" in node:
            self.enums[location] = {_canonical(node["const"]): node["const"]}
        for key, value in node.items():
            if key in NOT_SCHEMA_KEYS or key.startswith("x-") or key == "$ref":
                continue
            self.walk(value, f"{location}/{_pointer_escape(key)}")

    def response(self, response: Any, location: str) -> None:
        if isinstance(response, dict) and isinstance(response.get("$ref"), str):
            name = response["$ref"].rpartition("/")[2]
            response = self.responses.get(name)
            location = f"#/components/responses/{_pointer_escape(name)}"
        if not isinstance(response, dict):
            return
        for media_type, content in (response.get("content") or {}).items():
            if isinstance(content, dict) and "schema" in content:
                escaped = _pointer_escape(media_type)
                self.walk(content["schema"], f"{location}/content/{escaped}/schema")


def response_enums(document: Any) -> dict[str, dict[str, Any]]:
    """Map each response enumeration's JSON pointer to its values, keyed by canonical JSON."""
    if not isinstance(document, dict):
        return {}
    collector = _EnumCollector(document)
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method not in OPERATIONS or not isinstance(operation, dict):
                continue
            prefix = f"#/paths/{_pointer_escape(path)}/{method}/responses"
            for status, response in (operation.get("responses") or {}).items():
                collector.response(response, f"{prefix}/{status}")
    return collector.enums


def gained_values(base: Any, head: Any) -> dict[str, list[Any]]:
    """Values each enumeration present in both documents holds only in `head`."""
    before = response_enums(base)
    after = response_enums(head)
    gained: dict[str, list[Any]] = {}
    for location, values in after.items():
        if location in before:
            new = [value for key, value in values.items() if key not in before[location]]
            if new:
                gained[location] = new
    return gained


# --- Code paths in the pull request --------------------------------------------------------------


@dataclass(frozen=True)
class CodePath:
    path: str
    line: int
    value: Any
    how: str


def _is_enum_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if isinstance(name, str) and name.endswith("Enum"):
            return True
    return False


def _declarations(tree: ast.Module) -> tuple[set[int], dict[tuple[str, str], Any]]:
    """Ids of the constants that only declare a value, and every enum member's value."""
    declared: set[int] = set()
    members: dict[tuple[str, str], Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_enum_class(node):
            for statement in node.body:
                value = statement.value if isinstance(statement, ast.Assign) else None
                if isinstance(value, ast.Constant) and isinstance(statement, ast.Assign):
                    declared.add(id(value))
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            members[(node.name, target.id)] = value.value
        elif isinstance(node, ast.Subscript):
            name = node.value.attr if isinstance(node.value, ast.Attribute) else ""
            name = node.value.id if isinstance(node.value, ast.Name) else name
            if name == "Literal":
                declared.update(id(child) for child in ast.walk(node.slice))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            declared.add(id(node.value))
    return declared, members


def code_paths(
    path: str,
    tree: ast.Module,
    added_lines: set[int],
    gained: Iterable[Any],
    members: dict[tuple[str, str], Any],
) -> list[CodePath]:
    """Find the added lines of one file that return a gained value.

    `members` maps (enum class, member) to its value across every changed file, because the
    model that gains the value and the code that returns it are usually different modules.
    """
    wanted = {_canonical(value): value for value in gained}
    declared, _ = _declarations(tree)
    wanted_members = {key: value for key, value in members.items() if _canonical(value) in wanted}
    found: list[CodePath] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line not in added_lines:
            continue
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in declared
            and _canonical(node.value) in wanted
        ):
            found.append(CodePath(path, node.lineno, node.value, "literal"))
        elif isinstance(node, ast.Attribute):
            owner = node.value.attr if isinstance(node.value, ast.Attribute) else ""
            owner = node.value.id if isinstance(node.value, ast.Name) else owner
            if (owner, node.attr) in wanted_members:
                value = wanted_members[(owner, node.attr)]
                found.append(CodePath(path, node.lineno, value, f"{owner}.{node.attr}"))
    return found


def added_lines(diff: str) -> dict[str, set[int]]:
    """Map each file of a `git diff -U0` to the line numbers the diff adds to it."""
    lines: dict[str, set[int]] = {}
    current: set[int] | None = None
    for text in diff.splitlines():
        if text.startswith("+++ "):
            target = text[4:]
            current = lines.setdefault(target[2:], set()) if target.startswith("b/") else None
            continue
        match = HUNK.match(text)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            current.update(range(start, start + count))
    return lines


# --- Git and the event ---------------------------------------------------------------------------


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git is not on PATH")
    # Every argument is a fixed git subcommand, a commit, a ref or a repository path.
    return subprocess.run(  # noqa: S603
        [executable, *arguments], cwd=root, check=False, capture_output=True, text=True
    )


def read_event() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return event if isinstance(event, dict) else {}


def resolve_base(explicit: str | None, event: dict[str, Any]) -> str | None:
    if explicit:
        return explicit
    base = (event.get("pull_request") or {}).get("base", {}).get("sha")
    if isinstance(base, str) and base:
        return base
    base_ref = os.environ.get("GITHUB_BASE_REF")
    return f"origin/{base_ref}" if base_ref else None


def event_labels(event: dict[str, Any]) -> set[str]:
    labels = (event.get("pull_request") or {}).get("labels") or []
    return {str(label.get("name")) for label in labels if isinstance(label, dict)}


def ensure_commit(root: Path, base: str) -> None:
    """Make the base commit available; a shallow pull-request checkout holds only the merge."""
    if git(root, "cat-file", "-e", f"{base}^{{commit}}").returncode == 0:
        return
    if git(root, "fetch", "--no-tags", "--depth=1", "origin", base).returncode != 0:
        raise RuntimeError(f"the base {base} isn't in the checkout and couldn't be fetched")


def base_document(root: Path, base: str) -> Any:
    shown = git(root, "show", f"{base}:{DOCUMENT}")
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def changed_source(root: Path, base: str) -> Iterator[tuple[str, set[int]]]:
    # Two dots: a pull-request checkout's HEAD is the merge commit, whose first parent is the
    # base, and a shallow checkout has no history to find a merge base in.
    diff = git(root, "diff", "-U0", "--no-color", "--diff-filter=AMR", base, "HEAD")
    if diff.returncode != 0:
        raise RuntimeError(f"git diff against {base} failed: {diff.stderr.strip()}")
    for path, lines in sorted(added_lines(diff.stdout).items()):
        if PRODUCTION_SOURCE.match(path) and lines:
            yield path, lines


def check(root: Path, base: str) -> tuple[dict[str, list[Any]], list[CodePath]]:
    """Return the gained response-enumeration values and the code paths returning them."""
    ensure_commit(root, base)
    before = base_document(root, base)
    after = json.loads((root / DOCUMENT).read_text(encoding="utf-8"))
    gained = gained_values(before, after)
    if not gained:
        return gained, []
    every_value = [value for values in gained.values() for value in values]
    changed = [
        (path, lines, ast.parse((root / path).read_text(encoding="utf-8"), filename=path))
        for path, lines in changed_source(root, base)
    ]
    members: dict[tuple[str, str], Any] = {}
    for _, _, tree in changed:
        members.update(_declarations(tree)[1])
    paths: list[CodePath] = []
    for path, lines, tree in changed:
        paths.extend(code_paths(path, tree, lines, every_value, members))
    return gained, paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Response enumerations evolve client first.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--base", help="the commit or ref the pull request merges into")
    parser.add_argument("--label", action="append", default=[], help="a pull request label")
    args = parser.parse_args(argv)
    event = read_event()
    base = resolve_base(args.base, event)
    if base is None:
        sys.stdout.write("check_contract_enums: no base; pass --base or run on a pull request\n")
        return 1
    try:
        gained, paths = check(args.root, base)
    except (OSError, RuntimeError, SyntaxError, json.JSONDecodeError) as error:
        sys.stdout.write(f"check_contract_enums: {error}\n")
        return 1
    for location, values in sorted(gained.items()):
        shown = ", ".join(json.dumps(value) for value in values)
        sys.stdout.write(f"{location}: the response enumeration gains {shown}\n")
    for code_path in paths:
        sys.stdout.write(
            f"{code_path.path}:{code_path.line}: returns {json.dumps(code_path.value)} "
            f"({code_path.how}) in the same pull request\n"
        )
    if not paths:
        sys.stdout.write("no added code path returns a gained response-enumeration value\n")
        return 0
    if LABEL in event_labels(event) | set(args.label):
        sys.stdout.write(f"passing: the pull request carries the label {LABEL}\n")
        return 0
    sys.stdout.write(
        "a response enumeration gains a value that the same pull request returns; ship the "
        f"value first, deploy the web release that understands it, then return it, or add the "
        f"label {LABEL} once that release is deployed\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
