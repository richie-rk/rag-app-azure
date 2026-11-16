"""`apps/api/gunicorn.conf.py` (`11-api.md` section 1.15; `81-config.md` section 6.1)."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

CONFIG = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


@pytest.fixture
def tiktoken(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    loaded: list[str] = []
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=loaded.append))
    return loaded


def test_the_configuration_comes_from_the_api_settings(
    api_env: pytest.MonkeyPatch, tiktoken: list[str]
) -> None:
    api_env.setenv("DOCCHAT_API_WORKERS", "2")
    api_env.setenv("DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS", "100")

    config: dict[str, Any] = runpy.run_path(str(CONFIG))

    assert config["bind"] == "0.0.0.0:8000"
    assert config["workers"] == 2
    assert config["worker_class"] == "docchat_api.http.worker.DocChatUvicornWorker"
    assert config["graceful_timeout"] == 100
    assert config["timeout"] == 120
    assert config["accesslog"] is None
    assert config["errorlog"] == "-"
    assert config["loglevel"] == "warning"
    assert config["capture_output"] is False
    assert tiktoken == ["o200k_base", "o200k_base"]


def test_a_refused_start_up_exits_78_before_forking(
    api_env: pytest.MonkeyPatch, tiktoken: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    api_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "prod")

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(CONFIG))

    assert exited.value.code == 78
    record = json.loads(capsys.readouterr().err)
    assert record["settings"] == ["DOCCHAT_AUTH_TEST_CLIENT_ID"]
    assert tiktoken == []


def test_an_encoding_that_does_not_load_exits_78(
    api_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> None:
        raise FileNotFoundError(name)

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=missing))

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(CONFIG))

    assert exited.value.code == 78
    assert json.loads(capsys.readouterr().err)["settings"] == [
        "DOCCHAT_OPENAI_CHAT_ENCODING",
        "DOCCHAT_OPENAI_SMALL_ENCODING",
    ]
