"""Unit tests of the deploy checks (`83-infrastructure.md` section 10.7 step 10; T-081 and T-114
logic)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import deploy_checks
import pytest
from deploy_checks import (
    GrantState,
    Permission,
    SettingsInputs,
    SqlUnreachableError,
    compare_grants,
    compare_settings,
    expected_permissions,
    expected_settings,
    login_delay,
    wait_for_login,
)
from sql_principals import user_names
from sqlalchemy.exc import DBAPIError

from docchat_core.sql.engine import pyodbc  # type: ignore[attr-defined]
from docchat_core.sql.tables import DATABASE_ROLES

TENANT = "AAAAAAAA-0000-4000-8000-000000000001"
OUTPUTS = {
    "AZURE_RESOURCE_GROUP": "rg-docchat-dev",
    "AZURE_TENANT_ID": TENANT,
    "API_CLIENT_ID": "api-app",
    "WEB_CLIENT_ID": "web-app",
    "TEST_CLIENT_ID": "test-app",
    "DOCCHAT_API_IDENTITY_CLIENT_ID": "api-client",
    "DOCCHAT_API_IDENTITY_PRINCIPAL_ID": "api-principal",
    "DOCCHAT_INGESTION_IDENTITY_CLIENT_ID": "ingest-client",
    "DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID": "ingest-principal",
    "DOCCHAT_SQL_SERVER": "sql-docchat-dev-x.database.windows.net",
    "DOCCHAT_COSMOS_ENDPOINT": "https://cosmos-docchat-dev-x.documents.azure.com:443/",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://stdocchatdevx.blob.core.windows.net/",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": "https://stdocchatdevx.queue.core.windows.net/",
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev-x.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev-x.openai.azure.com",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT": "https://cs-docchat-dev-x.cognitiveservices.azure.com",
    "DOCCHAT_OPENAI_EMBEDDING_MODELS": '[{"model":"text-embedding-3-large"}]',
    "DOCCHAT_MODEL_STUB_APP_NAME": "app-docchat-stub-load-x",
}
DEPLOYMENT = {
    "name": "chat",
    "model": "gpt-5.6-sol",
    "version": "2026-07-09",
    "encoding": "o200k_base",
}
CONFIG = {
    "telemetrySamplingRatio": 1.0,
    "storage": {"malwareScanning": False},
    "search": {"sku": "basic"},
    "api": {"sku": "B1"},
    "ingestion": {"sku": "EP1"},
    "openAi": {
        "deployments": {
            "chat": DEPLOYMENT,
            "chatSmall": {**DEPLOYMENT, "name": "chat-small", "model": "gpt-5.6-terra"},
            "tableSummary": {**DEPLOYMENT, "name": "ingest-summary"},
        }
    },
}


def spec_defaults() -> dict[str, dict[str, str]]:
    """The owners' defaults of `81-config.md` section 2.2, the content of
    `infra/config/app-settings.json`."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "81-config.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            section = text[text.index("### 2.2 `infra/config/app-settings.json`") :]
            block = re.search(r"```json\n(.*?)\n```", section, re.S)
            assert block is not None
            loaded: dict[str, dict[str, str]] = json.loads(block.group(1))
            return loaded
    raise FileNotFoundError("81-config.md")


def inputs(environment: Any = "dev") -> SettingsInputs:
    return SettingsInputs(
        environment=environment, defaults=spec_defaults(), config=CONFIG, outputs=OUTPUTS
    )


def test_t114_site_sets_have_the_catalogues_sizes() -> None:
    assert len(expected_settings("api", inputs())) == 57
    assert len(expected_settings("ingestion", inputs())) == 43
    assert len(expected_settings("modelStub", inputs("load"))) == 17


def test_values_from_each_source() -> None:
    api = expected_settings("api", inputs())
    assert api["DOCCHAT_SQL_THREADS"] == "8"
    assert api["DOCCHAT_CORE_ENVIRONMENT"] == "dev"
    assert api["DOCCHAT_CORE_IDENTITY_CLIENT_ID"] == "api-client"
    assert (
        api["APPLICATIONINSIGHTS_AUTHENTICATION_STRING"] == "Authorization=AAD;ClientId=api-client"
    )
    assert api["APPLICATIONINSIGHTS_CONNECTION_STRING"] is None
    assert api["DOCCHAT_API_WORKERS"] == "1"
    assert api["DOCCHAT_SEARCH_MAX_INDEXES"] == "15"
    assert api["WEBSITE_AUTH_AAD_ALLOWED_TENANTS"] == TENANT.lower()
    assert api["DOCCHAT_OPENAI_SMALL_DEPLOYMENT"] == "chat-small"
    assert api["DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED"] == "false"
    ingestion = expected_settings("ingestion", inputs())
    assert ingestion["DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS"] == "120"
    assert ingestion["AzureFunctionsJobHost__extensions__durableTask__hubName"] == "docchatdev"
    assert ingestion["FUNCTIONS_WORKER_PROCESS_COUNT"] == "1"
    assert ingestion["DOCCHAT_INGESTION_QUEUE__clientId"] == "ingest-client"
    assert ingestion["DOCCHAT_DOCINTEL_ENDPOINT"] is None
    assert ingestion["WEBSITES_CONTAINER_START_TIME_LIMIT"] == "900"


def test_compare_settings() -> None:
    expected: dict[str, str | None] = {"A": "1.0", "B": "x", "C": None, "D": '[{"a": 1}]'}
    actual = {"A": "1", "B": "y", "C": "anything", "D": '[{"a":1}]', "E": "z"}
    assert compare_settings("api", expected, actual) == [
        "api: E extra",
        "api: B value differs",
    ]
    assert compare_settings("api", {"A": "x"}, {}) == ["api: A missing"]
    assert compare_settings(
        "api", {"K": None}, {"K": "DefaultEndpointsProtocol=https;AccountKey=x"}
    ) == ["api: K holds a secret"]
    assert compare_settings("api", {"F": "true"}, {"F": "True"}) == []


def test_check_app_settings_reads_each_site(tmp_path: Path) -> None:
    (tmp_path / "infra" / "config").mkdir(parents=True)
    (tmp_path / "infra" / "env").mkdir(parents=True)
    (tmp_path / "infra" / "config" / "app-settings.json").write_text(json.dumps(spec_defaults()))
    (tmp_path / "infra" / "env" / "dev.json").write_text(json.dumps(CONFIG))
    sites = {
        "app-docchat-api-dev-x": expected_settings("api", inputs()),
        "func-docchat-ingest-dev-x": expected_settings("ingestion", inputs()),
    }
    calls: list[Sequence[str]] = []

    def az(arguments: Sequence[str]) -> Any:
        calls.append(arguments)
        if arguments[0] == "resource":
            service = arguments[arguments.index("--tag") + 1].split("=")[1]
            name = "app-docchat-api-dev-x" if service == "api" else "func-docchat-ingest-dev-x"
            return [{"name": name}]
        name = arguments[arguments.index("--name") + 1]
        listed = [{"name": key, "value": value or "set"} for key, value in sites[name].items()]
        if name.startswith("func"):
            listed.append({"name": "OTEL_TRACES_SAMPLER", "value": "x"})
        return listed

    differences = deploy_checks.check_app_settings("dev", az=az, root=tmp_path, outputs=OUTPUTS)
    assert differences == ["ingestion: OTEL_TRACES_SAMPLER extra"]
    assert calls[0][:2] == ["resource", "list"]
    assert "rg-docchat-dev" in calls[0]


def matrix_state(environment: Any = "dev") -> GrantState:
    names = user_names(environment)
    principals: list[tuple[str, str]] = [(name, kind) for name, kind in names.values()]
    principals += [(role, "R") for role in DATABASE_ROLES]
    principals += [("dbo", "S"), ("guest", "S"), ("public", "R"), ("db_owner", "R")]
    memberships = [(role, names[role][0]) for role in DATABASE_ROLES] + [("db_owner", "dbo")]
    permissions = [
        *expected_permissions(),
        # Every user holds CONNECT on the database; it is no right on an `app` object.
        Permission("DATABASE", "id-docchat-api-dev", None, None, None, "CONNECT", "GRANT"),
    ]
    return GrantState(permissions, principals, memberships)


def test_t081_the_matrix_passes() -> None:
    assert compare_grants("dev", matrix_state()) == []


def test_t081_every_difference_is_named() -> None:
    state = matrix_state()
    permissions = [
        p
        for p in state.permissions
        if not (
            p.grantee == "docchat_api"
            and p.object_name == "users"
            and p.permission_name == "SELECT"
        )
    ]
    permissions += [
        Permission("OBJECT_OR_COLUMN", "public", "app", "users", None, "SELECT", "GRANT"),
        Permission(
            "OBJECT_OR_COLUMN",
            "docchat_api",
            "app",
            "document_versions",
            "scan_status",
            "UPDATE",
            "GRANT",
        ),
        Permission("SCHEMA", "docchat_ingestion", "app", None, None, "SELECT", "GRANT"),
        Permission("DATABASE", "docchat_config_operator", None, None, None, "CONNECT", "GRANT"),
        Permission(
            "OBJECT_OR_COLUMN", "id-docchat-api-dev", "app", "ops_config", None, "UPDATE", "DENY"
        ),
    ]
    principals = [*state.principals, ("stray", "E")]
    principals.remove(("sg-docchat-dev-user-data-operators", "X"))
    memberships = [
        *state.memberships,
        ("db_datareader", "id-docchat-api-dev"),
        ("docchat_api", "stray"),
    ]
    differences = compare_grants("dev", GrantState(permissions, principals, memberships))
    assert set(differences) == {
        "docchat_api missing SELECT on app.users",
        "public extra SELECT on app.users",
        "docchat_api extra UPDATE on app.document_versions(scan_status)",
        "docchat_ingestion extra SELECT on schema app",
        "docchat_config_operator extra CONNECT on the database",
        "id-docchat-api-dev extra UPDATE (DENY) on app.ops_config",
        "sg-docchat-dev-user-data-operators missing contained user of type X",
        "stray extra contained user",
        "id-docchat-api-dev member of built-in role db_datareader",
        "stray extra member of docchat_api",
    }


def test_a_missing_role_and_membership() -> None:
    state = matrix_state()
    principals = [p for p in state.principals if p != ("docchat_ingestion", "R")]
    memberships = [m for m in state.memberships if m[0] != "docchat_ingestion"]
    differences = compare_grants("dev", GrantState(state.permissions, principals, memberships))
    assert differences == [
        "docchat_ingestion missing role",
        "id-docchat-ingest-dev missing member of docchat_ingestion",
    ]


def driver_error(number: int) -> DBAPIError:
    error = pyodbc.Error("08S01", f"[08S01] login failed ({number}) (SQLDriverConnect)")
    wrapped: DBAPIError = DBAPIError.instance("SELECT 1", {}, error, pyodbc.Error)
    return wrapped


def test_login_delays() -> None:
    assert login_delay(0, 5) == 5
    assert login_delay(59, 5) == 5
    assert login_delay(61, 5) == 10
    assert login_delay(200, 20) == 30


def test_wait_for_login_retries_a_resuming_database() -> None:
    clock = [0.0]
    sleeps: list[float] = []
    attempts = [driver_error(40613), driver_error(40615), None]

    def probe() -> None:
        outcome = attempts.pop(0)
        if outcome is not None:
            raise outcome

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    wait_for_login(probe, monotonic=lambda: clock[0], sleep=sleep)
    assert sleeps == [5, 5]


def test_wait_for_login_gives_up_after_five_minutes() -> None:
    clock = [0.0]

    def probe() -> None:
        raise driver_error(40613)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    with pytest.raises(SqlUnreachableError, match="40613"):
        wait_for_login(probe, monotonic=lambda: clock[0], sleep=sleep)
    assert clock[0] <= 300


def test_other_login_errors_are_unreachable_at_once() -> None:
    def probe() -> None:
        raise driver_error(18456)

    with pytest.raises(SqlUnreachableError, match="DBAPIError"):
        wait_for_login(probe, monotonic=lambda: 0.0, sleep=lambda seconds: None)


def test_main_reports_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        deploy_checks, "check_grants", lambda environment: ["x missing SELECT on app.y"]
    )
    assert deploy_checks.main(["grants", "--env", "dev"]) == 2
    line = json.loads(capsys.readouterr().err)
    assert line["code"] == "grants_mismatch"
    assert line["detail"] == "x missing SELECT on app.y"


def test_main_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def unreachable(environment: str) -> list[str]:
        raise SqlUnreachableError("error 40615 after 300 s")

    monkeypatch.setattr(deploy_checks, "check_grants", unreachable)
    assert deploy_checks.main(["grants", "--env", "dev"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "sql_unreachable"


def test_main_passes_and_refuses_grants_outside_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(deploy_checks, "check_app_settings", lambda environment: [])
    assert deploy_checks.main(["app-settings", "--env", "prod"]) == 0
    with pytest.raises(SystemExit):
        deploy_checks.main(["grants", "--env", "prod"])
    capsys.readouterr()


def test_main_reports_a_check_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def broken(environment: str) -> list[str]:
        raise FileNotFoundError("az")

    monkeypatch.setattr(deploy_checks, "check_app_settings", broken)
    assert deploy_checks.main(["app-settings", "--env", "dev"]) == 2
    line = json.loads(capsys.readouterr().err)
    assert line["code"] == "app_settings_mismatch"
    assert line["detail"] == "the check couldn't run: FileNotFoundError"


# --- static-site (T-475) ----------------------------------------------------------------------

SITE_ID = (
    "/subscriptions/s/resourceGroups/rg-docchat-dev/providers/Microsoft.Web/staticSites/"
    "stapp-docchat-dev"
)
FILES_ONLY_PROPERTIES: dict[str, Any] = {
    "stagingEnvironmentPolicy": "Disabled",
    "allowConfigFileUpdates": True,
    "linkedBackends": [],
    "userProvidedFunctionApps": None,
    "repositoryUrl": None,
}
FILES_ONLY_SITE = {"id": SITE_ID, "properties": FILES_ONLY_PROPERTIES}


class RecordedArm:
    """`az` answers for one static site, recording each call's method and URL."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, arguments: Sequence[str]) -> Any:
        if arguments[0] == "resource":
            assert "azd-service-name=web" in arguments
            assert "Microsoft.Web/staticSites" in arguments
            return [{"id": SITE_ID, "name": "stapp-docchat-dev"}]
        assert arguments[:2] == ["rest", "--method"]
        method, url = arguments[2], arguments[4]
        self.calls.append((method, url))
        path = url.removeprefix(f"https://management.azure.com{SITE_ID}").split("?")[0]
        assert url.endswith("?api-version=2025-03-01")
        return self.answers[path]


def recorded(**overrides: Any) -> RecordedArm:
    answers: dict[str, Any] = {
        "": FILES_ONLY_SITE,
        "/linkedBackends": {"value": []},
        "/functions": {"value": []},
        "/listAppSettings": {"properties": {}},
        "/listFunctionAppSettings": {"properties": {}},
    }
    answers.update({f"/{key}" if key != "site" else "": value for key, value in overrides.items()})
    return RecordedArm(answers)


def test_t475_a_site_that_serves_files_only_passes() -> None:
    az = recorded()
    assert deploy_checks.check_static_site("dev", az=az, outputs=OUTPUTS) == []
    assert (
        "post",
        f"https://management.azure.com{SITE_ID}/listAppSettings?api-version=2025-03-01",
    ) in az.calls
    assert {method for method, _ in az.calls} == {"get", "post"}


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"linkedBackends": {"value": [{"name": "backend"}]}}, "1 linked backend(s)"),
        ({"functions": {"value": [{"name": "api-hello"}]}}, "managed functions: api-hello"),
        (
            {"listAppSettings": {"properties": {"AAD_CLIENT_SECRET": "x"}}},
            "app settings: AAD_CLIENT_SECRET",
        ),
        (
            {"listFunctionAppSettings": {"properties": {"KEY": "x"}}},
            "function app settings: KEY",
        ),
        (
            {"site": {"properties": {**FILES_ONLY_PROPERTIES, "repositoryUrl": "https://x"}}},
            "repositoryUrl is set",
        ),
        (
            {"site": {"properties": {"stagingEnvironmentPolicy": "Enabled"}}},
            "stagingEnvironmentPolicy isn't Disabled",
        ),
        (
            {
                "site": {
                    "properties": {
                        **FILES_ONLY_PROPERTIES,
                        "userProvidedFunctionApps": [{}],
                    }
                }
            },
            "1 linked function app(s)",
        ),
    ],
)
def test_t475_each_way_of_doing_more_than_serving_files_is_named(
    overrides: dict[str, Any], problem: str
) -> None:
    problems = deploy_checks.check_static_site("dev", az=recorded(**overrides), outputs=OUTPUTS)
    assert problem in problems
    # A setting is named, never its value.
    assert not any(part == "x" for found in problems for part in found.split(": ")[1:])


def test_t475_the_failure_line_carries_no_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        deploy_checks, "check_static_site", lambda environment: ["managed functions: f"]
    )
    assert deploy_checks.main(["static-site", "--env", "prod"]) == 2
    line = json.loads(capsys.readouterr().err)
    assert line["code"] is None
    assert line["detail"] == "managed functions: f"
    monkeypatch.setattr(deploy_checks, "check_static_site", lambda environment: [])
    assert deploy_checks.main(["static-site", "--env", "load"]) == 0
