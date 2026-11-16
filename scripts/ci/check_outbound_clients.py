"""AST check of outbound clients and import contract 6 (01-repository.md 2.3, 2.4, 6.4; ADR 0150).

Parses every workspace member's `src/`, `scripts/ci/` and `tests/load/aci/run.py`, and fails,
naming file and line, when:

1. an `httpx.AsyncClient(` call has no `timeout=` keyword or doesn't pass `follow_redirects` as
   the literal `False` when it passes it at all;
2. a `urlopen(` call (`urllib.request.urlopen`) has no `timeout=` keyword;
3. an Azure SDK client constructor inside `docchat_core.clients` lacks `connection_timeout=` or
   `read_timeout=` (Cosmos DB: `connection_timeout=` only, its reads are bounded per operation);
4. in `docchat_core`, `docchat_api` or `docchat_ingestion`, an Azure SDK client class from
   `azure.storage`, `azure.cosmos`, `azure.search.documents`, `azure.ai.documentintelligence` or
   `azure.keyvault`, or `openai.AsyncOpenAI`, is instantiated outside `docchat_core.clients`
   (a class whose name ends in `Client`, called directly or through a `from_*` constructor);
5. a module of `docchat_core`, `docchat_api`, `docchat_ingestion` or `docchat_model_stub` other
   than `docchat_core.clients` (with its submodules) and `docchat_core.sql.engine` imports
   `azure.identity` or `azure.identity.aio` (import contract 6, which import-linter can't express
   for a subpackage of an external package).

Names are resolved through each module's imports, so aliases (`import httpx as h`,
`from urllib.request import urlopen`) are followed. A call that passes `**kwargs` without the
keyword is treated as missing it, because the check can't see what the mapping holds.

Usage: uv run --frozen python scripts/ci/check_outbound_clients.py [--root <repository>]
Exit codes: 0 no breach; 1 at least one breach, or a file can't be parsed.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXTRA_PATHS = ("scripts/ci", "tests/load/aci/run.py")
CLIENT_PACKAGES = (
    "azure.storage",
    "azure.cosmos",
    "azure.search.documents",
    "azure.ai.documentintelligence",
    "azure.keyvault",
)
OPENAI_CLIENT = "openai.AsyncOpenAI"
COSMOS = "azure.cosmos"
FACTORY_RESTRICTED = ("docchat_core", "docchat_api", "docchat_ingestion")
IDENTITY_RESTRICTED = (*FACTORY_RESTRICTED, "docchat_model_stub")
CLIENT_FACTORY = "docchat_core.clients"
IDENTITY_ALLOWED = (CLIENT_FACTORY, "docchat_core.sql.engine")
IDENTITY = "azure.identity"


@dataclass(frozen=True)
class Breach:
    path: str
    line: int
    message: str

    @override
    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _within(module: str | None, package: str) -> bool:
    return module is not None and (module == package or module.startswith(package + "."))


def _within_any(module: str | None, packages: Sequence[str]) -> bool:
    return any(_within(module, package) for package in packages)


def module_name(source_root: Path, path: Path) -> str:
    """The dotted module name of a file below a member's `src/`."""
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _ModuleChecker(ast.NodeVisitor):
    def __init__(self, path: str, module: str | None) -> None:
        self.path = path
        self.module = module
        self.names: dict[str, str] = {}
        self.breaches: list[Breach] = []

    def _breach(self, node: ast.stmt | ast.expr, message: str) -> None:
        self.breaches.append(Breach(self.path, node.lineno, message))

    def _identity_import(self, node: ast.stmt, imported: str) -> None:
        if (
            _within(imported, IDENTITY)
            and _within_any(self.module, IDENTITY_RESTRICTED)
            and not _within_any(self.module, IDENTITY_ALLOWED)
        ):
            self._breach(
                node,
                f"{self.module} imports {imported}; only docchat_core.clients and "
                "docchat_core.sql.engine may (import contract 6)",
            )

    @override
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._identity_import(node, alias.name)
            if alias.asname:
                self.names[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                self.names[root] = root

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for alias in node.names:
            qualified = f"{node.module}.{alias.name}"
            self._identity_import(node, qualified if node.module == "azure" else node.module)
            self.names[alias.asname or alias.name] = qualified

    def resolve(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    @override
    def visit_Call(self, node: ast.Call) -> None:
        qualified = self.resolve(node.func)
        if qualified:
            self._check_call(node, qualified)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call, qualified: str) -> None:
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        if qualified == "httpx.AsyncClient":
            if "timeout" not in keywords:
                self._breach(node, "httpx.AsyncClient( without timeout=")
            redirects = keywords.get("follow_redirects")
            if redirects is not None and not (
                isinstance(redirects, ast.Constant) and redirects.value is False
            ):
                self._breach(node, "httpx.AsyncClient( must not follow redirects")
        elif qualified == "urllib.request.urlopen":
            if "timeout" not in keywords:
                self._breach(node, "urlopen( without timeout=")
        else:
            client = client_class(qualified)
            if client is not None:
                self._check_client(node, client, keywords)

    def _check_client(self, node: ast.Call, client: str, keywords: dict[str, ast.expr]) -> None:
        if _within_any(self.module, FACTORY_RESTRICTED) and not _within(
            self.module, CLIENT_FACTORY
        ):
            self._breach(node, f"{client} built outside docchat_core.clients")
            return
        if not _within(self.module, CLIENT_FACTORY) or client == OPENAI_CLIENT:
            return
        required = ["connection_timeout"]
        if not _within(client, COSMOS):
            required.append("read_timeout")
        missing = [name for name in required if name not in keywords]
        if missing:
            self._breach(
                node, f"{client} built without {' or '.join(name + '=' for name in missing)}"
            )


def client_class(qualified: str) -> str | None:
    """The client class a call constructs, directly or through a `from_*` classmethod."""
    if qualified == OPENAI_CLIENT:
        return qualified
    if not _within_any(qualified, CLIENT_PACKAGES):
        return None
    head, _, last = qualified.rpartition(".")
    if last.endswith("Client"):
        return qualified
    if last.startswith("from_") and head.rpartition(".")[2].endswith("Client"):
        return head
    return None


def check_file(path: Path, display: str, module: str | None) -> list[Breach]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
    except SyntaxError as error:
        return [Breach(display, error.lineno or 0, f"can't parse: {error.msg}")]
    checker = _ModuleChecker(display, module)
    checker.visit(tree)
    return checker.breaches


def source_files(root: Path) -> Iterator[tuple[Path, str | None]]:
    """Yield every file the check parses with its module name (None outside a member's src)."""
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    members = config.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    for member in members:
        source = root / member / "src"
        if source.is_dir():
            for path in sorted(source.rglob("*.py")):
                yield path, module_name(source, path)
    for extra in EXTRA_PATHS:
        target = root / extra
        if target.is_dir():
            for path in sorted(target.rglob("*.py")):
                yield path, None
        elif target.is_file():
            yield target, None


def check_repository(root: Path) -> list[Breach]:
    breaches: list[Breach] = []
    for path, module in source_files(root):
        breaches.extend(check_file(path, path.relative_to(root).as_posix(), module))
    return breaches


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check outbound clients and contract 6.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    try:
        breaches = check_repository(args.root)
    except (OSError, tomllib.TOMLDecodeError) as error:
        sys.stdout.write(f"check_outbound_clients: {error}\n")
        return 1
    for breach in breaches:
        sys.stdout.write(f"{breach}\n")
    if breaches:
        return 1
    sys.stdout.write("every outbound client has its timeouts and import contract 6 holds\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
