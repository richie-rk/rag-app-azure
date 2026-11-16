"""The client factory: `ClientRegistry`, `search_client(physical_index_name)` and the `openai`
module's `AsyncOpenAI` profiles (`01-repository.md` section 2.3; ADR 0113).

No other module of `docchat_core`, `docchat_api` or `docchat_ingestion` constructs an Azure SDK
client, `openai.AsyncOpenAI` or an Azure credential; `scripts/ci/check_outbound_clients.py`
enforces it.
"""

from docchat_core.clients.contentsafety import (
    CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS,
    CONTENTSAFETY_SCOPE,
    BearerTokenAuth,
)
from docchat_core.clients.openai import OPENAI_SCOPE, ClientProfile, openai_base_url
from docchat_core.clients.registry import ClientRegistry, ServiceEndpoints, search_client
from docchat_core.clients.search import SEARCH_API_VERSION, SEARCH_CLIENTS_MAX

__all__ = [
    "CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS",
    "CONTENTSAFETY_SCOPE",
    "OPENAI_SCOPE",
    "SEARCH_API_VERSION",
    "SEARCH_CLIENTS_MAX",
    "BearerTokenAuth",
    "ClientProfile",
    "ClientRegistry",
    "ServiceEndpoints",
    "openai_base_url",
    "search_client",
]
