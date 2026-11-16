from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

from docchat_core.clients import registry as registry_module
from docchat_core.clients.emulators import COSMOS_EMULATOR_KEY, LOCAL_STUB_BEARER_TOKEN
from docchat_core.clients.openai import ClientProfile
from docchat_core.clients.registry import ClientRegistry, ServiceEndpoints, search_client
from docchat_core.settings import (
    CoreSettings,
    LocalSettings,
    SettingsValidationError,
    StartupRefusalError,
)

_CLIENT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
_PRINCIPAL_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_COSMOS = "https://cosmos-docchat-dev-x7.documents.azure.com:443/"
_BLOB = "https://stdocchatdatadevx7.blob.core.windows.net/"
_QUEUE = "https://stdocchatdatadevx7.queue.core.windows.net/"
_SEARCH = "https://srch-docchat-dev-x7.search.windows.net"
_OPENAI = "https://oai-docchat-dev-x7.openai.azure.com"
_CONTENTSAFETY = "https://cs-docchat-dev-x7.cognitiveservices.azure.com"
_DOCINTEL = "https://di-docchat-dev-x7.cognitiveservices.azure.com"

_API = ServiceEndpoints(
    cosmos_endpoint=_COSMOS,
    cosmos_database="docchat",
    blob_endpoint=_BLOB,
    queue_endpoint=_QUEUE,
    queue_names=("ingestion-requests",),
    search_endpoint=_SEARCH,
    search_timeout_seconds=12,
    openai_endpoint=_OPENAI,
    openai_connect_timeout_seconds=10,
    openai_read_timeout_seconds=60,
    contentsafety_endpoint=_CONTENTSAFETY,
    contentsafety_timeout_seconds=5,
)
_INGESTION = ServiceEndpoints(
    blob_endpoint=_BLOB,
    queue_endpoint=_QUEUE,
    queue_names=("ingestion-requests-poison",),
    search_endpoint=_SEARCH,
    openai_endpoint=_OPENAI,
    docintel_endpoint=_DOCINTEL,
)

type Open = Callable[..., ClientRegistry]


def _core(environment: str = "dev", client_id: str | None = _CLIENT_ID) -> CoreSettings:
    return CoreSettings(
        core_environment=environment,  # type: ignore[arg-type]
        core_identity_client_id=client_id,  # type: ignore[arg-type]
        core_workload_principal_id=_PRINCIPAL_ID,  # type: ignore[arg-type]
    )


@pytest.fixture
async def open_registry(settings_env: pytest.MonkeyPatch) -> AsyncIterator[Open]:
    opened: list[ClientRegistry] = []

    def build(
        endpoints: ServiceEndpoints = _API,
        *,
        profile: ClientProfile = ClientProfile.API,
        core: CoreSettings | None = None,
        emulators: bool = False,
    ) -> ClientRegistry:
        registry = ClientRegistry(
            core or _core(),
            LocalSettings(local_emulators=emulators),
            profile=profile,
            endpoints=endpoints,
            environ={},
        )
        opened.append(registry)
        return registry

    yield build
    for registry in opened:
        await registry.close()


def _transport(client: Any) -> dict[str, Any]:
    config = client._client._pipeline._transport.connection_config
    return {"timeout": config.timeout, "read_timeout": config.read_timeout}


def _storage_transport(client: Any) -> dict[str, Any]:
    config = client._pipeline._transport.connection_config
    return {"timeout": config.timeout, "read_timeout": config.read_timeout}


def _storage_retry(client: Any) -> tuple[int, int, int, int]:
    retry = client._config.retry_policy
    return (
        retry.initial_backoff,
        retry.increment_base,
        retry.total_retries,
        retry.random_jitter_range,
    )


async def test_in_azure_every_client_shares_one_managed_identity_credential(
    open_registry: Open, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []

    class _Recording(ManagedIdentityCredential):
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(registry_module, "ManagedIdentityCredential", _Recording)

    registry = open_registry()

    assert created == [{"client_id": _CLIENT_ID}]
    assert isinstance(registry.credential, _Recording)
    assert registry.blob_service.credential is registry.credential
    assert registry.search_client("docchat-a-g1")._config.credential is registry.credential


async def test_local_uses_the_azure_cli_credential(open_registry: Open) -> None:
    registry = open_registry(core=_core("local", None))

    assert isinstance(registry.credential, AzureCliCredential)


async def test_outside_local_the_client_id_is_required(open_registry: Open) -> None:
    with pytest.raises(SettingsValidationError) as caught:
        open_registry(core=_core("prod", None))

    assert caught.value.settings == ("DOCCHAT_CORE_IDENTITY_CLIENT_ID",)


async def test_local_emulator_mode_is_refused_inside_azure(
    settings_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(StartupRefusalError) as caught:
        ClientRegistry(
            _core("local", None),
            LocalSettings(local_emulators=True),
            profile=ClientProfile.API,
            endpoints=_API,
            environ={"WEBSITE_SITE_NAME": "app-docchat-api-dev"},
        )

    assert caught.value.code == "local_emulators_in_azure"


async def test_the_api_cosmos_client(open_registry: Open) -> None:
    registry = open_registry()
    connection = registry.cosmos.client_connection

    assert connection.url_connection == _COSMOS
    assert connection.connection_policy.RequestTimeout == 5
    assert connection.default_headers["x-ms-consistency-level"] == "Session"
    assert connection.master_key is None
    assert registry.cosmos_database.id == "docchat"


async def test_the_api_blob_client_streams_in_one_mebibyte_reads(open_registry: Open) -> None:
    registry = open_registry()
    blob = registry.blob_service

    assert blob.url == _BLOB
    assert _storage_transport(blob) == {"timeout": 10, "read_timeout": 60}
    assert (blob._config.max_single_get_size, blob._config.max_chunk_get_size) == (
        1_048_576,
        1_048_576,
    )
    assert _storage_retry(blob) == (1, 2, 3, 1)
    container = registry.blob_container("renditions")
    assert container.container_name == "renditions"
    assert registry.blob_container("renditions") is container


async def test_the_queue_clients(open_registry: Open) -> None:
    registry = open_registry()
    queue = registry.queue("ingestion-requests")

    assert queue.url == _QUEUE + "ingestion-requests"
    assert _storage_transport(queue) == {"timeout": 10, "read_timeout": 60}
    assert _storage_retry(queue) == (1, 2, 3, 1)
    assert type(queue._message_encode_policy).__name__ == "TextBase64EncodePolicy"
    with pytest.raises(RuntimeError, match="queue ingestion-requests-poison"):
        registry.queue("ingestion-requests-poison")


async def test_the_api_search_clients(open_registry: Open) -> None:
    registry = open_registry()
    client = registry.search_client("docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2")

    assert client._config.api_version == "2026-04-01"
    assert client._config.index_name == "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2"
    assert _transport(client) == {"timeout": 10, "read_timeout": 12}
    retry = client._config.retry_policy
    assert (retry.total_retries, retry.backoff_factor, retry.backoff_max) == (2, 0.5, 4)
    assert registry.search_client("docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2") is client
    with pytest.raises(RuntimeError, match="AI Search index"):
        _ = registry.search_index_client


async def test_search_client_is_the_process_registry_s(open_registry: Open) -> None:
    registry = open_registry()

    assert search_client("docchat-a-g1") is registry.search_client("docchat-a-g1")


async def test_the_api_model_and_screening_clients(open_registry: Open) -> None:
    registry = open_registry()

    assert str(registry.openai.base_url) == _OPENAI + "/openai/v1/"
    assert registry.openai.max_retries == 0
    assert callable(registry.openai._api_key_provider)
    assert str(registry.contentsafety.base_url) == _CONTENTSAFETY
    assert registry.contentsafety.timeout.read == 5
    with pytest.raises(RuntimeError, match="Document Intelligence"):
        _ = registry.document_intelligence


async def test_t195_the_ingestion_clients(open_registry: Open) -> None:
    registry = open_registry(_INGESTION, profile=ClientProfile.INGESTION)

    blob = registry.blob_service
    assert blob._config.max_single_get_size != 1_048_576
    assert _storage_transport(blob) == {"timeout": 10, "read_timeout": 60}
    assert _storage_retry(blob) == (1, 2, 3, 1)

    poison = registry.queue("ingestion-requests-poison")
    assert _storage_transport(poison) == {"timeout": 10, "read_timeout": 60}
    assert _storage_retry(poison) == (1, 2, 3, 1)

    # Search and Document Intelligence keep azure-core's default retry policy (10 retries).
    search = registry.search_client("docchat-a-g1")
    assert _transport(search) == {"timeout": 10, "read_timeout": 120}
    assert search._config.retry_policy.total_retries == 10

    index = registry.search_index_client
    assert index._config.api_version == "2026-04-01"
    assert _transport(index) == {"timeout": 10, "read_timeout": 120}
    assert index._config.retry_policy.total_retries == 10

    docintel = registry.document_intelligence
    assert docintel._config.api_version == "2024-11-30"
    assert _transport(docintel) == {"timeout": 10, "read_timeout": 300}
    assert docintel._config.retry_policy.total_retries == 10

    assert registry.openai.max_retries == 3
    timeout = registry.openai.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (  # type: ignore[union-attr]
        5.0,
        60.0,
        60.0,
        60.0,
    )
    with pytest.raises(RuntimeError, match="Cosmos DB"):
        _ = registry.cosmos


async def test_local_emulator_mode_uses_the_published_constants(open_registry: Open) -> None:
    endpoints = ServiceEndpoints(
        cosmos_endpoint="http://127.0.0.1:8081/",
        cosmos_database="docchat",
        blob_endpoint="http://127.0.0.1:10000/devstoreaccount1",
        queue_endpoint="http://127.0.0.1:10001/devstoreaccount1",
        queue_names=("ingestion-requests",),
        search_endpoint=_SEARCH,
        search_timeout_seconds=10,
        openai_endpoint="http://127.0.0.1:8091",
        openai_connect_timeout_seconds=10,
        openai_read_timeout_seconds=60,
        contentsafety_endpoint="http://127.0.0.1:8091",
        contentsafety_timeout_seconds=5,
    )
    registry = open_registry(endpoints, core=_core("local", None), emulators=True)

    assert registry.local_emulators is True
    assert registry.cosmos.client_connection.master_key == COSMOS_EMULATOR_KEY
    assert registry.blob_service.url == "http://127.0.0.1:10000/devstoreaccount1/"
    assert registry.queue("ingestion-requests").url == (
        "http://127.0.0.1:10001/devstoreaccount1/ingestion-requests"
    )
    assert registry.openai.api_key == LOCAL_STUB_BEARER_TOKEN
    assert registry.search_client("docchat-a-g1")._config.credential is registry.credential


async def test_a_remote_model_endpoint_gets_a_real_token_even_in_emulator_mode(
    open_registry: Open,
) -> None:
    endpoints = ServiceEndpoints(openai_endpoint=_OPENAI, openai_connect_timeout_seconds=10,
                                 openai_read_timeout_seconds=60)  # fmt: skip
    registry = open_registry(endpoints, core=_core("local", None), emulators=True)

    assert callable(registry.openai._api_key_provider)


@pytest.mark.parametrize(
    ("endpoints", "profile", "message"),
    [
        (ServiceEndpoints(cosmos_endpoint=_COSMOS), ClientProfile.API, "Cosmos DB"),
        (ServiceEndpoints(queue_names=("ingestion-requests",)), ClientProfile.API, "queue"),
        (ServiceEndpoints(contentsafety_endpoint=_CONTENTSAFETY), ClientProfile.API, "timeout"),
        (ServiceEndpoints(search_endpoint=_SEARCH), ClientProfile.API, "SEARCH_TIMEOUT"),
        (ServiceEndpoints(openai_endpoint=_OPENAI), ClientProfile.API, "connect and read"),
    ],
)
async def test_incomplete_endpoints_are_refused(
    open_registry: Open, endpoints: ServiceEndpoints, profile: ClientProfile, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        open_registry(endpoints, profile=profile)


async def test_one_registry_per_process(open_registry: Open) -> None:
    first = open_registry(ServiceEndpoints())

    assert ClientRegistry.current() is first
    with pytest.raises(RuntimeError, match="already open"):
        open_registry(ServiceEndpoints())

    await first.close()
    with pytest.raises(RuntimeError, match="no client registry"):
        search_client("docchat-a-g1")
    second = open_registry(ServiceEndpoints())
    assert ClientRegistry.current() is second


async def test_close_closes_every_client_then_the_credential(open_registry: Open) -> None:
    registry = open_registry()
    order: list[str] = []

    def recording(name: str) -> Callable[[], Any]:
        async def close() -> None:
            order.append(name)

        return close

    registry.search_client("docchat-a-g1")
    # Cosmos DB, Blob, one queue, the search clients, the model client and Content Safety.
    assert len(registry._closers) == 6
    registry._closers = [recording(f"client-{n}") for n in range(6)]
    registry._credential.close = recording("credential")  # type: ignore[method-assign]

    await registry.close()
    await registry.close()

    assert order == [*(f"client-{n}" for n in reversed(range(6))), "credential"]


async def test_a_failing_close_still_closes_the_rest(open_registry: Open) -> None:
    registry = open_registry(ServiceEndpoints())
    closed: list[str] = []

    async def failing() -> None:
        raise OSError("close failed")

    async def credential_close() -> None:
        closed.append("credential")

    registry._closers = [failing]
    registry._credential.close = credential_close  # type: ignore[method-assign]

    with pytest.raises(OSError, match="close failed"):
        await registry.close()
    assert closed == ["credential"]


async def test_the_registry_is_an_async_context_manager(settings_env: pytest.MonkeyPatch) -> None:
    async with ClientRegistry(
        _core(), LocalSettings(), profile=ClientProfile.API, endpoints=ServiceEndpoints()
    ) as registry:
        assert ClientRegistry.current() is registry

    with pytest.raises(RuntimeError):
        ClientRegistry.current()
