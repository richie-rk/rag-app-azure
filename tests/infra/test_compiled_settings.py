"""T-127's compiled case and the apps' composed settings (81-config.md section 2;
83-infrastructure.md sections 2.5, 2.6 and 8.9).

The deployed comparison T-114 proves the full set after a deploy; this checks what the template
composes: exactly the API site's 57 settings of 81-config.md sections 2.3, 2.4 and 2.7, and the
ingestion app's 43 of sections 2.3, 2.5 and 2.7.
"""

from __future__ import annotations

import re

import pytest
from arm_template import TENANT_ID
from compiled_support import ENVIRONMENTS, INFRA, REPO_ROOT, MainFactory, avm_call, load_json

EMBEDDING_MODELS = (
    '[{"model":"text-embedding-3-large","modelVersion":"1","queryDeployment":"embed-query-te3-large-v1",'
    '"ingestionDeployment":"embed-ingest-te3-large-v1","dimensions":[256,1024,3072],"defaultDimensions":1024,'
    '"maxInputTokens":8192,"encoding":"cl100k_base"}]'
)

SHARED_SETTINGS = {
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING",
    "DOCCHAT_TELEMETRY_SAMPLING_RATIO",
    "DOCCHAT_TELEMETRY_LOG_LEVEL",
    "DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS",
    "DOCCHAT_CORE_ENVIRONMENT",
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID",
    "DOCCHAT_CORE_IDENTITY_CLIENT_ID",
    "DOCCHAT_SQL_SERVER",
    "DOCCHAT_SQL_DATABASE",
    "DOCCHAT_SQL_THREADS",
    "DOCCHAT_SQL_POOL_TIMEOUT_SECONDS",
    "DOCCHAT_SQL_CONNECT_TIMEOUT_SECONDS",
    "DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS",
    "DOCCHAT_SQL_IDLE_RELEASE_MINUTES",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
    "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED",
    "DOCCHAT_SEARCH_ENDPOINT",
    "DOCCHAT_OPENAI_ENDPOINT",
    "DOCCHAT_OPENAI_EMBEDDING_MODELS",
}

API_SETTINGS = SHARED_SETTINGS | {
    "DOCCHAT_AUTH_TENANT_ID",
    "DOCCHAT_AUTH_API_CLIENT_ID",
    "DOCCHAT_AUTH_WEB_CLIENT_ID",
    "DOCCHAT_AUTH_TEST_CLIENT_ID",
    "DOCCHAT_API_WORKERS",
    "DOCCHAT_API_GRACEFUL_TIMEOUT_SECONDS",
    "DOCCHAT_API_WORKER_TIMEOUT_SECONDS",
    "DOCCHAT_COSMOS_ENDPOINT",
    "DOCCHAT_COSMOS_DATABASE",
    "DOCCHAT_UPLOAD_MAX_BYTES",
    "DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH",
    "DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER",
    "DOCCHAT_SEARCH_MAX_INDEXES",
    "DOCCHAT_SEARCH_TIMEOUT_SECONDS",
    "DOCCHAT_OPENAI_CHAT_DEPLOYMENT",
    "DOCCHAT_OPENAI_CHAT_MODEL",
    "DOCCHAT_OPENAI_CHAT_MODEL_VERSION",
    "DOCCHAT_OPENAI_CHAT_ENCODING",
    "DOCCHAT_OPENAI_CHAT_REASONING_EFFORT",
    "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE",
    "DOCCHAT_OPENAI_SMALL_DEPLOYMENT",
    "DOCCHAT_OPENAI_SMALL_MODEL",
    "DOCCHAT_OPENAI_SMALL_MODEL_VERSION",
    "DOCCHAT_OPENAI_SMALL_ENCODING",
    "DOCCHAT_OPENAI_SMALL_REASONING_EFFORT",
    "DOCCHAT_OPENAI_SMALL_PROMPT_CACHE",
    "DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS",
    "DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT",
    "DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS",
    "WEBSITE_AUTH_AAD_ALLOWED_TENANTS",
    "WEBSITES_ENABLE_APP_SERVICE_STORAGE",
    "WEBSITES_PORT",
    "WEBSITES_CONTAINER_START_TIME_LIMIT",
    "WEBSITES_CONTAINER_STOP_TIME_LIMIT",
    "WEBSITE_HEALTHCHECK_MAXPINGFAILURES",
}

INGESTION_SETTINGS = SHARED_SETTINGS | {
    "DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT",
    "DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING",
    "DOCCHAT_DOCINTEL_ENDPOINT",
    "DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB",
    "DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS",
    "DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS",
    "DOCCHAT_INGESTION_QUEUE__queueServiceUri",
    "DOCCHAT_INGESTION_QUEUE__credential",
    "DOCCHAT_INGESTION_QUEUE__clientId",
    "AzureWebJobsStorage__blobServiceUri",
    "AzureWebJobsStorage__queueServiceUri",
    "AzureWebJobsStorage__tableServiceUri",
    "AzureWebJobsStorage__credential",
    "AzureWebJobsStorage__clientId",
    "FUNCTIONS_EXTENSION_VERSION",
    "FUNCTIONS_WORKER_RUNTIME",
    "FUNCTIONS_WORKER_PROCESS_COUNT",
    "AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions",
    "AzureFunctionsJobHost__extensions__durableTask__maxConcurrentOrchestratorFunctions",
    "AzureFunctionsJobHost__extensions__durableTask__hubName",
    "WEBSITES_ENABLE_APP_SERVICE_STORAGE",
    "WEBSITES_CONTAINER_START_TIME_LIMIT",
}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t127_compiled_embedding_catalogue(main_deployment: MainFactory, environment: str) -> None:
    assert (
        main_deployment(environment).outputs()["DOCCHAT_OPENAI_EMBEDDING_MODELS"]
        == EMBEDDING_MODELS
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_ingestion_app_settings(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    site = avm_call(deployment, f"func-docchat-ingest-{environment}-{token}")
    (config,) = site["configs"]
    assert config["name"] == "appsettings"
    settings = config["properties"]
    assert set(settings) == INGESTION_SETTINGS
    assert len(settings) == 43
    defaults = load_json(INFRA / "config" / "app-settings.json")["ingestion"]
    assert {key: settings[key] for key in defaults} == defaults
    sku_values = {"dev": "1", "prod": "2", "load": "2"}[environment]
    durable = {"dev": "2", "prod": "4", "load": "4"}[environment]
    assert settings["FUNCTIONS_WORKER_PROCESS_COUNT"] == sku_values
    assert (
        settings["AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions"]
        == durable
    )
    assert (
        settings[
            "AzureFunctionsJobHost__extensions__durableTask__maxConcurrentOrchestratorFunctions"
        ]
        == durable
    )
    assert (
        settings["AzureFunctionsJobHost__extensions__durableTask__hubName"]
        == f"docchat{environment}"
    )
    assert settings["DOCCHAT_CORE_ENVIRONMENT"] == environment
    assert settings["DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED"] == (
        "true" if environment == "prod" else "false"
    )
    assert settings["AzureWebJobsStorage__credential"] == "managedidentity"
    assert settings["DOCCHAT_INGESTION_QUEUE__credential"] == "managedidentity"
    assert settings["WEBSITES_CONTAINER_START_TIME_LIMIT"] == "900"
    client_id = f"<id-docchat-ingest-{environment}.outputs.clientId.value>"
    assert str(settings["AzureWebJobsStorage__clientId"]) == client_id
    assert (
        settings["APPLICATIONINSIGHTS_AUTHENTICATION_STRING"]
        == f"Authorization=AAD;ClientId={client_id}"
    )
    assert not [key for key in settings if key.startswith(("OTEL_", "WEBSITE_CONTENT"))]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_ingestion_service_endpoints(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    outputs = deployment.outputs()
    token = outputs["DOCCHAT_RESOURCE_TOKEN"]
    site = avm_call(deployment, f"func-docchat-ingest-{environment}-{token}")
    settings = site["configs"][0]["properties"]
    search = f"https://srch-docchat-{environment}-{token}.search.windows.net"
    assert settings["DOCCHAT_SEARCH_ENDPOINT"] == outputs["DOCCHAT_SEARCH_ENDPOINT"] == search
    assert settings["DOCCHAT_DOCINTEL_ENDPOINT"] == (
        f"https://di-docchat-{environment}-{token}.cognitiveservices.azure.com"
    )
    # load calls the model stub through its private endpoint; dev and prod their own account.
    openai = (
        f"https://app-docchat-stub-load-{token}.azurewebsites.net"
        if environment == "load"
        else f"https://oai-docchat-{environment}-{token}.openai.azure.com"
    )
    assert settings["DOCCHAT_OPENAI_ENDPOINT"] == outputs["DOCCHAT_OPENAI_ENDPOINT"] == openai
    assert settings["DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT"] == "ingest-summary"
    assert settings["DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING"] == "o200k_base"


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_ingestion_service_outputs(main_deployment: MainFactory, environment: str) -> None:
    outputs = main_deployment(environment).outputs()
    token = outputs["DOCCHAT_RESOURCE_TOKEN"]
    assert outputs["DOCCHAT_SEARCH_SERVICE_NAME"] == f"srch-docchat-{environment}-{token}"
    max_indexes = {"dev": "15", "prod": "50", "load": "50"}[environment]
    assert outputs["DOCCHAT_SEARCH_MAX_INDEXES"] == max_indexes
    account = "" if environment == "load" else f"oai-docchat-{environment}-{token}"
    assert outputs["DOCCHAT_OPENAI_ACCOUNT_NAME"] == account


def _api_settings(main_deployment: MainFactory, environment: str) -> dict[str, object]:
    deployment = main_deployment(environment)
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    site = avm_call(deployment, f"app-docchat-api-{environment}-{token}")
    (config,) = site["configs"]
    assert config["name"] == "appsettings"
    # The set is exact: a setting the template doesn't declare is removed.
    assert config["retainCurrentAppSettings"] is False
    settings: dict[str, object] = config["properties"]
    return settings


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_app_settings(main_deployment: MainFactory, environment: str) -> None:
    settings = _api_settings(main_deployment, environment)
    assert set(settings) == API_SETTINGS
    assert len(settings) == 57
    defaults = load_json(INFRA / "config" / "app-settings.json")["api"]
    assert {key: settings[key] for key in defaults} == defaults
    assert settings["DOCCHAT_API_WORKERS"] == {"dev": "1", "prod": "2", "load": "2"}[environment]
    assert settings["DOCCHAT_CORE_ENVIRONMENT"] == environment
    assert settings["DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED"] == (
        "true" if environment == "prod" else "false"
    )
    assert settings["DOCCHAT_SEARCH_MAX_INDEXES"] == ("15" if environment == "dev" else "50")
    assert settings["DOCCHAT_SQL_DATABASE"] == settings["DOCCHAT_COSMOS_DATABASE"] == "docchat"
    expected_platform = {
        "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "false",
        "WEBSITES_PORT": "8000",
        "WEBSITES_CONTAINER_START_TIME_LIMIT": "300",
        "WEBSITES_CONTAINER_STOP_TIME_LIMIT": "120",
        "WEBSITE_HEALTHCHECK_MAXPINGFAILURES": "3",
    }
    assert {key: settings[key] for key in expected_platform} == expected_platform
    client_id = f"<id-docchat-api-{environment}.outputs.clientId.value>"
    assert str(settings["DOCCHAT_CORE_IDENTITY_CLIENT_ID"]) == client_id
    assert str(settings["DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID"]) == (
        f"<id-docchat-api-{environment}.outputs.principalId.value>"
    )
    assert (
        settings["APPLICATIONINSIGHTS_AUTHENTICATION_STRING"]
        == f"Authorization=AAD;ClientId={client_id}"
    )
    assert not [key for key in settings if key.startswith(("OTEL_", "WEBSITE_CONTENT"))]
    assert "WEBSITE_RUN_FROM_PACKAGE" not in settings


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_identity_settings(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    outputs = deployment.outputs()
    settings = _api_settings(main_deployment, environment)
    assert settings["DOCCHAT_AUTH_TENANT_ID"] == TENANT_ID.lower()
    assert settings["WEBSITE_AUTH_AAD_ALLOWED_TENANTS"] == TENANT_ID.lower()
    assert settings["DOCCHAT_AUTH_API_CLIENT_ID"] == outputs["API_CLIENT_ID"]
    assert settings["DOCCHAT_AUTH_WEB_CLIENT_ID"] == outputs["WEB_CLIENT_ID"]
    # prod has no test client, so its setting is empty (the start-up check refuses anything else).
    assert settings["DOCCHAT_AUTH_TEST_CLIENT_ID"] == outputs["TEST_CLIENT_ID"]
    assert (settings["DOCCHAT_AUTH_TEST_CLIENT_ID"] == "") is (environment == "prod")


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_service_endpoints(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    outputs = deployment.outputs()
    token = outputs["DOCCHAT_RESOURCE_TOKEN"]
    settings = _api_settings(main_deployment, environment)
    for name in (
        "DOCCHAT_SQL_SERVER",
        "DOCCHAT_COSMOS_ENDPOINT",
        "DOCCHAT_STORAGE_BLOB_ENDPOINT",
        "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
        "DOCCHAT_SEARCH_ENDPOINT",
        "DOCCHAT_OPENAI_ENDPOINT",
        "DOCCHAT_OPENAI_EMBEDDING_MODELS",
        "DOCCHAT_CONTENTSAFETY_ENDPOINT",
    ):
        assert str(settings[name]) == str(outputs[name]), name
    assert settings["DOCCHAT_OPENAI_EMBEDDING_MODELS"] == EMBEDDING_MODELS
    # The Content Safety account's custom-subdomain endpoint, in every environment, load included.
    assert settings["DOCCHAT_CONTENTSAFETY_ENDPOINT"] == (
        f"https://cs-docchat-{environment}-{token}.cognitiveservices.azure.com"
    )
    chat = {
        "DOCCHAT_OPENAI_CHAT_DEPLOYMENT": "chat",
        "DOCCHAT_OPENAI_CHAT_MODEL": "gpt-5.6-sol",
        "DOCCHAT_OPENAI_CHAT_MODEL_VERSION": "2026-07-09",
        "DOCCHAT_OPENAI_CHAT_ENCODING": "o200k_base",
        "DOCCHAT_OPENAI_SMALL_DEPLOYMENT": "chat-small",
        "DOCCHAT_OPENAI_SMALL_MODEL": "gpt-5.6-terra",
        "DOCCHAT_OPENAI_SMALL_MODEL_VERSION": "2026-07-09",
        "DOCCHAT_OPENAI_SMALL_ENCODING": "o200k_base",
    }
    # load keeps the usual deployment names, by which the model stub keys its statistics.
    assert {key: settings[key] for key in chat} == chat


def _specified_outputs() -> set[str]:
    """The output names of 83-infrastructure.md section 8.8's table."""
    text = (REPO_ROOT / "docs" / "spec" / "83-infrastructure.md").read_text(encoding="utf-8")
    section = text.split("### 8.8 Outputs", 1)[1].split("\n### ", 1)[0]
    rows = re.findall(r"^\| ([^|]+) \| [^|]+ \|$", section, re.MULTILINE)
    return {name for row in rows for name in re.findall(r"`([A-Z][A-Z0-9_]+)`", row)}


def test_azd_outputs_equal_section_8_8(main_template: dict[str, object]) -> None:
    outputs = main_template["outputs"]
    assert isinstance(outputs, dict)
    assert set(outputs) == _specified_outputs()
