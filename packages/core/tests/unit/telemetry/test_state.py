from __future__ import annotations

import pytest

from docchat_core.telemetry import state
from docchat_core.telemetry.state import (
    SERVICE_DIMENSIONS,
    SERVICE_NAMES,
    STATE,
    is_strict,
    service_dimension,
    set_state,
)


def test_test_mode_is_on_while_the_test_plugin_is_loaded() -> None:
    assert is_strict()


def test_the_service_dimensions() -> None:
    assert SERVICE_NAMES == ("docchat-api", "docchat-ingestion")
    assert dict(SERVICE_DIMENSIONS) == {"docchat-api": "api", "docchat-ingestion": "ingestion"}


def test_set_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(STATE, "strict", True)
    monkeypatch.setattr(STATE, "service", None)
    assert service_dimension() is None

    set_state(strict=False, service="docchat-ingestion")

    assert not state.is_strict()
    assert service_dimension() == "ingestion"
