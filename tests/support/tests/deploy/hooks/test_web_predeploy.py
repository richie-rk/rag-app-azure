"""Unit test of the `web` service's azd `predeploy` hook (T-462)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[5]


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_script("scripts/deploy/hooks/web_predeploy.py", "docchat_script_web_predeploy")


def test_t462_web_predeploy_exits_1_with_azd_web_deploy_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert hook.main() == 1

    line = json.loads(capsys.readouterr().err.strip())
    assert line["code"] == "azd_web_deploy_refused"
    assert line["message"] == (
        "Deploy the web client with scripts/deploy/deploy_web.py, which pins the Static Web Apps "
        "CLI."
    )
    assert json.loads(summary.read_text()) == line
