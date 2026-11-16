"""T-115, the app-settings defaults unit test (`81-config.md` section 2.2, acceptance criterion 2).

`infra/config/app-settings.json` has exactly the keys `api`, `ingestion` and `modelStub`; each
holds exactly the owner-default (**def**) settings that sections 2.3 to 2.6 give that site, with
the values of section 2.2's listing; and every value parses into its field of the site's settings
classes, read from the process environment as the deployed process reads it. The tables and the
listing are read from `docs/spec/81-config.md` when the test runs.
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic_settings import BaseSettings

from docchat_api.settings import ApiSettings, load_process_settings
from docchat_core.auth import AuthSettings
from docchat_core.ingestion.settings import (
    EmbeddingModelCatalog,
    IngestionAppSettings,
    UploadSettings,
)
from docchat_core.settings import CoreSettings, DocChatSettings, LocalSettings, load_settings
from docchat_core.sql.settings import SqlSettings
from docchat_core.telemetry import TelemetrySettings
from docchat_model_stub.app import StubSettings

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
CONFIG: Final = REPOSITORY_ROOT / "docs" / "spec" / "81-config.md"
DEFAULTS_FILE: Final = REPOSITORY_ROOT / "infra" / "config" / "app-settings.json"

BOTH_APPS: Final = "### 2.3 Settings on both the API and the ingestion app"
API_ONLY: Final = "### 2.4 Settings on the API only"
INGESTION_ONLY: Final = "### 2.5 Settings on the ingestion app only"
MODEL_STUB: Final = "### 2.6 Settings on the model stub"
LISTING: Final = "### 2.2 `infra/config/app-settings.json`"

API_CLASSES: Final[tuple[type[DocChatSettings], ...]] = (
    CoreSettings,
    AuthSettings,
    TelemetrySettings,
    SqlSettings,
    LocalSettings,
    UploadSettings,
    EmbeddingModelCatalog,
    ApiSettings,
)
INGESTION_CLASSES: Final[tuple[type[DocChatSettings], ...]] = (
    CoreSettings,
    TelemetrySettings,
    SqlSettings,
    LocalSettings,
    EmbeddingModelCatalog,
    IngestionAppSettings,
)
"""The settings classes each app reads (section 2.1)."""

DEPLOYED: Final = {
    "DOCCHAT_CORE_ENVIRONMENT": "dev",
    "DOCCHAT_CORE_IDENTITY_CLIENT_ID": "6f1d3c2a-7b8e-4c9d-a0b1-c2d3e4f5a6b7",
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "DOCCHAT_BUILD_COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "DOCCHAT_SQL_SERVER": "sql-docchat-dev-abc123.database.windows.net",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://stdocchatdatadevabc123.blob.core.windows.net/",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": "https://stdocchatdatadevabc123.queue.core.windows.net/",
    "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "false",
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev.openai.azure.com",
}
"""The settings both apps need from outside the defaults file, as a `dev` deploy sets them."""
API_ONLY_DEPLOYED: Final = {
    "DOCCHAT_AUTH_TENANT_ID": "5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    "DOCCHAT_AUTH_API_CLIENT_ID": "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b",
    "DOCCHAT_AUTH_WEB_CLIENT_ID": "1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6",
    "DOCCHAT_AUTH_TEST_CLIENT_ID": "9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5",
    "DOCCHAT_SEARCH_MAX_INDEXES": "15",
    "DOCCHAT_COSMOS_ENDPOINT": "https://cosmos-docchat-dev.documents.azure.com:443/",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT": "https://cs-docchat-dev.cognitiveservices.azure.com",
    "DOCCHAT_API_WORKERS": "1",
}
INGESTION_ONLY_DEPLOYED: Final = {
    "DOCCHAT_DOCINTEL_ENDPOINT": "https://di-docchat-dev.cognitiveservices.azure.com",
}
_HOST_VARIABLES: Final = ("WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID")


def _rows(heading: str) -> list[list[str]]:
    """The body rows of the first table under `heading`, each cell stripped of its code span."""
    lines = CONFIG.read_text(encoding="utf-8").splitlines()
    rows: list[list[str]] = []
    for line in lines[lines.index(heading) + 1 :]:
        if line.startswith("#"):
            break
        if not line.startswith("|"):
            if rows:
                break
            continue
        if set(line.strip()) <= set("|-: "):
            continue
        rows.append([cell.strip().strip("`") for cell in line.strip().strip("|").split(" | ")])
    return rows[1:]


def _def_names(heading: str) -> set[str]:
    """The settings of a section whose source is **def**: the owners' defaults."""
    return {row[0] for row in _rows(heading) if row[1] == "def"}


def _listing() -> dict[str, dict[str, str]]:
    text = CONFIG.read_text(encoding="utf-8")
    section = text[text.index(LISTING) :]
    block = re.search(r"```json\n(.*?)\n```", section, re.S)
    assert block is not None
    listed: dict[str, dict[str, str]] = json.loads(block.group(1))
    return listed


@pytest.fixture(scope="module")
def defaults() -> dict[str, dict[str, str]]:
    loaded: dict[str, dict[str, str]] = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process environment with no `DOCCHAT_*` setting and no App Service host variable."""
    for name in list(os.environ):
        if name.startswith("DOCCHAT_") or name in _HOST_VARIABLES:
            monkeypatch.delenv(name)
    return monkeypatch


def _as_setting(value: Any) -> str:
    """A parsed field back in its app-setting text form, to compare with the file's value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, re.Pattern):
        return str(value.pattern)
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _parsed(instances: list[BaseSettings], names: dict[str, tuple[int, str]]) -> dict[str, str]:
    """Each setting's parsed value from the instance of the class that reads it."""
    return {
        name: _as_setting(getattr(instances[index], field))
        for name, (index, field) in names.items()
    }


def _readers(classes: tuple[type[DocChatSettings], ...]) -> dict[str, tuple[int, str]]:
    """Each app-setting name the classes read, with the reading class's position and field."""
    readers: dict[str, tuple[int, str]] = {}
    for index, settings_class in enumerate(classes):
        for field, name in settings_class.setting_names().items():
            readers.setdefault(name, (index, field))
    return readers


def test_t115_the_file_holds_exactly_the_three_sites_with_the_listed_values(
    defaults: dict[str, dict[str, str]],
) -> None:
    assert list(defaults) == ["api", "ingestion", "modelStub"]
    assert defaults == _listing()


def test_t115_each_site_holds_exactly_its_def_settings(
    defaults: dict[str, dict[str, str]],
) -> None:
    both = _def_names(BOTH_APPS)

    assert set(defaults["api"]) == both | _def_names(API_ONLY)
    assert set(defaults["ingestion"]) == both | _def_names(INGESTION_ONLY)
    assert set(defaults["modelStub"]) == {row[0] for row in _rows(MODEL_STUB)}


def test_t115_every_api_value_parses_into_its_field(
    defaults: dict[str, dict[str, str]], environment: pytest.MonkeyPatch
) -> None:
    for name, value in {**DEPLOYED, **API_ONLY_DEPLOYED, **defaults["api"]}.items():
        environment.setenv(name, value)
    readers = _readers(API_CLASSES)

    settings = load_process_settings()

    assert set(defaults["api"]) <= set(readers)
    instances: list[BaseSettings] = [
        settings.core,
        settings.auth,
        settings.telemetry,
        settings.sql,
        settings.local,
        settings.upload,
        settings.embeddings,
        settings.api,
    ]
    parsed = _parsed(instances, {name: readers[name] for name in defaults["api"]})
    assert parsed == defaults["api"]


def test_t115_every_ingestion_value_parses_into_its_field(
    defaults: dict[str, dict[str, str]], environment: pytest.MonkeyPatch
) -> None:
    for name, value in {**DEPLOYED, **INGESTION_ONLY_DEPLOYED, **defaults["ingestion"]}.items():
        environment.setenv(name, value)
    readers = _readers(INGESTION_CLASSES)

    instances: list[BaseSettings] = [load_settings(cls) for cls in INGESTION_CLASSES]

    assert set(defaults["ingestion"]) <= set(readers)
    parsed = _parsed(instances, {name: readers[name] for name in defaults["ingestion"]})
    assert parsed == defaults["ingestion"]


def test_t115_every_model_stub_value_parses_into_its_field(
    defaults: dict[str, dict[str, str]], environment: pytest.MonkeyPatch
) -> None:
    for name, value in defaults["modelStub"].items():
        environment.setenv(name, value)
    prefix = str(StubSettings.model_config.get("env_prefix"))
    readers = {f"{prefix}{field}".upper(): (0, field) for field in StubSettings.model_fields}

    settings = StubSettings()

    assert set(defaults["modelStub"]) == set(readers)
    assert _parsed([settings], readers) == defaults["modelStub"]
