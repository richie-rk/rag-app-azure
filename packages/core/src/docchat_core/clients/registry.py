"""`ClientRegistry`: the one place a process builds its credential and its service clients
(`01-repository.md` sections 2.3 and 6.4; `83-infrastructure.md` section 6.2; ADR 0113,
ADR 0150).

A process creates one registry at start-up (the API lifespan, the ingestion worker's first
invocation) and closes it at shutdown: every client, then the credential. In Azure every client
shares one `ManagedIdentityCredential(client_id=DOCCHAT_CORE_IDENTITY_CLIENT_ID)`, so the process
holds one token cache; the Azure CLI's credential is used only when `DOCCHAT_CORE_ENVIRONMENT` is
`local`, and `DefaultAzureCredential` never, so no other credential source can be picked up. No
client takes a key, a connection string or a SAS, except the emulators' published constants in
local-emulator mode. Every endpoint comes from a validated setting the caller passes in
`ServiceEndpoints`; every transport timeout is explicit, because azure-core's defaults are
300 seconds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self
from urllib.parse import urlsplit

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.cosmos.aio import CosmosClient, DatabaseProxy
from azure.identity.aio import (
    AzureCliCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.storage.blob import ExponentialRetry as BlobExponentialRetry
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from azure.storage.queue import ExponentialRetry as QueueExponentialRetry
from azure.storage.queue import TextBase64DecodePolicy, TextBase64EncodePolicy
from azure.storage.queue.aio import QueueClient

from docchat_core.clients.contentsafety import (
    CONTENTSAFETY_SCOPE,
    BearerToken,
    contentsafety_client,
)
from docchat_core.clients.emulators import (
    AZURITE_CONNECTION_STRING,
    COSMOS_EMULATOR_KEY,
    LOCAL_STUB_BEARER_TOKEN,
)
from docchat_core.clients.openai import (
    OPENAI_SCOPE,
    ClientProfile,
    api_openai_client,
    ingestion_openai_client,
)
from docchat_core.clients.search import SEARCH_API_VERSION, InFlightPolicy, SearchClientCache
from docchat_core.settings import (
    CoreSettings,
    EnvironmentName,
    LocalSettings,
    check_identity_client_id,
    check_local_emulators,
    is_loopback_host,
)

if TYPE_CHECKING:
    import httpx
    from azure.core.credentials_async import AsyncTokenCredential
    from openai import AsyncOpenAI

COSMOS_CONSISTENCY_LEVEL: Final = "Session"
COSMOS_CONNECTION_TIMEOUT_SECONDS: Final = 5
STORAGE_CONNECTION_TIMEOUT_SECONDS: Final = 10
STORAGE_READ_TIMEOUT_SECONDS: Final = 60
CITATION_GET_SIZE_BYTES: Final = 1_048_576
"""The API streams a citation file in 1 MiB reads, so it never buffers a whole file."""
SEARCH_CONNECTION_TIMEOUT_SECONDS: Final = 10
INGESTION_SEARCH_READ_TIMEOUT_SECONDS: Final = 120
API_SEARCH_RETRY_OPTIONS: Final[Mapping[str, float]] = {
    "retry_total": 2,
    "retry_backoff_factor": 0.5,
    "retry_backoff_max": 4,
}
"""The API's search retries: 2, backing off from 0.5 s to at most 4 s; ingestion keeps
azure-core's default retry policy."""
DOCINTEL_API_VERSION: Final = "2024-11-30"
DOCINTEL_CONNECTION_TIMEOUT_SECONDS: Final = 10
DOCINTEL_READ_TIMEOUT_SECONDS: Final = 300


def _storage_retry_options() -> dict[str, Any]:
    # Waits of about 1, 3 and 5 s inside one attempt.
    return {"initial_backoff": 1, "increment_base": 2, "retry_total": 3, "random_jitter_range": 1}


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceEndpoints:
    """The endpoint settings of the services a process calls; `None` leaves a service out.

    The values come from the settings classes that own them (`81-config.md` section 2.1):
    `cosmos_*` are `DOCCHAT_COSMOS_ENDPOINT` and `DOCCHAT_COSMOS_DATABASE`, `search_timeout_seconds`
    is `DOCCHAT_SEARCH_TIMEOUT_SECONDS`, the `openai_*_timeout_seconds` are
    `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` and `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`, and so on.
    `queue_names` are the queues of the data account the process sends to or peeks.
    """

    cosmos_endpoint: str | None = None
    cosmos_database: str | None = None
    blob_endpoint: str | None = None
    queue_endpoint: str | None = None
    queue_names: tuple[str, ...] = ()
    search_endpoint: str | None = None
    search_timeout_seconds: float | None = None
    openai_endpoint: str | None = None
    openai_connect_timeout_seconds: float | None = None
    openai_read_timeout_seconds: float | None = None
    contentsafety_endpoint: str | None = None
    contentsafety_timeout_seconds: float | None = None
    docintel_endpoint: str | None = None

    def check(self, profile: ClientProfile) -> None:
        """Refuse a combination the profile can't build a client from."""
        if (self.cosmos_endpoint is None) != (self.cosmos_database is None):
            raise ValueError("the Cosmos DB endpoint and database come together")
        if self.queue_names and self.queue_endpoint is None:
            raise ValueError("queue names need the queue endpoint")
        if self.contentsafety_endpoint is not None and self.contentsafety_timeout_seconds is None:
            raise ValueError("the Content Safety client needs its timeout")
        if profile is ClientProfile.API:
            if self.search_endpoint is not None and self.search_timeout_seconds is None:
                raise ValueError("the API's search clients need DOCCHAT_SEARCH_TIMEOUT_SECONDS")
            if self.openai_endpoint is not None and None in (
                self.openai_connect_timeout_seconds,
                self.openai_read_timeout_seconds,
            ):
                raise ValueError("the API's model client needs its connect and read timeouts")


def _required(value: float | None, setting: str) -> float:
    # `ServiceEndpoints.check` has refused a missing value already; this keeps the type exact.
    if value is None:
        raise ValueError(f"{setting} is required")
    return value


def _loopback(endpoint: str) -> bool:
    return is_loopback_host(urlsplit(endpoint).hostname or "")


class ClientRegistry:
    """The process's credential and clients, created once and closed once.

    A second registry can't open while one is open, so `search_client` always means the
    process's clients. Each accessor raises `RuntimeError` for a service the process didn't
    configure. Opening repeats the start-up checks the credential branch depends on (a missing
    client ID outside `local`, local-emulator mode inside Azure, judged on `environ` or the
    process environment), so no caller can reach a client through the wrong branch.
    """

    _open: ClassVar[ClientRegistry | None] = None

    def __init__(
        self,
        core: CoreSettings,
        local: LocalSettings,
        *,
        profile: ClientProfile,
        endpoints: ServiceEndpoints,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        check_identity_client_id(core)
        check_local_emulators(local, environ)
        endpoints.check(profile)
        if ClientRegistry._open is not None:
            raise RuntimeError("the process's client registry is already open")
        self.profile = profile
        self.local_emulators = local.local_emulators
        self._endpoints = endpoints
        self._closed = False
        self._credential = self._build_credential(core)
        self._closers: list[Callable[[], Awaitable[object]]] = []
        self._cosmos = self._build_cosmos()
        self._blob = self._build_blob()
        self._blob_containers: dict[str, ContainerClient] = {}
        self._queues = {name: self._build_queue(name) for name in endpoints.queue_names}
        self._search = self._build_search()
        self._search_index = self._build_search_index()
        self._openai = self._build_openai()
        self._contentsafety = self._build_contentsafety()
        self._docintel = self._build_docintel()
        ClientRegistry._open = self

    # Construction --------------------------------------------------------------------------

    @staticmethod
    def _build_credential(core: CoreSettings) -> AsyncTokenCredential:
        if core.core_environment is EnvironmentName.LOCAL:
            return AzureCliCredential()
        return ManagedIdentityCredential(client_id=str(core.core_identity_client_id))

    def _token(self, endpoint: str, scope: str) -> BearerToken:
        # The local model stub checks only that a bearer token is present.
        if self.local_emulators and _loopback(endpoint):
            return LOCAL_STUB_BEARER_TOKEN
        return get_bearer_token_provider(self._credential, scope)

    def _build_cosmos(self) -> CosmosClient | None:
        endpoint = self._endpoints.cosmos_endpoint
        if endpoint is None:
            return None
        credential: str | AsyncTokenCredential = (
            COSMOS_EMULATOR_KEY if self.local_emulators else self._credential
        )
        client = CosmosClient(
            endpoint,
            credential,
            consistency_level=COSMOS_CONSISTENCY_LEVEL,
            connection_timeout=COSMOS_CONNECTION_TIMEOUT_SECONDS,
        )
        self._closers.append(client.close)
        return client

    def _build_blob(self) -> BlobServiceClient | None:
        endpoint = self._endpoints.blob_endpoint
        if endpoint is None:
            return None
        options: dict[str, Any] = {"retry_policy": BlobExponentialRetry(**_storage_retry_options())}
        if self.profile is ClientProfile.API:
            options |= {
                "max_single_get_size": CITATION_GET_SIZE_BYTES,
                "max_chunk_get_size": CITATION_GET_SIZE_BYTES,
            }
        if self.local_emulators:
            client = BlobServiceClient.from_connection_string(
                AZURITE_CONNECTION_STRING,
                connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
                read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
                **options,
            )
        else:
            client = BlobServiceClient(
                endpoint,
                credential=self._credential,
                connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
                read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
                **options,
            )
        self._closers.append(client.close)
        return client

    def _build_queue(self, name: str) -> QueueClient:
        options: dict[str, Any] = {
            "retry_policy": QueueExponentialRetry(**_storage_retry_options()),
            # The trigger reads messages as base64 (`messageEncoding: base64`).
            "message_encode_policy": TextBase64EncodePolicy(),
            "message_decode_policy": TextBase64DecodePolicy(),
        }
        if self.local_emulators:
            client = QueueClient.from_connection_string(
                AZURITE_CONNECTION_STRING,
                name,
                connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
                read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
                **options,
            )
        else:
            client = QueueClient(
                str(self._endpoints.queue_endpoint),
                name,
                credential=self._credential,
                connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
                read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
                **options,
            )
        self._closers.append(client.close)
        return client

    def _build_search(self) -> SearchClientCache | None:
        endpoint = self._endpoints.search_endpoint
        if endpoint is None:
            return None
        if self.profile is ClientProfile.API:
            read_timeout = _required(
                self._endpoints.search_timeout_seconds, "DOCCHAT_SEARCH_TIMEOUT_SECONDS"
            )
            retry_options = dict(API_SEARCH_RETRY_OPTIONS)
        else:
            read_timeout = float(INGESTION_SEARCH_READ_TIMEOUT_SECONDS)
            retry_options = {}

        def build(physical_index_name: str, in_flight: InFlightPolicy) -> SearchClient:
            return SearchClient(
                endpoint,
                physical_index_name,
                self._credential,
                api_version=SEARCH_API_VERSION,
                connection_timeout=SEARCH_CONNECTION_TIMEOUT_SECONDS,
                read_timeout=read_timeout,
                per_call_policies=[in_flight],
                **retry_options,
            )

        cache = SearchClientCache(build)
        self._closers.append(cache.close)
        return cache

    def _build_search_index(self) -> SearchIndexClient | None:
        endpoint = self._endpoints.search_endpoint
        if endpoint is None or self.profile is not ClientProfile.INGESTION:
            return None
        client = SearchIndexClient(
            endpoint,
            self._credential,
            api_version=SEARCH_API_VERSION,
            connection_timeout=SEARCH_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=INGESTION_SEARCH_READ_TIMEOUT_SECONDS,
        )
        self._closers.append(client.close)
        return client

    def _build_openai(self) -> AsyncOpenAI | None:
        endpoint = self._endpoints.openai_endpoint
        if endpoint is None:
            return None
        token = self._token(endpoint, OPENAI_SCOPE)
        if self.profile is ClientProfile.API:
            client = api_openai_client(
                endpoint,
                token,
                connect_timeout_seconds=_required(
                    self._endpoints.openai_connect_timeout_seconds,
                    "DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS",
                ),
                read_timeout_seconds=_required(
                    self._endpoints.openai_read_timeout_seconds,
                    "DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS",
                ),
            )
        else:
            client = ingestion_openai_client(endpoint, token)
        self._closers.append(client.close)
        return client

    def _build_contentsafety(self) -> httpx.AsyncClient | None:
        endpoint = self._endpoints.contentsafety_endpoint
        if endpoint is None:
            return None
        client = contentsafety_client(
            endpoint,
            self._token(endpoint, CONTENTSAFETY_SCOPE),
            timeout_seconds=_required(
                self._endpoints.contentsafety_timeout_seconds,
                "DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS",
            ),
        )
        self._closers.append(client.aclose)
        return client

    def _build_docintel(self) -> DocumentIntelligenceClient | None:
        endpoint = self._endpoints.docintel_endpoint
        if endpoint is None:
            return None
        client = DocumentIntelligenceClient(
            endpoint,
            self._credential,
            api_version=DOCINTEL_API_VERSION,
            connection_timeout=DOCINTEL_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=DOCINTEL_READ_TIMEOUT_SECONDS,
        )
        self._closers.append(client.close)
        return client

    # Access --------------------------------------------------------------------------------

    @classmethod
    def current(cls) -> ClientRegistry:
        """The process's open registry."""
        if cls._open is None:
            raise RuntimeError("no client registry is open in this process")
        return cls._open

    @staticmethod
    def _configured[T](client: T | None, service: str) -> T:
        if client is None:
            raise RuntimeError(f"this process has no {service} client")
        return client

    @property
    def credential(self) -> AsyncTokenCredential:
        """The process's one credential, for the SQL layer and the telemetry exporter."""
        return self._credential

    @property
    def cosmos(self) -> CosmosClient:
        return self._configured(self._cosmos, "Cosmos DB")

    @property
    def cosmos_database(self) -> DatabaseProxy:
        """Database `DOCCHAT_COSMOS_DATABASE`; containers come from it by name."""
        return self.cosmos.get_database_client(str(self._endpoints.cosmos_database))

    @property
    def blob_service(self) -> BlobServiceClient:
        return self._configured(self._blob, "Blob Storage")

    def blob_container(self, name: str) -> ContainerClient:
        """A container of the data account, sharing the service client's connections."""
        container = self._blob_containers.get(name)
        if container is None:
            container = self.blob_service.get_container_client(name)
            self._blob_containers[name] = container
        return container

    def queue(self, name: str) -> QueueClient:
        return self._configured(self._queues.get(name), f"queue {name}")

    def search_client(self, physical_index_name: str) -> SearchClient:
        """The search client of one physical index, created on first use (see `search`)."""
        return self._configured(self._search, "AI Search").get(physical_index_name)

    @property
    def search_index_client(self) -> SearchIndexClient:
        return self._configured(self._search_index, "AI Search index")

    @property
    def openai(self) -> AsyncOpenAI:
        return self._configured(self._openai, "Azure OpenAI")

    @property
    def contentsafety(self) -> httpx.AsyncClient:
        return self._configured(self._contentsafety, "Content Safety")

    @property
    def document_intelligence(self) -> DocumentIntelligenceClient:
        return self._configured(self._docintel, "Document Intelligence")

    # Shutdown ------------------------------------------------------------------------------

    async def close(self) -> None:
        """Close every client, then the credential. Every close runs even when one fails; the
        failure is raised afterwards. Closing twice is a no-op."""
        if self._closed:
            return
        self._closed = True
        if ClientRegistry._open is self:
            ClientRegistry._open = None
        async with AsyncExitStack() as stack:
            # Callbacks run last-in first-out: the clients in reverse order, the credential last.
            stack.push_async_callback(self._credential.close)
            for close in self._closers:
                stack.push_async_callback(close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


def search_client(physical_index_name: str) -> SearchClient:
    """The process's `SearchClient` for one physical index: created on first use with
    `api_version="2026-04-01"` and the caller's timeouts, held in an LRU of at most 16, an
    evicted client closed once its in-flight requests end."""
    return ClientRegistry.current().search_client(physical_index_name)
