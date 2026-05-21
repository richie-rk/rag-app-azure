"""Centralized configuration via pydantic-settings.

Every environment variable the app reads is declared here as a typed field.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str

    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str = "2024-10-21"
    default_llm_deployment: str = "gpt-4o"
    embedding_deployment: str = "text-embedding-ada-002"
    embedding_dimensions: int = 1536

    # Azure AI Search
    azure_search_endpoint: str
    azure_search_admin_key: str

    # Azure Storage
    azure_storage_connection_string: str
    default_blob_container: str = "documents"

    # Azure Table Storage
    azure_table_name: str = "chatsessions"

    # Authentication
    jwt_secret: str
    jwt_audience: str = "rag-app-azure"
    jwt_issuer: str = "rag-app-azure"
    magic_link_base_url: str = "http://localhost:5173/auth/verify"
    msal_client_id: str = ""
    msal_tenant_id: str = ""
    # Audience expected on inbound Azure AD access tokens. Typically the
    # App Registration client ID or its api://<client-id> URI.
    api_audience: str = ""
    # Object IDs of the two security groups whose transitive membership maps
    # to a platform role. See ADR-0003.
    azure_ad_admin_group_id: str = ""
    azure_ad_user_group_id: str = ""

    # Search Defaults
    default_top_k: int = 10

    # CORS
    allowed_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return cached settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
