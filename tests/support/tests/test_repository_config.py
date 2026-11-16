"""T-511, the repository-configuration test (`01-repository.md` section 1.13).

It reads `docs/spec/01-repository.md` with the standard library (YAML with PyYAML, which
`pre-commit` brings into the development environment) and checks the repository against it: the
tree of section 1 and the absence of unlisted top-level subpackages, the workspace members and
their declarations (sections 3.1 and 3.3), the root files of section 1.12, the Ruff, mypy, pytest,
coverage and import-linter tables (sections 2.4, 3.2 and 5), the pre-commit revisions and the
gitleaks allowlist (section 5.7), the Dependabot file (section 8.2), the `uv export` output for the
ingestion member (section 3.2), fixture packages that break each import contract (section 2.4),
and the pins of section 10 against the files that apply them. A difference names the file, the
key and both values.

The build's known deviations from the specification are marked `xfail(strict=True)` with their
reason, so each one fails the test the day it is resolved.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
SPEC: Final = (REPOSITORY_ROOT / "docs" / "spec" / "01-repository.md").read_text(encoding="utf-8")

MYPY_DEVIATION: Final = (
    "known deviation (mypy `explicit_package_bases = true`): the build adds "
    "explicit_package_bases and mypy_path to [tool.mypy]"
)
LATER_STAGES: Final = (
    "the tree of section 1 is the finished repository; its paths arrive with the stage that "
    "delivers them (90-build-order.md section 1.2 item 3), so until stage 6 some are absent"
)
_HEX40: Final = re.compile(r"\A[0-9a-f]{40}\Z")


# --- Reading the specification ---------------------------------------------------------------


def section(heading: str) -> str:
    """The text under a heading of `01-repository.md`, up to the next heading of its level or
    above."""
    lines = SPEC.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        marks = len(line) - len(line.lstrip("#"))
        if line.startswith("#") and marks <= level and line[marks : marks + 1] == " ":
            break
        body.append(line)
    return "\n".join(body)


def code_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)


def table_rows(text: str) -> list[list[str]]:
    """The body rows of every table in `text`, cells stripped; escaped `\\|` stays in its cell."""
    lines = text.splitlines()
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        if not line.startswith("|") or set(line.strip()) <= set("|-: "):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if following.startswith("|") and set(following.strip()) <= set("|-: "):
            continue  # the header row
        rows.append([cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])])
    return rows


def code_names(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


_TREE_NAME: Final = re.compile(r"\A[\w.@][\w.@/-]*\Z")
_TREE_SKIP: Final = re.compile(r"[<*]|NNNN|RNN")
_TREE_DRAWING: Final = re.compile(r"[│├└─\s]*")


def tree_paths(block: str) -> list[str]:
    """The paths a tree block names in its name column; a name with a placeholder (`<file>`,
    `NNNN`) stands for files the tree can't list and is skipped."""
    paths: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in block.splitlines():
        drawing = _TREE_DRAWING.match(line).group(0)  # type: ignore[union-attr]
        column = len(drawing)
        # Only the root line and branch lines (`├──`, `└──`) name entries; the others continue
        # the description column.
        if column and "──" not in drawing:
            continue
        names: list[str] = []
        for token in re.split(r"\s{2,}", line[column:].strip()):
            if not _TREE_NAME.match(token):
                break
            names.append(token)
            # What follows a directory on its line describes its contents.
            if token.endswith("/"):
                break
        if not names:
            continue
        depth = column // 4
        stack = [entry for entry in stack if entry[0] < depth]
        parent = stack[-1][1] if stack else ""
        for name in names:
            path = parent + name
            if not _TREE_SKIP.search(name):
                paths.append(path)
        if names[-1].endswith("/"):
            stack.append((depth, parent + names[-1]))
    return paths


def _tree_blocks() -> Iterator[tuple[str, str]]:
    for heading in ("### 1.1 ", "### 1.2 ", "### 1.3 ", "### 1.4 ", "### 1.5 ", "### 1.6 ",
                    "### 1.7 ", "### 1.8 ", "### 1.9 ", "### 1.10 ", "### 1.11 "):  # fmt: skip
        for block in code_blocks(section(heading), "text"):
            yield heading.strip("# "), block


def specified_tree() -> list[str]:
    # Section 1.1's tree is rooted at the checkout directory itself.
    paths = [
        path.removeprefix("docchat/") for _, block in _tree_blocks() for path in tree_paths(block)
    ]
    return [path for path in dict.fromkeys(paths) if path]


def spec_toml(heading: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for block in code_blocks(section(heading), "toml"):
        merged.update(tomllib.loads(block))
    return merged


def pyproject() -> dict[str, Any]:
    return tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _table(document: dict[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _differs(file: str, key: str, expected: object, actual: object) -> str:
    return f"{file}: {key}: specified {expected!r}, found {actual!r}"


# --- The tree --------------------------------------------------------------------------------


def test_t511_the_tree_reader_reads_names_and_nesting() -> None:
    block = (
        "apps/api/\n"
        "├── pyproject.toml                distribution docchat-api (section 3.3)\n"
        "├── src/docchat_api/\n"
        "│   ├── __init__.py  py.typed\n"
        "│   ├── http/                     problems.py, routing.py (DocChatRoute),\n"
        "│   │                             (DocChatUvicornWorker, DocChatServer)\n"
        "│   └── <placeholder>.py          skipped\n"
        "└── tests/\n"
        "    └── unit/                     mirrors src/docchat_api module for module\n"
    )

    assert tree_paths(block) == [
        "apps/api/",
        "apps/api/pyproject.toml",
        "apps/api/src/docchat_api/",
        "apps/api/src/docchat_api/__init__.py",
        "apps/api/src/docchat_api/py.typed",
        "apps/api/src/docchat_api/http/",
        "apps/api/tests/",
        "apps/api/tests/unit/",
    ]


def test_t511_the_top_level_directories_of_section_1_1_exist() -> None:
    (block,) = code_blocks(section("### 1.1 "), "text")
    top = [path.removeprefix("docchat/") for path in tree_paths(block)]
    listed = [path for path in top if path.count("/") == 1 and path.endswith("/")]

    missing = [path for path in listed if not (REPOSITORY_ROOT / path).is_dir()]

    assert listed
    assert missing == []


def test_t511_no_top_level_directory_outside_section_1_1() -> None:
    (block,) = code_blocks(section("### 1.1 "), "text")
    listed = {
        path.removeprefix("docchat/").rstrip("/")
        for path in tree_paths(block)
        if path.removeprefix("docchat/").count("/") == 1
    }
    present = {
        entry.name
        for entry in REPOSITORY_ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    }

    assert present - listed == set()


@pytest.mark.parametrize(
    ("package", "source"),
    [
        ("docchat_core", "packages/core/src/docchat_core/"),
        ("docchat_api", "apps/api/src/docchat_api/"),
        ("docchat_ingestion", "apps/ingestion/src/docchat_ingestion/"),
    ],
)
def test_t511_no_unlisted_top_level_subpackage(package: str, source: str) -> None:
    listed = {
        path.removeprefix(source).strip("/")
        for path in specified_tree()
        if path.startswith(source) and path.endswith("/") and path != source
    }
    present = {
        entry.name
        for entry in (REPOSITORY_ROOT / source).iterdir()
        if entry.is_dir() and entry.name != "__pycache__"
    }

    assert present - listed == set(), f"{package}: subpackages outside section 1"


def test_t511_the_spike_module_goes_once_the_starters_arrive() -> None:
    package = REPOSITORY_ROOT / "apps" / "ingestion" / "src" / "docchat_ingestion"

    assert not ((package / "starters.py").exists() and (package / "spike.py").exists())


@pytest.mark.xfail(strict=True, reason=LATER_STAGES)
def test_t511_every_path_of_the_tree_exists() -> None:
    missing = [path for path in specified_tree() if not (REPOSITORY_ROOT / path).exists()]

    assert missing == []


# --- Root files ------------------------------------------------------------------------------


def test_t511_the_root_files_of_section_1_12_exist() -> None:
    names = [name for row in table_rows(section("### 1.12 ")) for name in code_names(row[0])]

    assert len(names) >= 19
    assert [name for name in names if not (REPOSITORY_ROOT / name).exists()] == []


def _exact_block(label: str) -> str:
    text = section("### 1.12 ")
    after = text[text.index(label) :]
    (block,) = code_blocks(after, r"\w+")[:1]
    return block


@pytest.mark.parametrize(
    ("name", "label"),
    [
        (".gitignore", "`.gitignore`, exactly:"),
        (".dockerignore", "`.dockerignore`, exactly,"),
        (".env.example", "`.env.example`, exactly"),
        (".editorconfig", "`.editorconfig`:"),
        (".gitattributes", "`.gitattributes`:"),
    ],
)
def test_t511_the_root_files_hold_their_specified_content(name: str, label: str) -> None:
    expected = _exact_block(label)

    assert (REPOSITORY_ROOT / name).read_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize(
    ("name", "content"), [(".python-version", "3.13"), (".node-version", "24.21.0")]
)
def test_t511_the_version_files(name: str, content: str) -> None:
    rows = {
        code_names(row[0])[0]: row[1]
        for row in table_rows(section("### 1.12 "))
        if code_names(row[0])
    }

    assert rows[name] == f"`{content}`"
    assert (REPOSITORY_ROOT / name).read_text(encoding="utf-8").strip() == content


# --- The workspace ----------------------------------------------------------------------------


def _members() -> list[list[str]]:
    return [row for row in table_rows(section("### 3.1 ")) if row[0].startswith("`")]


def test_t511_the_workspace_has_exactly_the_seven_members() -> None:
    specified = [code_names(row[0])[0] for row in _members()]

    assert _table(pyproject(), "tool.uv.workspace.members") == specified
    assert len(specified) == 7


@pytest.mark.parametrize("row", _members(), ids=lambda row: code_names(row[0])[0])
def test_t511_each_member_declares_what_section_3_3_requires(row: list[str]) -> None:
    path, distribution = code_names(row[0])[0], code_names(row[1])[0]
    member = tomllib.loads((REPOSITORY_ROOT / path / "pyproject.toml").read_text(encoding="utf-8"))
    file = f"{path}/pyproject.toml"
    (declaration,) = code_blocks(section("### 3.3 "), "toml")
    # The block's `dependencies = [ ... ]` is a placeholder, so only its build table is parsed.
    build = tomllib.loads(declaration.split("[project]")[0])["build-system"]
    expected_build = None if path == "db" else build
    problems = [
        _differs(file, key, expected, actual)
        for key, expected, actual in (
            ("project.name", distribution, _table(member, "project.name")),
            ("project.version", "0.0.0", _table(member, "project.version")),
            ("project.requires-python", ">=3.13,<3.14", _table(member, "project.requires-python")),
            ("build-system", expected_build, member.get("build-system")),
        )
        if expected != actual
    ]
    script = code_names(row[5])
    if script:
        scripts = _table(member, "project.scripts") or {}
        if script[0] not in scripts:
            problems.append(_differs(file, "project.scripts", script[0], sorted(scripts)))

    assert problems == []


# --- Tool tables ------------------------------------------------------------------------------

_TABLES: Final = (
    ("### 3.2 ", "project"),
    ("### 3.2 ", "dependency-groups"),
    ("### 3.2 ", "tool.uv"),
    ("### 3.2 ", "tool.uv.sources"),
    ("### 2.4 ", "tool.importlinter"),
    ("### 5.2 ", "tool.ruff"),
    ("### 5.3 ", "tool.pydantic-mypy"),
    ("### 5.4 ", "tool.pytest.ini_options"),
    ("### 5.5 ", "tool.coverage"),
    ("### 5.5 ", "tool.docchat.test-layout"),
)


@pytest.mark.parametrize(
    ("heading", "table"),
    [pytest.param(heading, table, id=table) for heading, table in _TABLES],
)
def test_t511_the_root_tables_equal_the_specified_ones(heading: str, table: str) -> None:
    expected = _table(spec_toml(heading), table)

    assert expected is not None
    assert _table(pyproject(), table) == expected, _differs("pyproject.toml", table, expected, "…")


def _mypy_keys() -> list[Any]:
    specified = spec_toml("### 5.3 ")["tool"]["mypy"]
    actual = pyproject()["tool"]["mypy"]
    # The overrides block grows by rule (section 5.3, ADR 0128); its own test checks that rule.
    keys = [key for key in dict.fromkeys([*specified, *actual]) if key != "overrides"]
    extras = set(actual) - set(specified)
    return [
        pytest.param(key, marks=pytest.mark.xfail(strict=True, reason=MYPY_DEVIATION))
        if key in extras and key in {"explicit_package_bases", "mypy_path"}
        else key
        for key in keys
    ]


@pytest.mark.parametrize("key", _mypy_keys())
def test_t511_the_mypy_table_equals_section_5_3(key: str) -> None:
    specified = spec_toml("### 5.3 ")["tool"]["mypy"]
    actual = pyproject()["tool"]["mypy"]

    assert actual.get(key) == specified.get(key), _differs(
        "pyproject.toml", f"tool.mypy.{key}", specified.get(key), actual.get(key)
    )


def test_t511_the_mypy_overrides_follow_section_5_3() -> None:
    """One block that follows untyped imports, listing the modules the type check reports as
    `import-untyped`: the spec's known entries plus third-party modules only (ADR 0128)."""
    (specified,) = spec_toml("### 5.3 ")["tool"]["mypy"]["overrides"]
    (actual,) = pyproject()["tool"]["mypy"]["overrides"]
    assert set(actual) == set(specified), _differs(
        "pyproject.toml", "tool.mypy.overrides keys", sorted(specified), sorted(actual)
    )
    assert actual["follow_untyped_imports"] is True
    modules = actual["module"]
    assert set(specified["module"]) <= set(modules)
    assert modules == sorted(modules)
    assert not [module for module in modules if module.startswith("docchat_")]


# --- pre-commit, gitleaks, Dependabot ----------------------------------------------------------


def _hook_tags() -> dict[str, str]:
    tags: dict[str, str] = {}
    for row in table_rows(section("### 5.7 ")):
        match = re.match(r"`([\w.-]+/[\w.-]+)` (\S+)", row[0])
        if match is not None:
            tags[f"https://github.com/{match.group(1)}"] = match.group(2)
    return tags


def test_t511_every_pre_commit_rev_is_the_frozen_sha_of_its_tag() -> None:
    path = REPOSITORY_ROOT / ".pre-commit-config.yaml"
    text = path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    frozen = dict(re.findall(r"^\s*rev: ([0-9a-f]+)\s+# frozen: (\S+)$", text, flags=re.MULTILINE))
    expected = _hook_tags()
    problems: list[str] = []
    if config.get("minimum_pre_commit_version") != "4.7.0":
        problems.append(
            _differs(
                path.name,
                "minimum_pre_commit_version",
                "4.7.0",
                config.get("minimum_pre_commit_version"),
            )
        )
    remote = [repo for repo in config["repos"] if repo["repo"] != "local"]
    for repo in remote:
        rev = str(repo["rev"])
        if not _HEX40.match(rev):
            problems.append(_differs(path.name, f"{repo['repo']} rev", "a 40-hex commit", rev))
        if frozen.get(rev) != expected.get(repo["repo"]):
            problems.append(
                _differs(
                    path.name,
                    f"{repo['repo']} frozen tag",
                    expected.get(repo["repo"]),
                    frozen.get(rev),
                )
            )
    if [repo["repo"] for repo in remote] != list(expected):
        problems.append(
            _differs(path.name, "repos", list(expected), [repo["repo"] for repo in remote])
        )

    assert problems == []


def test_t511_the_gitleaks_allowlist_holds_exactly_the_two_emulator_keys() -> None:
    config = tomllib.loads((REPOSITORY_ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert config["extend"] == {"useDefault": True}
    (allowlist,) = config["allowlists"]
    assert len(allowlist["regexes"]) == 2
    assert set(allowlist) <= {"description", "regexes"}
    assert set(config) == {"extend", "allowlists"}


def test_t511_dependabot_equals_section_8_2() -> None:
    (block,) = code_blocks(section("### 8.2 "), "yaml")
    actual = (REPOSITORY_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert yaml.safe_load(actual) == yaml.safe_load(block)


# --- The ingestion export ---------------------------------------------------------------------

TORCH_SHA256: Final = "331fa474e26e428e2e6af1143b2fdab19cee492d7d21897b8899ce1c14755662"


def test_t511_the_ingestion_export_pins_the_cpu_torch_wheel() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is not on PATH"
    exported = subprocess.run(  # noqa: S603 - a fixed command line
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--package",
            "docchat-ingestion",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    torch = [line for line in exported.splitlines() if line.startswith("torch")]

    assert torch, "requirements export: no torch line"
    assert "download.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu" in exported
    assert f"--hash=sha256:{TORCH_SHA256}" in exported
    assert not re.search(r"^nvidia-.*linux", exported, flags=re.MULTILINE)
    assert all(
        "sys_platform == 'never'" in line
        for line in exported.splitlines()
        if line.startswith("opencv-python==")
    )


# --- Import contract fixtures ------------------------------------------------------------------

_BREAKING_IMPORTS: Final = {
    "1 core imports no deployable or tool": ("docchat_core/leak.py", "import docchat_api\n"),
    "2 the two apps are independent": ("docchat_api/cross.py", "import docchat_ingestion\n"),
    "3 production code imports no test code": (
        "docchat_ingestion/test_code.py",
        "import docchat_testing\n",
    ),
    "4 telemetry and settings sit at the bottom of core": (
        "docchat_core/telemetry/upward.py",
        "import docchat_core.sql\n",
    ),
    "5 only the SQL engine imports pyodbc": ("docchat_api/driver.py", "import pyodbc\n"),
    "7 only the OpenAI client module imports httpx2": (
        "docchat_ingestion/transport.py",
        "import httpx2\n",
    ),
}


def test_t511_fixture_packages_break_each_import_contract(tmp_path: Path) -> None:
    contracts = spec_toml("### 2.4 ")["tool"]["importlinter"]
    roots = set(contracts["root_packages"])
    named = set(roots)
    for contract in contracts["contracts"]:
        for key in ("source_modules", "forbidden_modules", "modules"):
            named.update(contract.get(key, []))
    # Every first-party module a contract names exists in the fixture tree, as import-linter
    # requires; the external ones (`pyodbc`, `httpx2`) come from the environment.
    for module in sorted(name for name in named if name.split(".")[0] in roots):
        package = tmp_path.joinpath(*module.split("."))
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    # The allowed imports exist too, since import-linter refuses an ignore that matches nothing.
    for contract in contracts["contracts"]:
        for ignored in contract.get("ignore_imports", []):
            importer, imported = (part.strip() for part in ignored.split("->"))
            module = tmp_path.joinpath(*importer.split(".")).with_suffix(".py")
            module.parent.mkdir(parents=True, exist_ok=True)
            module.write_text(f"import {imported}\n", encoding="utf-8")
    for path, source in _BREAKING_IMPORTS.values():
        (tmp_path / path).write_text(source, encoding="utf-8")
    contracts_text = "\n".join(block for block in code_blocks(section("### 2.4 "), "toml"))
    (tmp_path / ".importlinter.toml").write_text(contracts_text, encoding="utf-8")
    lint_imports = Path(sys.executable).parent / "lint-imports"
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}

    result = subprocess.run(  # noqa: S603 - the workspace's own tool on fixture packages
        [str(lint_imports), "--config", str(tmp_path / ".importlinter.toml"), "--no-cache"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=300,
    )

    assert result.returncode != 0
    broken = {
        name
        for name in _BREAKING_IMPORTS
        if re.search(rf"^{re.escape(name)} BROKEN\b", result.stdout, flags=re.MULTILINE)
    }
    assert broken == set(_BREAKING_IMPORTS), result.stdout[-2000:]


# --- Section 10 pins ----------------------------------------------------------------------------


def _lock_versions() -> dict[str, str]:
    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package.get("version", "") for package in lock["package"]}


def _dev_tool_rows() -> list[tuple[str, str]]:
    names = {"Ruff": "ruff", "mypy": "mypy", "pytest": "pytest", "pytest-asyncio": "pytest-asyncio",
             "pytest-timeout": "pytest-timeout", "pytest-cov": "pytest-cov", "coverage": "coverage",
             "respx": "respx", "pre-commit": "pre-commit", "pip-audit": "pip-audit",
             "deptry": "deptry", "import-linter": "import-linter", "zizmor": "zizmor"}  # fmt: skip
    return [(names[row[0]], row[1]) for row in table_rows(section("### 10.3 ")) if row[0] in names]


@pytest.mark.parametrize(("distribution", "version"), _dev_tool_rows())
def test_t511_each_development_tool_is_pinned_and_locked_at_its_version(
    distribution: str, version: str
) -> None:
    group = pyproject()["dependency-groups"]["dev"]

    assert f"{distribution}=={version}" in group, _differs(
        "pyproject.toml", f"dev {distribution}", version, group
    )
    assert _lock_versions().get(distribution) == version, _differs(
        "uv.lock", distribution, version, _lock_versions().get(distribution)
    )


def _runtime_rows() -> list[Any]:
    rows: list[Any] = []
    for row in table_rows(section("### 10.2 ")):
        names = code_names(row[0])
        if not names or not re.match(r"\A\d", row[1]):
            continue
        distribution = names[0].split("[")[0]
        version = re.match(r"[0-9][0-9A-Za-z.]*", row[1]).group(0).rstrip(".")  # type: ignore[union-attr]
        rows.append(pytest.param(distribution, version, id=distribution))
    return rows


def _declared_lower_bounds(distribution: str) -> dict[str, str]:
    bounds: dict[str, str] = {}
    for member in ("packages/core", "apps/api", "apps/ingestion", "db", "scripts/ops",
                   "tests/support", "tests/model-stub"):  # fmt: skip
        declared = tomllib.loads(
            (REPOSITORY_ROOT / member / "pyproject.toml").read_text(encoding="utf-8")
        )["project"].get("dependencies", [])
        for requirement in declared:
            match = re.match(r"\A([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?>=([^,]+),", requirement)
            if match is not None and match.group(1).lower() == distribution.lower():
                bounds[member] = match.group(2)
    return bounds


@pytest.mark.parametrize(("distribution", "version"), _runtime_rows())
def test_t511_each_runtime_library_is_declared_from_its_confirmed_version(
    distribution: str, version: str
) -> None:
    """Members declare `>=<confirmed>,<next major>` (section 3.3); the lock may resolve a newer
    release within that range, so it must not be older."""
    declared = _declared_lower_bounds(distribution)
    locked = _lock_versions().get(distribution.lower())
    problems = [
        _differs(f"{member}/pyproject.toml", distribution, f">={version}", f">={bound}")
        for member, bound in declared.items()
        if bound != version
    ]
    if distribution == "torch" or declared:
        if locked is None:
            problems.append(_differs("uv.lock", distribution, version, None))
        elif _numeric(locked) < _numeric(version):
            problems.append(_differs("uv.lock", distribution, f">={version}", locked))

    assert problems == []


def _numeric(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.partition("+")[0])[:4])


def _workflow_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPOSITORY_ROOT / ".github" / "workflows").glob("*.yaml"))
    )


def _action_rows() -> list[tuple[str, str]]:
    text = section("### 10.6 ")
    actions = text[text.index("GitHub Actions, each pinned") :]
    return [
        (code_names(row[0])[0], row[1].split(" ")[0])
        for row in table_rows(actions)
        if code_names(row[0])
    ]


@pytest.mark.parametrize(("action", "tag"), _action_rows())
def test_t511_each_github_action_is_pinned_to_its_tag(action: str, tag: str) -> None:
    uses = re.findall(
        rf"uses: {re.escape(action)}@(\S+)\s+#\s*(\S+)", _workflow_text(), flags=re.IGNORECASE
    )
    if not uses:
        pytest.skip(f"no workflow uses {action} yet")
    problems = [
        _differs(".github/workflows", action, f"<40-hex>  # {tag}", f"{sha}  # {comment}")
        for sha, comment in uses
        if not _HEX40.match(sha) or comment != tag
    ]

    assert problems == []


def test_t511_the_scanner_images_and_tool_versions_in_the_workflows() -> None:
    workflows = _workflow_text()
    rows = {row[0]: row[1] for row in table_rows(section("### 10.6 "))}
    images = {
        "gitleaks": code_names(rows["`gitleaks/gitleaks` hook; image"])[0],
        "Trivy": code_names(rows["Trivy"])[0],
    }
    bicep = rows["Bicep CLI"].split(" ")[0]

    applied = set(re.findall(r"ghcr\.io/(?:gitleaks|aquasecurity)/\S+", workflows))
    assert applied == set(images.values()), _differs(
        ".github/workflows", "scanner images", sorted(images.values()), sorted(applied)
    )
    assert set(re.findall(r"az bicep install --version v(\S+)", workflows)) == {bicep}


def test_t511_uv_is_pinned_everywhere_it_is_applied() -> None:
    assert pyproject()["tool"]["uv"]["required-version"] == "==0.13.1"
    setups = re.findall(r'setup-uv@\S+.*?\n\s+with:\n\s+version: "([^"]+)"', _workflow_text())
    assert setups
    assert set(setups) == {"0.13.1"}


def test_t511_the_psrule_ranges() -> None:
    config = yaml.safe_load((REPOSITORY_ROOT / "ps-rule.yaml").read_text(encoding="utf-8"))

    assert config["requires"] == {
        "PSRule": ">=2.9.0 <3.0.0",
        "PSRule.Rules.Azure": ">=1.47.0 <1.48.0",
    }


def _service_images() -> list[tuple[str, str]]:
    return [(row[0], code_names(row[1])[0]) for row in table_rows(section("### 10.7 "))]


@pytest.mark.parametrize(("service", "image"), _service_images())
def test_t511_each_compose_image_is_its_pinned_digest(service: str, image: str) -> None:
    compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    images = [definition.get("image") for definition in compose["services"].values()]

    assert image in images, _differs("compose.yaml", service, image, images)


def _base_images() -> list[tuple[str, str, str]]:
    return [
        (row[0], images[0], path)
        for row in table_rows(section("### 10.1 "))
        if (images := code_names(row[1])) and "@sha256:" in images[0]
        for path in code_names(row[3])
        if path.endswith("Dockerfile")
    ]


@pytest.mark.parametrize(("component", "image", "dockerfile"), _base_images())
def test_t511_each_base_image_is_its_pinned_digest(
    component: str, image: str, dockerfile: str
) -> None:
    path = REPOSITORY_ROOT / dockerfile
    if not path.exists():
        pytest.skip(f"{dockerfile} arrives with its stage")
    sources = re.findall(r"^FROM (\S+)", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    external = [source for source in sources if "@sha256:" in source or ":" in source]

    assert external
    assert set(external) == {image}, _differs(dockerfile, component, image, external)


def _bicep_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPOSITORY_ROOT / "infra").rglob("*.bicep"))
    )


def _module_rows() -> list[tuple[str, str]]:
    return [
        (code_names(row[0])[0], row[1])
        for row in table_rows(section("### 10.9 "))
        if len(row) == 3 and code_names(row[0]) and re.match(r"\A\d+\.\d+\.\d+\Z", row[1])
    ]


@pytest.mark.parametrize(("module", "version"), _module_rows())
def test_t511_each_verified_module_reference_is_its_pinned_version(
    module: str, version: str
) -> None:
    references = set(re.findall(rf"br/public:avm/res/{re.escape(module)}:([0-9.]+)", _bicep_text()))
    if not references:
        pytest.skip(f"no template references avm/res/{module} yet")

    assert references == {version}, _differs("infra/", module, version, sorted(references))


def _api_rows() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for row in table_rows(section("### 10.9 ")):
        if len(row) != 3 or not re.match(r"\A(?:\d{4}-\d{2}-\d{2}(?:-preview)?|v1\.0)\Z", row[1]):
            continue
        names = code_names(row[0])
        provider = names[0].split("/")[0]
        for name in names:
            full = name if "." in name.split("/")[0] else f"{provider}/{name}"
            if full.startswith("Microsoft.") and not full.startswith("Microsoft.Graph"):
                cases.append((full, row[1]))
    return cases


@pytest.mark.parametrize(("resource_type", "api_version"), _api_rows())
def test_t511_each_declared_resource_type_uses_its_api_version(
    resource_type: str, api_version: str
) -> None:
    used = set(re.findall(rf"'{re.escape(resource_type)}@([0-9a-z-]+)'", _bicep_text()))
    if not used:
        pytest.skip(f"no template declares {resource_type} yet")

    assert used == {api_version}, _differs("infra/", resource_type, api_version, sorted(used))


# --- The one deptry ignore ----------------------------------------------------------------------

_MEMBERS: Final = ("packages/core", "apps/api", "apps/ingestion", "db", "scripts/ops",
                   "tests/support", "tests/model-stub")  # fmt: skip


def test_t511_only_two_members_ignore_deptry_findings() -> None:
    """Section 3.3: `docchat-ingestion`'s DEP002 for its two declared-but-unimported
    distributions and `docchat-model-stub`'s for `uvicorn` are the repository's only deptry
    ignores."""
    configured = {
        member: tomllib.loads((REPOSITORY_ROOT / member / "pyproject.toml").read_text("utf-8"))
        .get("tool", {})
        .get("deptry")
        for member in _MEMBERS
    }
    expected = {
        "per_rule_ignores": {"DEP002": ["azure-functions-runtime", "opencv-python-headless"]}
    }

    assert configured.pop("apps/ingestion") == expected
    assert configured.pop("tests/model-stub") == {"per_rule_ignores": {"DEP002": ["uvicorn"]}}
    assert configured == dict.fromkeys(configured)
    assert "deptry" not in pyproject().get("tool", {})
