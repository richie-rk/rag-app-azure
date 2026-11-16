from __future__ import annotations

import importlib
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from docchat_api.settings import (
    ApiSettings,
    ProcessSettings,
    check_encodings,
    check_models,
    load_process_settings,
)
from docchat_core.auth import AuthSettings
from docchat_core.ingestion.settings import EmbeddingModelCatalog, UploadSettings
from docchat_core.settings import (
    CoreSettings,
    LocalSettings,
    SettingsValidationError,
    StartupCheckError,
    StartupRefusalError,
    exit_on_startup_failure,
)
from docchat_core.sql.settings import SqlSettings
from docchat_core.telemetry import TelemetrySettings

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "infra").is_dir())

# Settings the platform or a library reads by their own names; no settings class reads them.
PLATFORM_READ = {
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING",
    "WEBSITE_AUTH_AAD_ALLOWED_TENANTS",
    "WEBSITES_ENABLE_APP_SERVICE_STORAGE",
    "WEBSITES_PORT",
    "WEBSITES_CONTAINER_START_TIME_LIMIT",
    "WEBSITES_CONTAINER_STOP_TIME_LIMIT",
    "WEBSITE_HEALTHCHECK_MAXPINGFAILURES",
}
API_CLASSES = (
    CoreSettings,
    AuthSettings,
    TelemetrySettings,
    SqlSettings,
    UploadSettings,
    EmbeddingModelCatalog,
    ApiSettings,
)


def _read_names() -> set[str]:
    """Every app setting the API's classes read; `DOCCHAT_BUILD_COMMIT` is the image's `ENV`."""
    names = {name for cls in API_CLASSES for name in cls.setting_names().values()}
    return names - {"DOCCHAT_BUILD_COMMIT"}


def _bicep_api_settings() -> set[str]:
    text = (ROOT / "infra" / "modules" / "compute" / "app-settings.bicep").read_text()
    block = text[
        text.index("var apiSettings = union(apiDefaults, {") : text.index(
            "})", text.index("var apiSettings")
        )
    ]
    composed = set(re.findall(r"^\s+([A-Z][A-Za-z0-9_]+):", block, re.MULTILINE))
    defaults = json.loads((ROOT / "infra" / "config" / "app-settings.json").read_text())["api"]
    return composed | set(defaults)


def test_the_api_site_composes_57_settings_and_the_classes_read_all_but_the_platform_s() -> None:
    composed = _bicep_api_settings()

    assert len(composed) == 57
    assert _read_names() == composed - PLATFORM_READ


def test_the_deploy_check_expects_the_same_57_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "deploy"))
    deploy_checks: Any = importlib.import_module("deploy_checks")
    inputs = deploy_checks.SettingsInputs(
        environment="dev",
        defaults=json.loads((ROOT / "infra" / "config" / "app-settings.json").read_text()),
        config=json.loads((ROOT / "infra" / "env" / "dev.json").read_text()),
        outputs={},
    )

    expected = set(deploy_checks.expected_settings("api", inputs))

    assert expected == _bicep_api_settings()
    assert _read_names() == expected - PLATFORM_READ


def test_the_api_settings_are_the_catalogue_rows_the_api_class_owns() -> None:
    text = (ROOT / "docs" / "spec" / "81-config.md").read_text(encoding="utf-8")
    section = text[text.index("### 2.4 Settings on the API only") : text.index("### 2.5 ")]
    rows = set(re.findall(r"^\| `(DOCCHAT_[A-Z_]+)` \|", section, re.MULTILINE))
    owned_elsewhere = {
        name for cls in (AuthSettings, UploadSettings) for name in cls.setting_names().values()
    }
    both_apps = {"DOCCHAT_SEARCH_ENDPOINT", "DOCCHAT_OPENAI_ENDPOINT"}

    assert set(ApiSettings.setting_names().values()) == (rows - owned_elsewhere) | both_apps


def test_the_owner_defaults_load(api_env: pytest.MonkeyPatch) -> None:
    defaults = json.loads((ROOT / "infra" / "config" / "app-settings.json").read_text())["api"]
    for name, value in defaults.items():
        api_env.setenv(name, value)

    settings = load_process_settings()

    assert settings.api.api_graceful_timeout_seconds == 110
    assert settings.api.api_worker_timeout_seconds == 120
    assert settings.api.openai_chat_reasoning_effort == "low"
    assert settings.api.openai_small_reasoning_effort == "none"
    assert settings.sql.sql_threads == 8


def test_load_process_settings_reads_every_class(api_env: pytest.MonkeyPatch) -> None:
    settings = load_process_settings()

    assert isinstance(settings, ProcessSettings)
    assert settings.api.api_workers == 1
    assert settings.api.cosmos_database == "docchat"
    assert settings.api.openai_chat_model == "gpt-5.6-sol"
    assert settings.api.openai_small_deployment == "chat-small"
    assert settings.auth.auth_test_client_id is not None
    assert settings.local.local_emulators is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_API_WORKERS", "0"),
        ("DOCCHAT_API_WORKERS", "17"),
        ("DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS", "29"),
        ("DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS", "111"),
        ("DOCCHAT_API_WORKER_TIMEOUT_SECONDS", "121"),
        ("DOCCHAT_SEARCH_TIMEOUT_SECONDS", "1"),
        ("DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS", "31"),
        ("DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS", "9"),
        ("DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS", "16"),
        ("DOCCHAT_COSMOS_DATABASE", "other"),
        ("DOCCHAT_OPENAI_CHAT_DEPLOYMENT", "x"),
        ("DOCCHAT_OPENAI_CHAT_MODEL", "GPT-5"),
        ("DOCCHAT_OPENAI_SMALL_ENCODING", "p50k_base"),
        ("DOCCHAT_OPENAI_CHAT_PROMPT_CACHE", "off"),
        ("DOCCHAT_OPENAI_SMALL_REASONING_EFFORT", "extreme"),
    ],
)
def test_check_1_refuses_a_value_outside_its_range(
    api_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    api_env.setenv(name, value)

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == (name,)


def test_check_1_names_a_missing_setting(api_env: pytest.MonkeyPatch) -> None:
    api_env.delenv("DOCCHAT_COSMOS_ENDPOINT")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_COSMOS_ENDPOINT",)


def _exit_record(failure: StartupCheckError) -> tuple[int, dict[str, Any]]:
    stream = io.StringIO()
    with pytest.raises(SystemExit) as exited:
        exit_on_startup_failure(failure, stream)
    return int(str(exited.value.code)), json.loads(stream.getvalue())


def test_t117_local_emulators_in_azure_exit_78(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    api_env.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", "Abcdefgh1234!Abcdefgh123")
    api_env.setenv("WEBSITE_SITE_NAME", "app-docchat-api-dev")

    with pytest.raises(StartupRefusalError) as caught:
        load_process_settings()
    code, record = _exit_record(caught.value)

    assert code == 78
    assert record["name"] == "local_emulators_in_azure"


def test_t117_an_insecure_endpoint_exits_78(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_SEARCH_ENDPOINT", "http://127.0.0.1:9")

    with pytest.raises(StartupRefusalError) as caught:
        load_process_settings()
    code, record = _exit_record(caught.value)

    assert code == 78
    assert record["name"] == "insecure_endpoint"
    assert record["settings"] == ["DOCCHAT_SEARCH_ENDPOINT"]


def test_t118_check_4_refuses_local_inside_azure(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "local")
    api_env.setenv("WEBSITE_INSTANCE_ID", "abc")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_CORE_ENVIRONMENT",)


def test_t118_check_5_refuses_prod_with_a_test_client(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "prod")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_AUTH_TEST_CLIENT_ID",)


def test_t118_prod_starts_without_a_test_client(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_CORE_ENVIRONMENT", "prod")
    api_env.delenv("DOCCHAT_AUTH_TEST_CLIENT_ID")

    assert load_process_settings().auth.auth_test_client_id is None


def test_t118_check_6_refuses_equal_client_ids(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_AUTH_WEB_CLIENT_ID", "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert set(caught.value.settings) == {
        "DOCCHAT_AUTH_API_CLIENT_ID",
        "DOCCHAT_AUTH_WEB_CLIENT_ID",
    }


def test_t118_check_7_requires_the_identity_outside_local(api_env: pytest.MonkeyPatch) -> None:
    api_env.delenv("DOCCHAT_CORE_IDENTITY_CLIENT_ID")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_CORE_IDENTITY_CLIENT_ID",)


def test_check_9_refuses_debug_outside_local_emulators(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_TELEMETRY_LOG_LEVEL", "DEBUG")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_TELEMETRY_LOG_LEVEL",)


def test_check_10_refuses_a_broken_embedding_entry(api_env: pytest.MonkeyPatch) -> None:
    api_env.setenv("DOCCHAT_OPENAI_EMBEDDING_MODELS", '[{"model":"x"}]')

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_OPENAI_EMBEDDING_MODELS",)


def test_the_sql_server_is_required_outside_local_emulators(api_env: pytest.MonkeyPatch) -> None:
    api_env.delenv("DOCCHAT_SQL_SERVER")

    with pytest.raises(SettingsValidationError) as caught:
        load_process_settings()

    assert caught.value.settings == ("DOCCHAT_SQL_SERVER",)


def _api(**values: object) -> ApiSettings:
    base: dict[str, object] = {
        "cosmos_endpoint": "https://c.documents.azure.com:443/",
        "search_endpoint": "https://s.search.windows.net",
        "openai_endpoint": "https://o.openai.azure.com",
        "contentsafety_endpoint": "https://cs.cognitiveservices.azure.com",
        "api_workers": 2,
    }
    return ApiSettings.model_validate(base | values)


def test_t118_check_8_accepts_the_catalogue_defaults() -> None:
    check_models(_api())


@pytest.mark.parametrize(
    ("values", "failing"),
    [
        ({"openai_chat_model": "gpt-4o"}, ("DOCCHAT_OPENAI_CHAT_MODEL",)),
        ({"openai_small_model": "gpt-4o-mini"}, ("DOCCHAT_OPENAI_SMALL_MODEL",)),
    ],
)
def test_t118_check_8_refuses_a_model_outside_the_table(
    values: dict[str, object], failing: tuple[str, ...]
) -> None:
    with pytest.raises(SettingsValidationError) as caught:
        check_models(_api(**values))

    assert caught.value.settings == failing


def test_t118_check_8_refuses_an_effort_outside_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from docchat_core.chat import models  # noqa: PLC0415 - the table under test

    narrowed = models.ModelCapability(
        streaming_chat_completions=True,
        strict_structured_outputs=True,
        prompt_cache_options=False,
        reasoning_efforts=frozenset({"low"}),
    )
    monkeypatch.setattr(
        "docchat_api.settings.MODEL_CAPABILITIES",
        {"gpt-5.6-sol": narrowed, "gpt-5.6-terra": models.MODEL_CAPABILITIES["gpt-5.6-terra"]},
    )

    with pytest.raises(SettingsValidationError) as caught:
        check_models(_api(openai_chat_reasoning_effort="high"))

    assert caught.value.settings == (
        "DOCCHAT_OPENAI_CHAT_REASONING_EFFORT",
        "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE",
    )


def test_the_chat_small_efforts_come_from_its_row() -> None:
    assert _api().chat_small_efforts == frozenset({"none", "low", "medium", "high"})
    assert _api(openai_small_model="gpt-4o-mini").chat_small_efforts == frozenset()


def test_check_8_loads_both_encodings() -> None:
    loaded: list[str] = []

    check_encodings(_api(openai_small_encoding="cl100k_base"), loaded.append)

    assert loaded == ["o200k_base", "cl100k_base"]


def test_check_8_refuses_an_encoding_that_does_not_load() -> None:
    def load(name: str) -> None:
        if name == "cl100k_base":
            raise FileNotFoundError(name)

    with pytest.raises(SettingsValidationError) as caught:
        check_encodings(_api(openai_small_encoding="cl100k_base"), load)

    assert caught.value.settings == ("DOCCHAT_OPENAI_SMALL_ENCODING",)


def test_the_default_loader_reads_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    import types  # noqa: PLC0415

    loaded: list[str] = []
    fake = types.SimpleNamespace(get_encoding=loaded.append)
    monkeypatch.setitem(__import__("sys").modules, "tiktoken", fake)

    check_encodings(_api())

    assert loaded == ["o200k_base", "o200k_base"]


def test_local_settings_are_read_but_never_composed() -> None:
    assert not set(LocalSettings.setting_names().values()) & _bicep_api_settings()
