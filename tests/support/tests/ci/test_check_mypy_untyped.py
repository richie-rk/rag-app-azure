"""T-517: `check_mypy_untyped.py` on fixture `pyproject.toml` files (01-repository.md 5.3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_CI = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_mypy_untyped  # noqa: E402

UNTYPED_BLOCK = """
[tool.mypy]
strict = true

[[tool.mypy.overrides]]
module = {modules}
follow_untyped_imports = true
"""


def _run(tmp_path: Path, text: str) -> int:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(text, encoding="utf-8")
    return check_mypy_untyped.main(["--pyproject", str(pyproject)])


def test_t517_the_committed_pyproject_passes() -> None:
    assert check_mypy_untyped.main(["--pyproject", str(REPOSITORY_ROOT / "pyproject.toml")]) == 0


def test_t517_third_party_modules_in_the_one_block_pass(tmp_path: Path) -> None:
    modules = '["unstructured_inference.*", "pyodbc.*"]'

    assert _run(tmp_path, UNTYPED_BLOCK.format(modules=modules)) == 0


@pytest.mark.parametrize("module", ["docchat_core.*", "docchat_api", "docchat_testing.fakes"])
def test_t517_a_first_party_module_in_the_block_fails(
    tmp_path: Path, module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    modules = f'["unstructured_inference.*", "{module}"]'

    assert _run(tmp_path, UNTYPED_BLOCK.format(modules=modules)) == 1
    assert f"names the first-party module {module}" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["follow_untyped_imports", "ignore_missing_imports"])
def test_t517_a_second_block_relaxing_untyped_imports_fails(
    tmp_path: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    text = UNTYPED_BLOCK.format(modules='["unstructured_inference.*"]') + (
        f'\n[[tool.mypy.overrides]]\nmodule = ["somelib.*"]\n{key} = true\n'
    )

    assert _run(tmp_path, text) == 1
    assert f"override block 2 (somelib.*) sets {key}" in capsys.readouterr().out


def test_t517_a_second_block_with_other_settings_passes(tmp_path: Path) -> None:
    text = UNTYPED_BLOCK.format(modules='["unstructured_inference.*"]') + (
        '\n[[tool.mypy.overrides]]\nmodule = ["somelib.*"]\nwarn_unreachable = false\n'
    )

    assert _run(tmp_path, text) == 0


def test_a_single_module_string_is_read_like_a_list(tmp_path: Path) -> None:
    assert _run(tmp_path, UNTYPED_BLOCK.format(modules='"docchat_ops.cli"')) == 1


def test_an_unreadable_pyproject_fails(tmp_path: Path) -> None:
    assert _run(tmp_path, "[tool.mypy\n") == 1
