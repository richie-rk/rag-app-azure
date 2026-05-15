"""Azure SDK client singletons, built once via lru_cache and reused across requests."""

from functools import lru_cache

from azure.core.credentials import AzureKeyCredential
from azure.data.tables import TableServiceClient
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from .config import get_settings


@lru_cache(maxsize=1)
def get_openai_client() -> AzureOpenAI:
    """Return a singleton Azure OpenAI client."""
    s = get_settings()
    return AzureOpenAI(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
    )


@lru_cache(maxsize=1)
def get_blob_service_client() -> BlobServiceClient:
    """Return a singleton BlobServiceClient."""
    s = get_settings()
    return BlobServiceClient.from_connection_string(s.azure_storage_connection_string)


@lru_cache(maxsize=1)
def get_table_service_client() -> TableServiceClient:
    """Return a singleton TableServiceClient."""
    s = get_settings()
    return TableServiceClient.from_connection_string(s.azure_storage_connection_string)


@lru_cache(maxsize=1)
def get_search_index_client() -> SearchIndexClient:
    """Return a singleton SearchIndexClient for index management."""
    s = get_settings()
    return SearchIndexClient(
        endpoint=s.azure_search_endpoint,
        credential=AzureKeyCredential(s.azure_search_admin_key),
    )


@lru_cache(maxsize=32)
def get_search_client(index_name: str) -> SearchClient:
    """Return a cached SearchClient for a specific index."""
    s = get_settings()
    return SearchClient(
        endpoint=s.azure_search_endpoint,
        index_name=index_name,
        credential=AzureKeyCredential(s.azure_search_admin_key),
    )
