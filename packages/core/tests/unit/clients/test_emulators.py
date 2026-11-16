from __future__ import annotations

import re
from pathlib import Path

from docchat_core.clients.emulators import (
    AZURITE_CONNECTION_STRING,
    COSMOS_EMULATOR_KEY,
    LOCAL_STUB_BEARER_TOKEN,
)


def _repository_file(name: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / name).is_file():
            return parent / name
    raise FileNotFoundError(name)


def test_azurite_is_reached_through_the_development_storage_shortcut() -> None:
    assert AZURITE_CONNECTION_STRING == "UseDevelopmentStorage=true"


def test_the_cosmos_emulator_key_is_the_published_one_the_secret_scan_allows() -> None:
    allowlist = _repository_file(".gitleaks.toml").read_text(encoding="utf-8")
    patterns = re.findall(r"'''(.+?)'''", allowlist)

    assert any(re.fullmatch(pattern, COSMOS_EMULATOR_KEY) for pattern in patterns)


def test_the_stub_token_is_a_placeholder() -> None:
    assert LOCAL_STUB_BEARER_TOKEN == "local-emulator"
