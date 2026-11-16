"""Ingestion's settings models (`14-ingestion.md` section 18): `IngestionOpsConfig`, the `ingestion`
area of the ops-config root model `docchat_core.opsconfig.OpsConfig` (`81-config.md` section 3.8),
and the app-settings classes `81-config.md` section 2.1 places here: `UploadSettings` (API),
`IngestionAppSettings` (ingestion app) and `EmbeddingModelCatalog` (both apps).

The area model follows the rules of every ops-config area (`81-config.md` section 3.1): each field
is aliased to its full dotted key, carries its range and has no code default, because the database
is the source; unknown fields are forbidden and values are typed data (a JSON integer, never a
string or a float that happens to be whole). It is dumped with `model_dump(mode="json",
by_alias=True)` into the dispatcher's and each item's orchestration input, so the snapshot an item
runs with is the dotted keys (`14-ingestion.md` section 5.2).

The app-settings classes derive from `DocChatSettings` (prefix `DOCCHAT_`, read once at start-up,
frozen). A setting whose value comes from a Bicep output, a SKU map or an environment-file field
that differs per environment has no code default, so a process given none stops before it serves;
a setting with one owner default for every environment keeps that default.

This module imports nothing of `docchat_core.opsconfig`, which imports it for the root model.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from docchat_core.settings import BoolSetting, DocChatSettings, EndpointUrl


class IngestionOpsConfig(BaseModel):
    """The 25 integer keys the dispatcher reads at each wake and passes to each item."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    max_parallel_items: int = Field(alias="ingestion.max_parallel_items", ge=1, le=16)
    active_wake_seconds: int = Field(alias="ingestion.active_wake_seconds", ge=15, le=300)
    idle_wake_minutes: int = Field(alias="ingestion.idle_wake_minutes", ge=5, le=60)
    scan_wait_minutes: int = Field(alias="ingestion.scan_wait_minutes", ge=30, le=1440)
    stale_upload_hours: int = Field(alias="ingestion.stale_upload_hours", ge=1, le=168)
    orphan_pending_minutes: int = Field(alias="ingestion.orphan_pending_minutes", ge=5, le=1440)
    cleanup_retry_minutes: int = Field(alias="ingestion.cleanup_retry_minutes", ge=5, le=1440)
    max_item_claims: int = Field(alias="ingestion.max_item_claims", ge=1, le=10)
    retry_max_attempts: int = Field(alias="ingestion.retry_max_attempts", ge=1, le=10)
    retry_first_delay_seconds: int = Field(
        alias="ingestion.retry_first_delay_seconds", ge=1, le=300
    )
    retry_max_delay_seconds: int = Field(alias="ingestion.retry_max_delay_seconds", ge=10, le=540)
    activity_time_budget_seconds: int = Field(
        alias="ingestion.activity_time_budget_seconds", ge=60, le=1200
    )
    max_pages_per_document: int = Field(alias="ingestion.max_pages_per_document", ge=1, le=2000)
    pagewise_pages_per_extract: int = Field(
        alias="ingestion.pagewise_pages_per_extract", ge=1, le=200
    )
    max_parse_seconds_per_document: int = Field(
        alias="ingestion.max_parse_seconds_per_document", ge=900, le=43_200
    )
    embedding_batch_max_inputs: int = Field(
        alias="ingestion.embedding_batch_max_inputs", ge=1, le=2048
    )
    embedding_batch_max_tokens: int = Field(
        alias="ingestion.embedding_batch_max_tokens", ge=8192, le=300_000
    )
    embedding_concurrency: int = Field(alias="ingestion.embedding_concurrency", ge=1, le=8)
    index_batch_max_documents: int = Field(
        alias="ingestion.index_batch_max_documents", ge=1, le=1000
    )
    index_batch_max_bytes: int = Field(
        alias="ingestion.index_batch_max_bytes", ge=1_000_000, le=15_000_000
    )
    table_summary_concurrency: int = Field(alias="ingestion.table_summary_concurrency", ge=1, le=16)
    table_summary_max_input_tokens: int = Field(
        alias="ingestion.table_summary_max_input_tokens", ge=500, le=20_000
    )
    table_summary_max_output_tokens: int = Field(
        alias="ingestion.table_summary_max_output_tokens", ge=50, le=500
    )
    run_retention_days: int = Field(alias="ingestion.run_retention_days", ge=30, le=3650)
    durable_history_retention_days: int = Field(
        alias="ingestion.durable_history_retention_days", ge=7, le=365
    )


# --- App settings ---------------------------------------------------------------------------------

EMBEDDING_DIMENSIONS: Final = (256, 1024, 3072)
"""The dimensions `CK_search_indexes_dimensions` and `CK_physical_indexes_dimensions` admit, so a
catalogue entry can name only dimensions an index row can store."""

DEPLOYMENT_NAME_PATTERN: Final = r"^[A-Za-z0-9_.\-]{2,64}$"
"""An Azure OpenAI deployment name: 2 to 64 letters, digits, `-`, `_` and `.`."""

MODEL_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9.\-]{0,63}$"
"""A model name as the audit metadata of `index.created` records it."""

DeploymentName = Annotated[str, StringConstraints(pattern=DEPLOYMENT_NAME_PATTERN)]


class TokenEncoding(StrEnum):
    """A `tiktoken` encoding the image carries (`TIKTOKEN_CACHE_DIR`)."""

    O200K_BASE = "o200k_base"
    CL100K_BASE = "cl100k_base"


def _unique_dimensions(values: tuple[int, ...]) -> tuple[int, ...]:
    if len(set(values)) != len(values):
        raise ValueError("each dimension appears once")
    if any(value not in EMBEDDING_DIMENSIONS for value in values):
        raise ValueError("dimensions are 256, 1024 or 3072")
    return values


class EmbeddingModel(BaseModel):
    """One entry of `DOCCHAT_OPENAI_EMBEDDING_MODELS` (`81-config.md` section 2.3): exactly the
    eight members `main.bicep` serialises, any other member refused."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=False)

    model: Annotated[str, StringConstraints(pattern=MODEL_NAME_PATTERN)]
    model_version: Annotated[str, StringConstraints(pattern=r"^\S{1,32}$")] = Field(
        alias="modelVersion"
    )
    query_deployment: DeploymentName = Field(alias="queryDeployment")
    ingestion_deployment: DeploymentName = Field(alias="ingestionDeployment")
    dimensions: Annotated[tuple[int, ...], Field(min_length=1), AfterValidator(_unique_dimensions)]
    default_dimensions: int = Field(alias="defaultDimensions")
    max_input_tokens: int = Field(alias="maxInputTokens", ge=1)
    encoding: TokenEncoding

    @model_validator(mode="after")
    def _default_is_offered(self) -> Self:
        if self.default_dimensions not in self.dimensions:
            raise ValueError("defaultDimensions is one of dimensions")
        return self


DEFAULT_EMBEDDING_MODELS_JSON: Final = (
    '[{"model":"text-embedding-3-large","modelVersion":"1",'
    '"queryDeployment":"embed-query-te3-large-v1",'
    '"ingestionDeployment":"embed-ingest-te3-large-v1",'
    '"dimensions":[256,1024,3072],"defaultDimensions":1024,"maxInputTokens":8192,'
    '"encoding":"cl100k_base"}]'
)
"""The setting's default value in every environment, byte for byte (`81-config.md` 2.3)."""

DEFAULT_EMBEDDING_MODELS: Final = tuple(
    EmbeddingModel.model_validate(entry) for entry in json.loads(DEFAULT_EMBEDDING_MODELS_JSON)
)


def _distinct_entries(entries: tuple[EmbeddingModel, ...]) -> tuple[EmbeddingModel, ...]:
    # A logical index records only the model name, so a name must find one entry; a deployment
    # serves one model and version, which `set-deployments` relies on.
    models = [entry.model for entry in entries]
    if len(set(models)) != len(models):
        raise ValueError("each model appears in one entry")
    deployments = [
        name for entry in entries for name in (entry.query_deployment, entry.ingestion_deployment)
    ]
    if len(set(deployments)) != len(deployments):
        raise ValueError("each deployment belongs to one entry")
    return entries


class EmbeddingModelCatalog(DocChatSettings):
    """`DOCCHAT_OPENAI_EMBEDDING_MODELS` (both apps): the models, deployments, dimensions and
    encodings a new physical index can use. A physical index copies its entry when it is
    created, so a later change of the setting never alters an existing index (section 8.2)."""

    openai_embedding_models: Annotated[
        tuple[EmbeddingModel, ...],
        Field(min_length=1, max_length=8),
        AfterValidator(_distinct_entries),
    ] = DEFAULT_EMBEDDING_MODELS

    def find(self, model: str) -> EmbeddingModel | None:
        """The entry of a model name, or `None` (`422 unsupported_embedding_model`)."""
        return next((entry for entry in self.openai_embedding_models if entry.model == model), None)

    def for_deployments(
        self, query_deployment: str, ingestion_deployment: str
    ) -> EmbeddingModel | None:
        """The entry whose deployment pair this is, or `None` (`set-deployments`)."""
        return next(
            (
                entry
                for entry in self.openai_embedding_models
                if entry.query_deployment == query_deployment
                and entry.ingestion_deployment == ingestion_deployment
            ),
            None,
        )


UPLOAD_MAX_FILES_PER_BATCH_LIMIT: Final = 100
"""The OpenAPI `maxItems` of preflight and submit, the setting's upper bound."""


class UploadSettings(DocChatSettings):
    """The API's upload and index-capacity settings (`14-ingestion.md` sections 2.7 and 18.1)."""

    upload_max_bytes: Annotated[int, Field(ge=1_048_576, le=209_715_200)] = 52_428_800
    upload_max_files_per_batch: Annotated[int, Field(ge=1, le=UPLOAD_MAX_FILES_PER_BATCH_LIMIT)] = (
        100
    )
    upload_concurrent_streams_per_worker: Annotated[int, Field(ge=1, le=16)] = 4
    search_max_indexes: Annotated[int, Field(ge=2, le=200)]
    storage_blob_endpoint: EndpointUrl
    storage_queue_endpoint: EndpointUrl
    storage_malware_scanning_enabled: BoolSetting


class IngestionAppSettings(DocChatSettings):
    """The ingestion app's own settings (`14-ingestion.md` section 18.1); start-up check 2 loads
    them with `EmbeddingModelCatalog`."""

    storage_blob_endpoint: EndpointUrl
    storage_queue_endpoint: EndpointUrl
    storage_malware_scanning_enabled: BoolSetting
    search_endpoint: EndpointUrl
    openai_endpoint: EndpointUrl
    openai_table_summary_deployment: DeploymentName = "ingest-summary"
    openai_table_summary_encoding: TokenEncoding = TokenEncoding.O200K_BASE
    docintel_endpoint: EndpointUrl
    ingestion_parse_memory_limit_mb: Annotated[int, Field(ge=512, le=6144)] = 2048
    ingestion_parse_timeout_seconds: Annotated[int, Field(ge=60, le=2400)] = 1200
    ingestion_parse_slots_per_process: Annotated[int, Field(ge=1, le=4)] = 1
