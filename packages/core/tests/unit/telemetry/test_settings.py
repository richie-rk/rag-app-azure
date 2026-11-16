from __future__ import annotations

import sys

import pytest

from docchat_core.settings import LocalSettings, SettingsValidationError, load_settings
from docchat_core.telemetry.settings import (
    TEST_PLUGIN_MODULE,
    TelemetryLogLevel,
    TelemetrySettings,
    check_telemetry_log_level,
)

pytestmark = pytest.mark.usefixtures("settings_env")

_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://westeurope-5.in.applicationinsights.azure.com/"
)


@pytest.fixture(autouse=True)
def _no_connection_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)


def test_setting_names() -> None:
    assert TelemetrySettings.setting_names() == {
        "telemetry_sampling_ratio": "DOCCHAT_TELEMETRY_SAMPLING_RATIO",
        "telemetry_log_level": "DOCCHAT_TELEMETRY_LOG_LEVEL",
        "telemetry_loop_lag_interval_ms": "DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS",
        "build_commit": "DOCCHAT_BUILD_COMMIT",
        "applicationinsights_connection_string": "APPLICATIONINSIGHTS_CONNECTION_STRING",
    }


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)

    settings = TelemetrySettings()

    assert settings.telemetry_sampling_ratio == 1.0
    assert settings.telemetry_log_level is TelemetryLogLevel.INFO
    assert settings.telemetry_loop_lag_interval_ms == 500
    assert settings.build_commit == _COMMIT
    assert settings.applicationinsights_connection_string is None


def test_reads_every_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DOCCHAT_TELEMETRY_SAMPLING_RATIO", "0.25")
    monkeypatch.setenv("DOCCHAT_TELEMETRY_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS", "100")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", _CONNECTION_STRING)

    settings = TelemetrySettings()

    assert settings.telemetry_sampling_ratio == 0.25
    assert settings.telemetry_log_level is TelemetryLogLevel.WARNING
    assert settings.telemetry_loop_lag_interval_ms == 100
    assert settings.applicationinsights_connection_string == _CONNECTION_STRING


def test_an_empty_connection_string_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

    assert TelemetrySettings().applicationinsights_connection_string is None


@pytest.mark.parametrize("ratio", ["0.1", "1.0", "1", "0.5"])
def test_the_sampling_ratio_range_ends_pass(monkeypatch: pytest.MonkeyPatch, ratio: str) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DOCCHAT_TELEMETRY_SAMPLING_RATIO", ratio)

    assert TelemetrySettings().telemetry_sampling_ratio == float(ratio)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_TELEMETRY_SAMPLING_RATIO", "0.09"),
        ("DOCCHAT_TELEMETRY_SAMPLING_RATIO", "1.01"),
        ("DOCCHAT_TELEMETRY_SAMPLING_RATIO", "nan"),
        ("DOCCHAT_TELEMETRY_SAMPLING_RATIO", "half"),
        ("DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS", "99"),
        ("DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS", "5001"),
        ("DOCCHAT_TELEMETRY_LOG_LEVEL", "info"),
        ("DOCCHAT_TELEMETRY_LOG_LEVEL", "CRITICAL"),
        ("DOCCHAT_BUILD_COMMIT", "0123456789ABCDEF0123456789ABCDEF01234567"),
        ("DOCCHAT_BUILD_COMMIT", "0123456"),
    ],
)
def test_a_value_outside_its_range_names_the_setting(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv(name, value)

    with pytest.raises(SettingsValidationError) as caught:
        load_settings(TelemetrySettings)

    assert caught.value.settings == (name,)
    assert value not in caught.value.record()


def test_the_build_commit_is_required() -> None:
    with pytest.raises(SettingsValidationError) as caught:
        load_settings(TelemetrySettings)

    assert caught.value.settings == ("DOCCHAT_BUILD_COMMIT",)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_every_level_passes_in_local_emulator_mode(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DOCCHAT_TELEMETRY_LOG_LEVEL", level)
    monkeypatch.setenv("DOCCHAT_LOCAL_EMULATORS", "true")

    check_telemetry_log_level(TelemetrySettings(), LocalSettings())


@pytest.mark.parametrize("level", ["INFO", "WARNING", "ERROR"])
def test_levels_above_debug_pass_outside_local_emulator_mode(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DOCCHAT_TELEMETRY_LOG_LEVEL", level)

    check_telemetry_log_level(TelemetrySettings(), LocalSettings())


def test_check_9_refuses_debug_outside_local_emulator_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    monkeypatch.setenv("DOCCHAT_TELEMETRY_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DOCCHAT_LOCAL_EMULATORS", "false")

    with pytest.raises(SettingsValidationError) as caught:
        check_telemetry_log_level(TelemetrySettings(), LocalSettings())

    assert caught.value.code is None
    assert caught.value.settings == ("DOCCHAT_TELEMETRY_LOG_LEVEL",)


def test_strict_follows_the_test_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", _COMMIT)
    settings = TelemetrySettings()

    assert TEST_PLUGIN_MODULE in sys.modules
    assert settings.strict

    monkeypatch.delitem(sys.modules, TEST_PLUGIN_MODULE)
    assert not settings.strict


def test_strict_is_not_a_setting() -> None:
    assert "strict" not in TelemetrySettings.model_fields
