"""Hold new Alembic revisions to expand, deploy, contract (80-data-model.md section 2.24).

84-testing.md section 10.3 and ADR 0027 give the same rule from the CI side.

Run by the `migrations` job (checkout with `fetch-depth: 0`, `permissions {contents: read,
deployments: read}`). It parses each revision the pull request adds or changes under
`db/migrations/versions/` and fails:

1. when a destructive operation appears in a revision whose docstring doesn't start with
   `Contract: completes NNNN`: `op.drop_table`, `op.drop_column`, `op.drop_constraint`,
   `op.drop_index` of an index created before the revision's parent, `op.alter_column` that changes
   `type_` or sets `nullable=False`, a table or column rename (`op.rename_table`,
   `new_column_name=`, `sp_rename`), or an `op.execute` whose SQL contains `DROP`, `TRUNCATE` or a
   `DELETE` without `WHERE`. Re-creating `CK_audit_events_action` and
   `CK_audit_events_target_type` with wider lists in one revision is the one drop an expand
   revision may hold. The check is syntactic: SQL built at run time rests on review.
2. when a contract revision's expand revision `NNNN` isn't in the commit of the latest successful
   `prod` deployment, found through `GET /repos/{owner}/{repo}/deployments?environment=prod
   &per_page=10` and each deployment's statuses, and checked with
   `git cat-file -e <sha>:db/migrations/versions/<NNNN>_<slug>.py`.

The base of the comparison is `--base`, else the pull request's base commit from
`GITHUB_EVENT_PATH`, else `origin/$GITHUB_BASE_REF`. Environment for rule 2: `GITHUB_REPOSITORY`,
`GITHUB_TOKEN` or `GH_TOKEN`, and optionally `GITHUB_API_URL`.
Exit codes: 0 every new revision follows the rule; 1 one doesn't, or the check can't run.
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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = "db/migrations/versions"
REVISION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
CONTRACT = re.compile(r"^Contract: completes (\d{4})\b")
AUDIT_CHECKS = frozenset({"CK_audit_events_action", "CK_audit_events_target_type"})
ALWAYS_DESTRUCTIVE = frozenset({"drop_table", "drop_column", "rename_table"})
SQL_TEXT_CALLS = frozenset({"text", "sa.text", "sqlalchemy.text"})
DEFAULT_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
DEPLOYMENTS_PER_PAGE = 10

DROP = re.compile(r"\bDROP\b", re.IGNORECASE)
DROP_AUDIT_CHECK = re.compile(
    r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?\[?(CK_audit_events_(?:action|target_type))\]?",
    re.IGNORECASE,
)
ADD_CHECK = re.compile(r"\bADD\s+CONSTRAINT\s+\[?(\w+)\]?\s+CHECK\b", re.IGNORECASE)
TRUNCATE = re.compile(r"\bTRUNCATE\b", re.IGNORECASE)
DELETE = re.compile(r"\bDELETE\b", re.IGNORECASE)
WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
RENAME = re.compile(r"\bsp_rename\b", re.IGNORECASE)
CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?INDEX\s+\[?(\w+)\]?", re.IGNORECASE
)
SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")
SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


# --- Parsing one revision ------------------------------------------------------------------------


@dataclass
class Revision:
    path: str
    number: str
    tree: ast.Module
    docstring_first_line: str
    parent: str | None
    constants: dict[str, str] = field(default_factory=dict)

    @property
    def completes(self) -> str | None:
        match = CONTRACT.match(self.docstring_first_line)
        return match.group(1) if match else None


def parse_revision(path: str, source: str) -> Revision:
    tree = ast.parse(source, filename=path)
    docstring = ast.get_docstring(tree) or ""
    match = REVISION_FILE.match(Path(path).name)
    revision = Revision(
        path=path,
        number=match.group(1) if match else "",
        tree=tree,
        docstring_first_line=docstring.strip().splitlines()[0] if docstring.strip() else "",
        parent=None,
    )
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "down_revision" and isinstance(value, ast.Constant):
                revision.parent = value.value if isinstance(value.value, str) else None
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                revision.constants[target.id] = value.value
    return revision


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _literal(node: ast.expr | None, constants: dict[str, str]) -> str | None:
    """The string a node stands for: a constant, a module constant, `op.f(...)` or `text(...)`."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Call) and node.args and _dotted(node.func) in {"op.f", *SQL_TEXT_CALLS}:
        return _literal(node.args[0], constants)
    return None


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    return call.args[position] if len(call.args) > position else None


def _clean_sql(sql: str) -> str:
    # Quoted values and comments can hold any word, so only the statement text is searched.
    sql = SQL_BLOCK_COMMENT.sub(" ", sql)
    sql = SQL_LINE_COMMENT.sub(" ", sql)
    return SQL_LITERAL.sub("''", sql)


def _calls(revision: Revision) -> Iterable[tuple[str, ast.Call]]:
    """Yield (operation, call) for each `op.<operation>(...)` outside `downgrade()`."""
    aliases = {"op"}
    for node in ast.walk(revision.tree):
        if isinstance(node, ast.With):
            for item in node.items:
                is_batch = isinstance(item.context_expr, ast.Call) and _dotted(
                    item.context_expr.func
                ) in {"op.batch_alter_table"}
                if is_batch and isinstance(item.optional_vars, ast.Name):
                    aliases.add(item.optional_vars.id)
    downgrade_nodes: set[int] = set()
    for node in revision.tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            downgrade_nodes.update(id(child) for child in ast.walk(node))
    for node in ast.walk(revision.tree):
        if id(node) in downgrade_nodes or not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            yield func.attr, node


def created_check_constraints(revision: Revision) -> set[str]:
    """Names of the check constraints a revision creates."""
    names: set[str] = set()
    for operation, call in _calls(revision):
        if operation == "create_check_constraint":
            name = _literal(_argument(call, 0, "constraint_name"), revision.constants)
            if name:
                names.add(name)
        elif operation == "execute":
            sql = _literal(_argument(call, 0, "sqltext"), revision.constants)
            if sql:
                names.update(ADD_CHECK.findall(_clean_sql(sql)))
    return names


def created_indexes(revision: Revision) -> set[str]:
    """Names of the indexes a revision creates, through `op.create_index`, `sa.Index` or SQL."""
    names: set[str] = set()
    for operation, call in _calls(revision):
        if operation == "create_index":
            name = _literal(_argument(call, 0, "index_name"), revision.constants)
            if name:
                names.add(name)
        elif operation == "execute":
            sql = _literal(_argument(call, 0, "sqltext"), revision.constants)
            if sql:
                names.update(CREATE_INDEX.findall(_clean_sql(sql)))
    for node in ast.walk(revision.tree):
        if isinstance(node, ast.Call) and _dotted(node.func) in {"sa.Index", "Index"}:
            name = _literal(node.args[0] if node.args else None, revision.constants)
            if name:
                names.add(name)
    return names


# --- Rule 1: destructive operations --------------------------------------------------------------


def _sql_problems(sql: str, recreated: set[str]) -> list[str]:
    cleaned = _clean_sql(sql)
    problems: list[str] = []
    allowed_drops = [name for name in DROP_AUDIT_CHECK.findall(cleaned) if name in recreated]
    if len(DROP.findall(cleaned)) > len(allowed_drops):
        problems.append("op.execute with DROP")
    if TRUNCATE.search(cleaned):
        problems.append("op.execute with TRUNCATE")
    if RENAME.search(cleaned):
        problems.append("op.execute renaming with sp_rename")
    problems.extend(
        "op.execute with a DELETE without WHERE"
        for statement in cleaned.split(";")
        if DELETE.search(statement) and not WHERE.search(statement)
    )
    return problems


def _index_created_in(name: str, number: str, history: dict[str, set[str]]) -> str | None:
    """The newest revision before `number` that creates index `name`, else `number` itself."""
    earlier = [
        revision for revision, names in history.items() if revision < number and name in names
    ]
    if earlier:
        return max(earlier)
    return number if name in history.get(number, set()) else None


def destructive_operations(
    revision: Revision, history: dict[str, set[str]]
) -> list[tuple[int, str]]:
    """Return (line, operation) for every destructive operation in a revision."""
    recreated = created_check_constraints(revision)
    found: list[tuple[int, str]] = []
    for operation, call in _calls(revision):
        line = call.lineno
        if operation in ALWAYS_DESTRUCTIVE:
            found.append((line, f"op.{operation}"))
        elif operation == "drop_constraint":
            name = _literal(_argument(call, 0, "constraint_name"), revision.constants)
            if not (name in AUDIT_CHECKS and name in recreated):
                found.append((line, f"op.drop_constraint of {name or 'a computed name'}"))
        elif operation == "drop_index":
            name = _literal(_argument(call, 0, "index_name"), revision.constants)
            created = _index_created_in(name, revision.number, history) if name else None
            if created is None or (revision.parent is not None and created < revision.parent):
                found.append((line, f"op.drop_index of {name or 'a computed name'}"))
        elif operation == "alter_column":
            keywords = {item.arg: item.value for item in call.keywords if item.arg}
            if "type_" in keywords:
                found.append((line, "op.alter_column changing type_"))
            nullable = keywords.get("nullable")
            if isinstance(nullable, ast.Constant) and nullable.value is False:
                found.append((line, "op.alter_column setting nullable=False"))
            if "new_column_name" in keywords:
                found.append((line, "op.alter_column renaming a column"))
        elif operation == "execute":
            sql = _literal(_argument(call, 0, "sqltext"), revision.constants)
            found.extend((line, problem) for problem in _sql_problems(sql or "", recreated))
    return sorted(found)


# --- Rule 2: the expand revision is in prod ------------------------------------------------------


class GitHubApiError(RuntimeError):
    """A GitHub REST call failed."""


def github_get(path: str) -> Any:
    """GET a GitHub REST path with the workflow token and return the parsed JSON body."""
    api = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).rstrip("/")
    if not api.startswith("https://"):
        raise GitHubApiError("GITHUB_API_URL must be an https URL")
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
        raise GitHubApiError(f"GET {path}: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise GitHubApiError(f"GET {path}: {error}") from error


def latest_prod_commit(repository: str, get: Callable[[str], Any]) -> str | None:
    """The commit of the newest `prod` deployment that has a `success` status, or None."""
    deployments = get(
        f"/repos/{repository}/deployments?environment=prod&per_page={DEPLOYMENTS_PER_PAGE}"
    )
    if not isinstance(deployments, list):
        raise GitHubApiError("the deployments answer isn't a list")
    for deployment in deployments:
        if not isinstance(deployment, dict):
            continue
        statuses = get(f"/repos/{repository}/deployments/{deployment.get('id')}/statuses")
        if isinstance(statuses, list) and any(
            isinstance(status, dict) and status.get("state") == "success" for status in statuses
        ):
            sha = deployment.get("sha")
            return sha if isinstance(sha, str) else None
    return None


# --- Git -----------------------------------------------------------------------------------------


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git is not on PATH")
    # Every argument is a fixed git subcommand, a ref or a repository path.
    return subprocess.run(  # noqa: S603
        [executable, *arguments], cwd=root, check=False, capture_output=True, text=True
    )


def resolve_base(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            event = {}
        pull_request = event.get("pull_request") if isinstance(event, dict) else None
        base = (pull_request or {}).get("base", {}).get("sha")
        if isinstance(base, str) and base:
            return base
    base_ref = os.environ.get("GITHUB_BASE_REF")
    return f"origin/{base_ref}" if base_ref else None


def changed_revisions(root: Path, base: str) -> list[str]:
    # Two dots: on a pull request HEAD is the merge commit, whose first parent is the base.
    completed = git(
        root, "diff", "--name-only", "--diff-filter=AMR", base, "HEAD", "--", f"{VERSIONS}/"
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff against {base} failed: {completed.stderr.strip()}")
    return sorted(
        path
        for path in completed.stdout.splitlines()
        if path.endswith(".py") and not Path(path).name.startswith("__")
    )


def file_in_commit(root: Path, commit: str, path: str) -> bool:
    return git(root, "cat-file", "-e", f"{commit}:{path}").returncode == 0


# --- The check -----------------------------------------------------------------------------------


def check(
    root: Path, changed: Sequence[str], repository: str, get: Callable[[str], Any]
) -> list[str]:
    """Return every breach of the rule among the changed revision files."""
    versions = root / VERSIONS
    revisions: dict[str, Revision] = {}
    problems: list[str] = []
    for path in sorted(versions.glob("*.py")):
        relative = path.relative_to(root).as_posix()
        if REVISION_FILE.match(path.name):
            revisions[relative] = parse_revision(relative, path.read_text(encoding="utf-8"))
    history = {revision.number: created_indexes(revision) for revision in revisions.values()}
    contracts: list[Revision] = []
    for changed_path in changed:
        revision = revisions.get(changed_path)
        if revision is None:
            if (root / changed_path).exists():
                problems.append(
                    f"{changed_path}: a revision file must be named NNNN_<snake_slug>.py"
                )
            continue
        if revision.completes:
            contracts.append(revision)
            continue
        problems.extend(
            f"{changed_path}:{line}: {operation} in an expand revision; only a revision "
            "whose docstring starts 'Contract: completes NNNN' may hold it"
            for line, operation in destructive_operations(revision, history)
        )
    if not contracts:
        return problems
    if repository.count("/") != 1:
        return [*problems, "GITHUB_REPOSITORY must name the repository as <owner>/<repo>"]
    commit = latest_prod_commit(repository, get)
    for revision in contracts:
        expand = revision.completes or ""
        expand_files = [p for p, r in revisions.items() if r.number == expand]
        if expand >= revision.number or not expand_files:
            problems.append(f"{revision.path}: completes {expand}, which isn't an earlier revision")
        elif commit is None:
            problems.append(
                f"{revision.path}: completes {expand}, but no prod deployment has succeeded yet"
            )
        elif not file_in_commit(root, commit, expand_files[0]):
            problems.append(
                f"{revision.path}: completes {expand}, which isn't in commit {commit} of the "
                "latest successful prod deployment"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold new revisions to expand and contract.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--base", help="the commit or ref the pull request merges into")
    args = parser.parse_args(argv)
    base = resolve_base(args.base)
    if base is None:
        sys.stdout.write("check_migrations: no base; pass --base or run on a pull request\n")
        return 1
    try:
        changed = changed_revisions(args.root, base)
        problems = check(args.root, changed, os.environ.get("GITHUB_REPOSITORY", ""), github_get)
    except (OSError, RuntimeError, SyntaxError) as error:
        sys.stdout.write(f"check_migrations: {error}\n")
        return 1
    for problem in problems:
        sys.stdout.write(problem + "\n")
    if problems:
        return 1
    sys.stdout.write(f"{len(changed)} new or changed revision(s) follow expand and contract\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
