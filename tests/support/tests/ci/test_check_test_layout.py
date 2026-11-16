"""T-515: `check_test_layout.py` on fixture workspaces (01-repository.md section 5.5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_CI = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_test_layout  # noqa: E402

PYPROJECT = """
[tool.uv.workspace]
members = ["packages/core", "db"]

[tool.docchat.test-layout]
exempt = [
{exempt}
]
"""


def _write(root: Path, path: str, text: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _workspace(root: Path, exempt: str = "") -> Path:
    _write(root, "pyproject.toml", PYPROJECT.format(exempt=exempt))
    for module in (
        "packages/core/src/docchat_core/__init__.py",
        "packages/core/src/docchat_core/__main__.py",
        "packages/core/src/docchat_core/py.typed",
        "packages/core/src/docchat_core/clock.py",
        "packages/core/src/docchat_core/sql/__init__.py",
        "packages/core/src/docchat_core/sql/engine.py",
        "packages/core/tests/unit/test_clock.py",
        "packages/core/tests/unit/sql/test_engine.py",
        "db/migrations/env.py",
    ):
        _write(root, module)
    return root


def test_t515_a_workspace_with_every_mirrored_test_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)

    assert check_test_layout.main(["--root", str(root)]) == 0
    assert "every module has its mirrored unit test" in capsys.readouterr().out


def test_t515_a_module_without_its_test_fails_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    _write(root, "packages/core/src/docchat_core/sql/statements.py")

    assert check_test_layout.main(["--root", str(root)]) == 1
    assert (
        "packages/core/src/docchat_core/sql/statements.py: no unit test; expected "
        "packages/core/tests/unit/sql/test_statements.py"
    ) in capsys.readouterr().out


def test_t515_a_test_in_the_wrong_folder_does_not_count(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write(root, "packages/core/src/docchat_core/auth/tokens.py")
    _write(root, "packages/core/tests/unit/test_tokens.py")

    missing, _ = check_test_layout.missing_tests(root)

    assert missing == [
        (
            "packages/core/src/docchat_core/auth/tokens.py",
            "packages/core/tests/unit/auth/test_tokens.py",
        )
    ]


def test_t515_a_module_exempt_with_a_reason_passes(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        '  { path = "packages/core/src/docchat_core/shim.py", reason = "two-line entry shim" },',
    )
    _write(root, "packages/core/src/docchat_core/shim.py")

    assert check_test_layout.main(["--root", str(root)]) == 0


def test_t515_an_exemption_without_a_reason_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path, '  { path = "packages/core/src/docchat_core/shim.py" },')
    _write(root, "packages/core/src/docchat_core/shim.py")

    assert check_test_layout.main(["--root", str(root)]) == 1
    assert "exempt entry packages/core/src/docchat_core/shim.py has no reason" in (
        capsys.readouterr().out
    )


def test_t515_the_repository_passes() -> None:
    assert check_test_layout.main(["--root", str(REPOSITORY_ROOT)]) == 0
