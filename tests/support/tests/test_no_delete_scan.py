"""T-077, the no-delete source scan (`80-data-model.md` section 2.20 and acceptance criterion 9).

No SQL statement built in `docchat_core`, `docchat_api`, `docchat_ingestion` and `docchat_ops`
deletes a row of `projects`, `audit_events`, `ingestion_methods`, `search_indexes`,
`physical_indexes`, `chunk_sets` or `ingestion_dispatcher_state`. Statements are SQLAlchemy Core
constructs over the `docchat_core.sql.tables` metadata, with raw text only as constants of
`docchat_core.sql.statements` (`80-data-model.md` section 1.4 item 6), so the scan reads every
module's syntax tree: each `sqlalchemy.delete(<table>)` and `<table>.delete()` call, its table
named through the module's imports, and every string constant that holds a `DELETE` or
`TRUNCATE` of a table. A `delete` whose table the scan can't name fails too, so a statement can't
pass by hiding its table behind a variable.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import Table

from docchat_core.sql import tables as tables_package

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
PACKAGES: Final = (
    REPOSITORY_ROOT / "packages" / "core" / "src" / "docchat_core",
    REPOSITORY_ROOT / "apps" / "api" / "src" / "docchat_api",
    REPOSITORY_ROOT / "apps" / "ingestion" / "src" / "docchat_ingestion",
    REPOSITORY_ROOT / "scripts" / "ops" / "src" / "docchat_ops",
)
"""The four packages that build SQL statements (the catalogue's `docchat_ingest` is the
ingestion app's `docchat_ingestion`)."""

NEVER_DELETED: Final = frozenset(
    {
        "projects",
        "audit_events",
        "ingestion_methods",
        "search_indexes",
        "physical_indexes",
        "chunk_sets",
        "ingestion_dispatcher_state",
    }
)

# SQL keywords are written in capitals, so prose about deleting ("delete the item") never matches.
_RAW_DELETE_RE: Final = re.compile(
    r"\b(?:DELETE(?:\s+TOP\s*\(\s*[^)]*\))?(?:\s+FROM)?|TRUNCATE\s+TABLE)\s+"
    r"(?:\[?app\]?\s*\.\s*)?\[?(?P<table>[A-Za-z_][A-Za-z0-9_]*)\]?"
)


def table_objects() -> dict[tuple[str, str], str]:
    """`(module, attribute)` → SQL table name for every `Table` of `docchat_core.sql.tables`,
    the package's own re-exports included."""
    found: dict[tuple[str, str], str] = {}
    modules = [tables_package.__name__] + [
        f"{tables_package.__name__}.{info.name}"
        for info in pkgutil.iter_modules(tables_package.__path__)
    ]
    for name in modules:
        module = importlib.import_module(name)
        for attribute, value in vars(module).items():
            if isinstance(value, Table):
                found[(name, attribute)] = value.name
    return found


@dataclass(frozen=True, slots=True)
class Delete:
    """One delete the scan found: where, and the table it names (`None` when it can't tell)."""

    location: str
    table: str | None


class _Imports:
    """A module's imported names: local name → (module, attribute) or (module, None)."""

    def __init__(self, tree: ast.Module) -> None:
        self.names: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                for alias in node.names:
                    self.names[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.names[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0],
                        None,
                    )

    def dotted(self, node: ast.expr) -> tuple[str, str] | None:
        """The (module, attribute) a name or a dotted attribute refers to through the imports."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id not in self.names:
            return None
        module, attribute = self.names[node.id]
        path = [
            *module.split("."),
            *([attribute] if attribute is not None else []),
            *reversed(parts),
        ]
        return ".".join(path[:-1]), path[-1]


def _is_sqlalchemy_delete(imports: _Imports, function: ast.expr) -> bool:
    resolved = imports.dotted(function)
    return (
        resolved is not None
        and resolved[1] == "delete"
        and (resolved[0] == "sqlalchemy" or resolved[0].startswith("sqlalchemy."))
    )


def scan_source(source: str, location: str, tables: dict[tuple[str, str], str]) -> Iterator[Delete]:
    """Every delete statement built in `source`."""
    tree = ast.parse(source)
    imports = _Imports(tree)

    def imported_table(node: ast.expr) -> str | None:
        resolved = imports.dotted(node)
        return None if resolved is None else tables.get(resolved)

    # A local name bound to a table (`runs = ingestion_runs`) names that table; a name bound to
    # two different tables names neither.
    aliases: dict[str, set[str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                aliases.setdefault(target.id, set()).add(imported_table(node.value))

    def table_of(node: ast.expr) -> str | None:
        table = imported_table(node)
        if table is None and isinstance(node, ast.Name):
            bound = aliases.get(node.id, set())
            table = next(iter(bound)) if len(bound) == 1 else None
        return table

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            where = f"{location}:{node.lineno}"
            if _is_sqlalchemy_delete(imports, node.func):
                yield Delete(where, table_of(node.args[0]) if node.args else None)
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete"
                and (table := table_of(node.func.value)) is not None
            ):
                yield Delete(where, table)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _RAW_DELETE_RE.finditer(node.value):
                yield Delete(f"{location}:{node.lineno}", match.group("table").lower())


def repository_deletes() -> list[Delete]:
    tables = table_objects()
    found: list[Delete] = []
    for package in PACKAGES:
        for path in sorted(package.rglob("*.py")):
            location = str(path.relative_to(REPOSITORY_ROOT))
            found.extend(scan_source(path.read_text(encoding="utf-8"), location, tables))
    return found


# T-077


def test_t077_no_statement_deletes_a_row_of_a_never_deleted_table() -> None:
    deletes = repository_deletes()

    assert [d for d in deletes if d.table in NEVER_DELETED] == []
    assert [d for d in deletes if d.table is None] == []


def test_t077_the_scan_reads_every_package_and_finds_the_known_deletes() -> None:
    deletes = repository_deletes()

    assert all(package.is_dir() for package in PACKAGES)
    # The retention steps and the stale-upload sweep delete rows, so the scan must see them.
    assert {"ingestion_run_items", "ingestion_runs", "scan_canaries"} <= {d.table for d in deletes}
    assert {"documents", "document_versions"} <= {d.table for d in deletes}


def test_t077_the_scan_finds_each_form_of_delete() -> None:
    tables = table_objects()
    source = (
        "import sqlalchemy as sa\n"
        "from sqlalchemy import delete, text\n"
        "from docchat_core.sql.tables.access import projects, audit_events as audit\n"
        "from docchat_core.sql.tables import ingestion\n"
        "a = delete(projects)\n"
        "b = sa.delete(audit).where(audit.c.id == 1)\n"
        "c = ingestion.chunk_sets.delete()\n"
        "d = delete(some_table)\n"
        "e = text('DELETE FROM app.search_indexes WHERE id = :id')\n"
        "f = 'DELETE TOP (500) FROM [app].[physical_indexes]'\n"
        "g = 'TRUNCATE TABLE app.ingestion_methods'\n"
        "h = queue.delete(message)\n"
        "runs = ingestion.ingestion_runs\n"
        "i = delete(runs)\n"
        "j = 'Delete the projects of a test, DELETE` on'\n"
    )

    found = sorted(
        scan_source(source, "example.py", tables),
        key=lambda d: int(d.location.rpartition(":")[2]),
    )

    assert [d.table for d in found] == [
        "projects",
        "audit_events",
        "chunk_sets",
        None,
        "search_indexes",
        "physical_indexes",
        "ingestion_methods",
        "ingestion_runs",
    ]


def test_t077_every_never_deleted_table_exists_in_the_metadata() -> None:
    assert set(table_objects().values()) >= NEVER_DELETED
