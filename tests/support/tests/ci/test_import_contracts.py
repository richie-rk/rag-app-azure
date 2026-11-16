"""The import contracts' fixtures (T-513; `01-repository.md` section 2.4; `84-testing.md` section
10.1): a `docchat_core` module importing `docchat_api` breaks contract 1, a module other than
`docchat_core.sql.engine` importing `pyodbc` breaks contract 5, and a route module importing
`azure.identity.aio` breaks contract 6, which `check_outbound_clients.py` checks because
import-linter can't express it.

The first two run `lint-imports` itself, with the root `pyproject.toml`'s contracts unchanged,
over a fixture tree of the six root packages; the lint job runs the same contracts over the
repository (`uv run --frozen lint-imports`, the step carrying T-513).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
SCRIPTS_CI: Final = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_outbound_clients  # noqa: E402

LINT_IMPORTS_TIMEOUT_SECONDS: Final = 60


def contracts() -> dict[str, Any]:
    """`[tool.importlinter]` of the root `pyproject.toml`."""
    loaded = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    found: dict[str, Any] = loaded["tool"]["importlinter"]
    return found


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "[" + ", ".join(toml_value(item) for item in value) + "]"


def fixture_tree(root: Path, modules: dict[str, str]) -> Path:
    """The six root packages, the fixture modules and a `pyproject.toml` with the repository's
    contracts; returns that file."""
    config = contracts()
    source = root / "src"
    roots = tuple(config["root_packages"])
    # Every first-party module a contract names exists, as import-linter requires.
    named = {
        str(name)
        for contract in config["contracts"]
        for key in ("source_modules", "forbidden_modules", "modules")
        for name in contract.get(key, [])
        if str(name).split(".")[0] in roots
    }
    for package in sorted(set(roots) | named):
        directory = source.joinpath(*package.split("."))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    # Every ignored import exists too, since import-linter refuses an ignore that matches none.
    ignored = {
        importer.strip(): f"import {imported.strip()}\n"
        for contract in config["contracts"]
        for line in contract.get("ignore_imports", [])
        for importer, imported in [str(line).split("->")]
    }
    for module, body in {**ignored, **modules}.items():
        path = source.joinpath(*module.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in path.relative_to(source).parents:
            if parent != Path():
                init = source / parent / "__init__.py"
                if not init.exists():
                    init.write_text("", encoding="utf-8")
        path.write_text(body, encoding="utf-8")
    lines = ["[tool.importlinter]"]
    lines += [f"{key} = {toml_value(value)}" for key, value in config.items() if key != "contracts"]
    for contract in config["contracts"]:
        lines.append("\n[[tool.importlinter.contracts]]")
        lines += [f"{key} = {toml_value(value)}" for key, value in contract.items()]
    pyproject = root / "pyproject.toml"
    pyproject.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pyproject


def lint_imports(pyproject: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert executable is not None, "lint-imports isn't installed beside this interpreter"
    environment = {**os.environ, "PYTHONPATH": str(pyproject.parent / "src")}
    return subprocess.run(  # noqa: S603 - the installed tool with fixed arguments
        [executable, "--config", str(pyproject), "--no-cache"],
        cwd=pyproject.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=LINT_IMPORTS_TIMEOUT_SECONDS,
    )


def contract_name(number: int) -> str:
    (name,) = [
        str(contract["name"])
        for contract in contracts()["contracts"]
        if str(contract["name"]).startswith(f"{number} ")
    ]
    return name


def test_t513_the_fixture_tree_without_a_breach_keeps_every_contract(tmp_path: Path) -> None:
    result = lint_imports(fixture_tree(tmp_path, {}))

    assert result.returncode == 0, result.stdout + result.stderr


def test_t513_a_core_module_importing_the_api_breaks_contract_1(tmp_path: Path) -> None:
    modules = {"docchat_core.fixture": "import docchat_api\n"}

    result = lint_imports(fixture_tree(tmp_path, modules))

    assert result.returncode == 1
    assert f"{contract_name(1)} BROKEN" in result.stdout


def test_t513_pyodbc_outside_the_sql_engine_breaks_contract_5(tmp_path: Path) -> None:
    modules = {"docchat_api.fixture": "import pyodbc\n"}

    result = lint_imports(fixture_tree(tmp_path, modules))

    assert result.returncode == 1
    assert f"{contract_name(5)} BROKEN" in result.stdout


def test_t513_a_route_module_importing_azure_identity_breaks_contract_6(tmp_path: Path) -> None:
    path = tmp_path / "me.py"
    path.write_text("from azure.identity.aio import ManagedIdentityCredential\n", "utf-8")

    breaches = check_outbound_clients.check_file(path, "me.py", "docchat_api.routes.me")

    assert breaches
    assert any("azure.identity" in str(breach) for breach in breaches)
