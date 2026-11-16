"""The API's settings and its start-up checks (`81-config.md` sections 2.1, 2.4 and 6.1;
`11-api.md` sections 1.15 and 1.16; ADR 0008).

`ApiSettings` holds every `DOCCHAT_*` setting the API reads that no other class owns: the Cosmos DB,
search, model and Content Safety endpoints and options, and the three `DOCCHAT_API_*` process
settings. `ProcessSettings` bundles it with the shared classes the API loads (`CoreSettings`,
`AuthSettings`, `TelemetrySettings`, `SqlSettings`, `LocalSettings`, `UploadSettings`,
`EmbeddingModelCatalog`), so the composition root takes one value.

`load_process_settings` runs checks 1 to 10 of section 6.1 in the table's order; `gunicorn.conf.py`
runs it, then `check_encodings`, before Gunicorn forks a worker, and exits with code 78 on the
first failure. Check 11 (the ops-config snapshot) runs in each worker's lifespan. Every failure
names the settings and never their values.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from docchat_core.auth import AuthSettings, check_test_client
from docchat_core.chat import MODEL_CAPABILITIES
from docchat_core.ingestion.settings import (
    MODEL_NAME_PATTERN,
    DeploymentName,
    EmbeddingModelCatalog,
    TokenEncoding,
    UploadSettings,
)
from docchat_core.settings import (
    CoreSettings,
    DocChatSettings,
    EndpointUrl,
    LocalSettings,
    SettingsValidationError,
    check_endpoints,
    check_identity_client_id,
    check_local_emulators,
    check_local_environment,
    load_settings,
)
from docchat_core.sql.settings import SqlSettings, check_sql_server
from docchat_core.telemetry import TelemetrySettings, check_telemetry_log_level

COSMOS_DATABASE_NAME: Final = "docchat"

type ReasoningEffort = Literal["none", "low", "medium", "high"]
type PromptCache = Literal["disabled", "automatic"]

ModelName = Annotated[str, StringConstraints(pattern=MODEL_NAME_PATTERN)]
ModelVersion = Annotated[str, StringConstraints(pattern=r"^\S{1,64}$")]


class ApiSettings(DocChatSettings):
    """Read once at start-up by `gunicorn.conf.py` and by each worker's lifespan.

    The deployment names, models, versions and encodings come from the environment file and
    default to the catalogue's value, which is the same in every environment; the endpoints and
    the worker count come from resource outputs and SKU maps and have no default.
    """

    cosmos_endpoint: EndpointUrl
    cosmos_database: Literal["docchat"] = COSMOS_DATABASE_NAME
    search_endpoint: EndpointUrl
    search_timeout_seconds: Annotated[int, Field(ge=2, le=30)] = 10
    openai_endpoint: EndpointUrl
    openai_chat_deployment: DeploymentName = "chat"
    openai_chat_model: ModelName = "gpt-5.6-sol"
    openai_chat_model_version: ModelVersion = "2026-07-09"
    openai_chat_encoding: TokenEncoding = TokenEncoding.O200K_BASE
    openai_chat_reasoning_effort: ReasoningEffort = "low"
    openai_chat_prompt_cache: PromptCache = "disabled"
    openai_small_deployment: DeploymentName = "chat-small"
    openai_small_model: ModelName = "gpt-5.6-terra"
    openai_small_model_version: ModelVersion = "2026-07-09"
    openai_small_encoding: TokenEncoding = TokenEncoding.O200K_BASE
    openai_small_reasoning_effort: ReasoningEffort = "none"
    openai_small_prompt_cache: PromptCache = "disabled"
    openai_connect_timeout_seconds: Annotated[int, Field(ge=1, le=30)] = 10
    openai_read_timeout_seconds: Annotated[int, Field(ge=10, le=120)] = 60
    contentsafety_endpoint: EndpointUrl
    contentsafety_timeout_seconds: Annotated[int, Field(ge=1, le=15)] = 5
    api_workers: Annotated[int, Field(ge=1, le=16)]
    api_graceful_timeout_seconds: Annotated[int, Field(ge=30, le=110)] = 110
    api_worker_timeout_seconds: Annotated[int, Field(ge=30, le=120)] = 120

    @property
    def chat_small_efforts(self) -> frozenset[str]:
        """The chat-small model's accepted reasoning efforts, the row the ops-config `summary`
        and `memory` areas are checked against; empty for a model outside the table, which
        check 8 refuses first."""
        row = MODEL_CAPABILITIES.get(self.openai_small_model)
        return frozenset() if row is None else row.reasoning_efforts


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessSettings:
    """Every settings class the API loads, validated together."""

    core: CoreSettings
    auth: AuthSettings
    telemetry: TelemetrySettings
    sql: SqlSettings
    local: LocalSettings
    upload: UploadSettings
    embeddings: EmbeddingModelCatalog
    api: ApiSettings


# The per-deployment settings check 8 compares with each model's row: the model, the effort and
# the prompt-cache setting of the chat and chat-small deployments.
_DEPLOYMENTS: Final = (
    ("openai_chat_model", "openai_chat_reasoning_effort", "openai_chat_prompt_cache"),
    ("openai_small_model", "openai_small_reasoning_effort", "openai_small_prompt_cache"),
)


def check_models(api: ApiSettings) -> None:
    """Check 8, the capability table's part: each model is in `MODEL_CAPABILITIES`, its
    reasoning effort is in its row, and `disabled` prompt caching needs `prompt_cache_options`."""
    names = ApiSettings.setting_names()
    failing: dict[str, None] = {}
    for model_field, effort_field, cache_field in _DEPLOYMENTS:
        row = MODEL_CAPABILITIES.get(getattr(api, model_field))
        if row is None:
            failing[names[model_field]] = None
            continue
        if getattr(api, effort_field) not in row.reasoning_efforts:
            failing[names[effort_field]] = None
        if getattr(api, cache_field) == "disabled" and not row.prompt_cache_options:
            failing[names[cache_field]] = None
    if failing:
        raise SettingsValidationError(tuple(failing))


def _load_encoding(name: str) -> None:
    # Loaded only by the check: tiktoken reads the file and builds its tables at first use.
    import tiktoken  # noqa: PLC0415

    tiktoken.get_encoding(name)


def check_encodings(api: ApiSettings, load: Callable[[str], None] = _load_encoding) -> None:
    """Check 8, the tokenizer's part: the chat and chat-small encodings load from
    `TIKTOKEN_CACHE_DIR`. In the image the cache holds both files and nothing is downloaded, so
    a failure means the encoding can't be used on a turn either."""
    names = ApiSettings.setting_names()
    failing: dict[str, None] = {}
    for field_name in ("openai_chat_encoding", "openai_small_encoding"):
        encoding: TokenEncoding = getattr(api, field_name)
        try:
            load(encoding.value)
        # Whatever tiktoken raises (a missing file, a hash mismatch, no network), the setting
        # can't be served; the record names the setting only.
        except Exception:  # noqa: BLE001
            failing[names[field_name]] = None
    if failing:
        raise SettingsValidationError(tuple(failing))


def load_process_settings() -> ProcessSettings:
    """Checks 1 to 10 of `81-config.md` section 6.1, in order, on the process environment.

    Check 6 (distinct client IDs) and check 10 (the embedding catalogue's shape) are validators of
    `AuthSettings` and `EmbeddingModelCatalog`, so they fail inside check 1; the SQL server rule
    belongs to check 1 because `DOCCHAT_SQL_SERVER` has no default outside local-emulator mode.
    """
    core = load_settings(CoreSettings)
    local = load_settings(LocalSettings)
    sql = load_settings(SqlSettings)
    telemetry = load_settings(TelemetrySettings)
    auth = load_settings(AuthSettings)
    upload = load_settings(UploadSettings)
    embeddings = load_settings(EmbeddingModelCatalog)
    api = load_settings(ApiSettings)
    check_sql_server(sql, local)
    check_local_emulators(local)
    check_endpoints(local, sql, upload, api)
    check_local_environment(core)
    check_test_client(core, auth)
    check_identity_client_id(core)
    check_models(api)
    check_telemetry_log_level(telemetry, local)
    return ProcessSettings(
        core=core,
        auth=auth,
        telemetry=telemetry,
        sql=sql,
        local=local,
        upload=upload,
        embeddings=embeddings,
        api=api,
    )
