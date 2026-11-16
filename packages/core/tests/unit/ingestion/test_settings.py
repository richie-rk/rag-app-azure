"""Unit tests of ingestion's settings models: `IngestionOpsConfig`, the `ingestion` ops-config area
(T-119 for that area; `81-config.md` section 3.8; `14-ingestion.md` sections 5.2 and 18.2), and the
app-settings classes `UploadSettings`, `IngestionAppSettings` and `EmbeddingModelCatalog` (T-116
for these classes, the catalogue half of T-127; `81-config.md` sections 2.1 and 2.3 to 2.5)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from docchat_core.ingestion.settings import (
    DEFAULT_EMBEDDING_MODELS,
    DEFAULT_EMBEDDING_MODELS_JSON,
    EMBEDDING_DIMENSIONS,
    EmbeddingModel,
    EmbeddingModelCatalog,
    IngestionAppSettings,
    IngestionOpsConfig,
    TokenEncoding,
    UploadSettings,
)

type SpecTable = Callable[[str, str], list[list[str]]]

HEADING = "### 3.8 Area `ingestion` (`IngestionOpsConfig`)"
_RANGE_RE = re.compile(r"^([\d,]+) to ([\d,]+)$")


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def catalogue(spec_table: SpecTable) -> dict[str, tuple[int, int, int]]:
    """Each key's default and range ends from the catalogue's table."""
    rows: dict[str, tuple[int, int, int]] = {}
    for key, default, allowed, _ in spec_table("81-config.md", HEADING):
        found = _RANGE_RE.match(allowed)
        assert found is not None, key
        rows[key] = (_number(default), _number(found.group(1)), _number(found.group(2)))
    return rows


def defaults(spec_table: SpecTable) -> dict[str, JsonValue]:
    return {key: default for key, (default, _, _) in catalogue(spec_table).items()}


def refused(values: dict[str, JsonValue]) -> set[str]:
    with pytest.raises(ValidationError) as raised:
        IngestionOpsConfig.model_validate(values)
    return {str(detail["loc"][0]) for detail in raised.value.errors()}


def test_t119_the_model_declares_exactly_the_25_catalogued_keys(spec_table: SpecTable) -> None:
    keys = [field.alias for field in IngestionOpsConfig.model_fields.values()]
    assert keys == list(catalogue(spec_table))
    assert len(keys) == 25
    for name, field in IngestionOpsConfig.model_fields.items():
        assert field.alias == f"ingestion.{name}"
        assert field.annotation is int
        assert field.is_required()


def test_t119_the_defaults_are_accepted(spec_table: SpecTable) -> None:
    values = defaults(spec_table)
    model = IngestionOpsConfig.model_validate(values)
    assert model.max_parallel_items == 1
    assert model.retry_max_delay_seconds == 480
    assert model.model_dump(mode="json", by_alias=True) == values


def test_t119_each_key_accepts_its_ends_and_refuses_one_step_outside(
    spec_table: SpecTable,
) -> None:
    base = defaults(spec_table)
    for key, (_, low, high) in catalogue(spec_table).items():
        for inside in (low, high):
            IngestionOpsConfig.model_validate({**base, key: inside})
        for outside in (low - 1, high + 1):
            assert refused({**base, key: outside}) == {key}, (key, outside)


@pytest.mark.parametrize("value", [1.0, "1", True, None, [1]])
def test_t119_values_are_json_integers_only(spec_table: SpecTable, value: JsonValue) -> None:
    assert refused({**defaults(spec_table), "ingestion.max_parallel_items": value}) == {
        "ingestion.max_parallel_items"
    }


def test_t119_a_missing_key_is_refused_by_its_name(spec_table: SpecTable) -> None:
    base = defaults(spec_table)
    for key in base:
        assert refused({other: value for other, value in base.items() if other != key}) == {key}


def test_unknown_keys_and_field_names_are_refused(spec_table: SpecTable) -> None:
    base = defaults(spec_table)
    assert IngestionOpsConfig.model_config.get("extra") == "forbid"
    assert refused({**base, "ingestion.added_later": 1}) == {"ingestion.added_later"}
    by_name = {key.removeprefix("ingestion."): value for key, value in base.items()}
    with pytest.raises(ValidationError):
        IngestionOpsConfig.model_validate(by_name)


def test_the_snapshot_is_frozen_and_dumps_dotted_keys(spec_table: SpecTable) -> None:
    model = IngestionOpsConfig.model_validate(defaults(spec_table))
    with pytest.raises(ValidationError):
        model.max_parallel_items = 2  # type: ignore[misc]
    dumped = model.model_dump(mode="json")
    assert next(iter(dumped)) == "ingestion.max_parallel_items"
    assert IngestionOpsConfig.model_validate(dumped) == model


# --- App settings (`81-config.md` sections 2.1, 2.3 to 2.5; `14-ingestion.md` section 18.1) -------

ENDPOINTS = {
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://stdocchatdatadevabc.blob.core.windows.net/",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": "https://stdocchatdatadevabc.queue.core.windows.net/",
}
INGESTION_ENDPOINTS = {
    **ENDPOINTS,
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev-abc.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://aoai-docchat-dev-abc.openai.azure.com",
    "DOCCHAT_DOCINTEL_ENDPOINT": "https://di-docchat-dev-abc.cognitiveservices.azure.com",
}
SPEC_EMBEDDING_JSON = (
    '[{"model":"text-embedding-3-large","modelVersion":"1","queryDeployment":'
    '"embed-query-te3-large-v1","ingestionDeployment":"embed-ingest-te3-large-v1",'
    '"dimensions":[256,1024,3072],"defaultDimensions":1024,"maxInputTokens":8192,'
    '"encoding":"cl100k_base"}]'
)


def _set(env: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        env.setenv(name, value)


def _settings_classes_table(spec_table: SpecTable) -> dict[str, set[str]]:
    rows = spec_table("81-config.md", "### 2.1 How each site's settings are composed")
    return {row[0]: set(re.findall(r"`(DOCCHAT_[A-Z0-9_]+)`", row[2])) for row in rows}


def test_t116_each_class_reads_the_settings_the_catalogue_gives_it(
    spec_table: SpecTable,
) -> None:
    table = _settings_classes_table(spec_table)
    assert set(UploadSettings.setting_names().values()) == table["UploadSettings"]
    assert set(EmbeddingModelCatalog.setting_names().values()) == table["EmbeddingModelCatalog"]
    # The table names "the three DOCCHAT_INGESTION_PARSE_* settings" rather than listing them.
    parse_settings = {
        "DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB",
        "DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS",
        "DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS",
    }
    expected = table["IngestionAppSettings"] | parse_settings
    assert set(IngestionAppSettings.setting_names().values()) == expected


def test_upload_settings_defaults(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, {**ENDPOINTS, "DOCCHAT_SEARCH_MAX_INDEXES": "15"})
    settings_env.setenv("DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED", "false")
    settings = UploadSettings()
    assert settings.upload_max_bytes == 52_428_800
    assert settings.upload_max_files_per_batch == 100
    assert settings.upload_concurrent_streams_per_worker == 4
    assert settings.search_max_indexes == 15
    assert settings.storage_malware_scanning_enabled is False
    assert settings.storage_blob_endpoint == ENDPOINTS["DOCCHAT_STORAGE_BLOB_ENDPOINT"]


@pytest.mark.parametrize(
    "missing",
    [
        "DOCCHAT_SEARCH_MAX_INDEXES",
        "DOCCHAT_STORAGE_BLOB_ENDPOINT",
        "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
        "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED",
    ],
)
def test_upload_settings_without_a_code_default_are_required(
    settings_env: pytest.MonkeyPatch, missing: str
) -> None:
    values = {
        **ENDPOINTS,
        "DOCCHAT_SEARCH_MAX_INDEXES": "50",
        "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "true",
    }
    del values[missing]
    _set(settings_env, values)
    with pytest.raises(ValidationError):
        UploadSettings()


@pytest.mark.parametrize(
    ("name", "low", "high"),
    [
        ("DOCCHAT_UPLOAD_MAX_BYTES", 1_048_576, 209_715_200),
        ("DOCCHAT_UPLOAD_MAX_FILES_PER_BATCH", 1, 100),
        ("DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER", 1, 16),
        ("DOCCHAT_SEARCH_MAX_INDEXES", 2, 200),
    ],
)
def test_upload_settings_ranges(
    settings_env: pytest.MonkeyPatch, name: str, low: int, high: int
) -> None:
    _set(
        settings_env,
        {
            **ENDPOINTS,
            "DOCCHAT_SEARCH_MAX_INDEXES": "50",
            "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "true",
        },
    )
    for inside in (low, high):
        settings_env.setenv(name, str(inside))
        UploadSettings()
    for outside in (low - 1, high + 1):
        settings_env.setenv(name, str(outside))
        with pytest.raises(ValidationError):
            UploadSettings()


def test_ingestion_app_settings_defaults(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, {**INGESTION_ENDPOINTS, "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "true"})
    settings = IngestionAppSettings()
    assert settings.openai_table_summary_deployment == "ingest-summary"
    assert settings.openai_table_summary_encoding is TokenEncoding.O200K_BASE
    assert settings.ingestion_parse_memory_limit_mb == 2048
    assert settings.ingestion_parse_timeout_seconds == 1200
    assert settings.ingestion_parse_slots_per_process == 1
    assert settings.storage_malware_scanning_enabled is True


@pytest.mark.parametrize(
    ("name", "low", "high"),
    [
        ("DOCCHAT_INGESTION_PARSE_MEMORY_LIMIT_MB", 512, 6144),
        ("DOCCHAT_INGESTION_PARSE_TIMEOUT_SECONDS", 60, 2400),
        ("DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS", 1, 4),
    ],
)
def test_ingestion_app_settings_ranges(
    settings_env: pytest.MonkeyPatch, name: str, low: int, high: int
) -> None:
    _set(settings_env, {**INGESTION_ENDPOINTS, "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "true"})
    for inside in (low, high):
        settings_env.setenv(name, str(inside))
        IngestionAppSettings()
    for outside in (low - 1, high + 1):
        settings_env.setenv(name, str(outside))
        with pytest.raises(ValidationError):
            IngestionAppSettings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT", "x"),
        ("DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT", "bad name"),
        ("DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT", "d" * 65),
        ("DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING", "p50k_base"),
        ("DOCCHAT_DOCINTEL_ENDPOINT", "not a url"),
        ("DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED", "yes"),
    ],
)
def test_ingestion_app_settings_refuse_invalid_values(
    settings_env: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set(settings_env, {**INGESTION_ENDPOINTS, "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "true"})
    settings_env.setenv(name, value)
    with pytest.raises(ValidationError):
        IngestionAppSettings()


def test_ingestion_app_settings_accept_the_other_encoding(settings_env: pytest.MonkeyPatch) -> None:
    _set(settings_env, {**INGESTION_ENDPOINTS, "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "false"})
    settings_env.setenv("DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING", "cl100k_base")
    settings_env.setenv("DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT", "ingest.summary_2")
    settings = IngestionAppSettings()
    assert settings.openai_table_summary_encoding is TokenEncoding.CL100K_BASE


# --- The embedding catalogue (T-127, the `EmbeddingModelCatalog` half) ----------------------------


def _entry(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = json.loads(SPEC_EMBEDDING_JSON)[0]
    entry.update(changes)
    return entry


def _numbered(index: int) -> dict[str, Any]:
    return _entry(model=f"m{index}", queryDeployment=f"q-{index}", ingestionDeployment=f"i-{index}")


def test_t127_the_default_is_the_catalogues_json_byte_for_byte() -> None:
    assert DEFAULT_EMBEDDING_MODELS_JSON == SPEC_EMBEDDING_JSON
    document = next(
        parent / "docs" / "spec" / "81-config.md"
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "spec" / "81-config.md").is_file()
    )
    assert f"```json\n{SPEC_EMBEDDING_JSON}\n```" in document.read_text(encoding="utf-8")


def test_t127_the_default_catalogue(settings_env: pytest.MonkeyPatch) -> None:
    del settings_env
    catalog = EmbeddingModelCatalog()
    assert catalog.openai_embedding_models == DEFAULT_EMBEDDING_MODELS
    entry = catalog.find("text-embedding-3-large")
    assert entry is not None
    assert entry.model_version == "1"
    assert entry.query_deployment == "embed-query-te3-large-v1"
    assert entry.ingestion_deployment == "embed-ingest-te3-large-v1"
    assert entry.dimensions == (256, 1024, 3072)
    assert entry.default_dimensions == 1024
    assert entry.max_input_tokens == 8192
    assert entry.encoding is TokenEncoding.CL100K_BASE
    assert catalog.find("text-embedding-3-small") is None
    assert catalog.for_deployments("embed-query-te3-large-v1", "embed-ingest-te3-large-v1") is entry
    assert catalog.for_deployments("embed-ingest-te3-large-v1", "embed-query-te3-large-v1") is None


def test_t127_the_setting_is_read_as_json(settings_env: pytest.MonkeyPatch) -> None:
    second = _entry(
        model="text-embedding-3-small",
        queryDeployment="embed-query-te3-small-v1",
        ingestionDeployment="embed-ingest-te3-small-v1",
        dimensions=[256],
        defaultDimensions=256,
    )
    settings_env.setenv("DOCCHAT_OPENAI_EMBEDDING_MODELS", json.dumps([_entry(), second]))
    catalog = EmbeddingModelCatalog()
    assert [entry.model for entry in catalog.openai_embedding_models] == [
        "text-embedding-3-large",
        "text-embedding-3-small",
    ]
    settings_env.setenv(
        "DOCCHAT_OPENAI_EMBEDDING_MODELS", json.dumps([_numbered(index) for index in range(8)])
    )
    assert len(EmbeddingModelCatalog().openai_embedding_models) == 8


@pytest.mark.parametrize(
    "entries",
    [
        [_entry(key="te3-large-v1")],
        [_entry(defaultDimensions=512)],
        [_entry(dimensions=[256, 512], defaultDimensions=256)],
        [_entry(dimensions=[1024, 1024])],
        [_entry(dimensions=[])],
        [_entry(encoding="p50k_base")],
        [_entry(maxInputTokens=0)],
        [_entry(queryDeployment="x")],
        [_entry(model="Text Embedding")],
        [_entry(modelVersion="")],
        [{name: value for name, value in _entry().items() if name != "encoding"}],
        [_entry(model_version="1")],
        [],
        [_numbered(index) for index in range(9)],
        [_entry(), _entry(queryDeployment="q-2", ingestionDeployment="i-2")],
        [_entry(), _entry(model="other")],
        [_entry(ingestionDeployment="embed-query-te3-large-v1")],
    ],
)
def test_t127_the_catalogue_refuses_a_bad_entry(
    settings_env: pytest.MonkeyPatch, entries: list[dict[str, Any]]
) -> None:
    settings_env.setenv("DOCCHAT_OPENAI_EMBEDDING_MODELS", json.dumps(entries))
    with pytest.raises(ValidationError):
        EmbeddingModelCatalog()


def test_an_embedding_entry_is_read_by_its_json_names_only() -> None:
    with pytest.raises(ValidationError):
        EmbeddingModel.model_validate(
            {
                "model": "m",
                "model_version": "1",
                "query_deployment": "q1",
                "ingestion_deployment": "i1",
                "dimensions": [256],
                "default_dimensions": 256,
                "max_input_tokens": 1,
                "encoding": "cl100k_base",
            }
        )
    assert EMBEDDING_DIMENSIONS == (256, 1024, 3072)
