"""Deploy checks run after every `full` and `images-only` deploy (`83-infrastructure.md`
section 10.7 step 10), and the Static Web Apps resource check after each provision
(`84-testing.md` section 8.3, T-475), as the environment's deploy identity:

    uv run --frozen python scripts/deploy/deploy_checks.py app-settings --env <env>
    uv run --frozen python scripts/deploy/deploy_checks.py grants --env dev
    uv run --frozen python scripts/deploy/deploy_checks.py static-site --env <env>

`app-settings` (`app_settings_mismatch`) compares each site's app settings, read with
`az webapp config appsettings list`, with the composed set of section 8.9 and the values that
`infra/config/app-settings.json`, `infra/env/<env>.json` and the azd outputs give; it names each
differing setting and never a value. `grants` (`grants_mismatch`) waits for a SQL login as the
deploy identity (`APP=docchat-grants`, token from the Azure CLI) and compares
`sys.database_permissions`, `sys.database_principals` and `sys.database_role_members` with the
grant matrix and the principal table of `80-data-model.md` section 2.22. `static-site` reads the
static site, its linked backends, its managed functions and its app and function-app settings
through Azure Resource Manager (`az rest`, through Contributor) and fails unless the site serves
files only (`82-security.md` SEC-64; `83-infrastructure.md` section 2.2): no linked backend or
user-provided function app, no managed function, no repository link or build properties,
`stagingEnvironmentPolicy` `Disabled`, and no app setting, which is where a custom authentication
provider's registration would keep its client ID and secret. Section 10.18 gives that failure no
code, so its line carries `"code": null`. Any failure leaves the release deployed; the job fails
for an operator to investigate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sql_principals import (
    ENVIRONMENTS,
    REPOSITORY,
    Environment,
    access_token,
    read_azd_environment,
    report_failure,
    user_names,
)
from sqlalchemy import Connection

from docchat_core.sql import statements
from docchat_core.sql.engine import create_sync_engine, describe_driver_error, driver_error_of
from docchat_core.sql.tables import DATABASE_ROLES, GRANT_MATRIX, SCHEMA

EXIT_FAILURE: Final = 2
AZ_TIMEOUT_SECONDS: Final = 120

APP_SETTINGS_MISMATCH: Final = (
    "app_settings_mismatch",
    (
        "A site's app settings differ from the composed set; the detail names each differing "
        "setting, never a value. The release is deployed but the run fails for an operator to "
        "investigate."
    ),
)
GRANTS_MISMATCH: Final = (
    "grants_mismatch",
    (
        "The database's rights or principals differ from the grant matrix; the detail names each "
        "principal, object and right. The release is deployed but the run fails for an operator "
        "to investigate."
    ),
)
SQL_UNREACHABLE: Final = ("sql_unreachable", "The database didn't accept a connection in time.")
STATIC_SITE_NOT_FILES_ONLY: Final = (
    None,
    (
        "The Static Web App doesn't serve files only; the detail names each linked backend, "
        "managed function, setting or property found. The release is deployed but the run fails "
        "for an operator to investigate."
    ),
)

# --- app-settings ---------------------------------------------------------------------------

API_WORKERS: Final = {"B1": 1, "P0v3": 1, "P1v3": 2, "P2v3": 4}
WORKER_PROCESSES: Final = {"EP1": 1, "EP2": 2, "EP3": 4}
DURABLE_CONCURRENCY: Final = {"EP1": 2, "EP2": 4, "EP3": 8}
SEARCH_MAX_INDEXES: Final = {"basic": 15, "standard": 50}
SECRET_MARKERS: Final = (
    "AccountKey=",
    "SharedAccessSignature",
    "sig=",
    "Password=",
    "client_secret",
)
STUB_DEFAULT_KEYS: Final = "modelStub"


@dataclass(frozen=True)
class SettingsInputs:
    """Where the expected values come from: the owners' defaults, the environment file and the
    azd environment (outputs and the values the deploy job set)."""

    environment: Environment
    defaults: Mapping[str, Mapping[str, str]]
    config: Mapping[str, Any]
    outputs: Mapping[str, str]


def _path(config: Mapping[str, Any], dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _output(inputs: SettingsInputs, name: str) -> str | None:
    """An azd output's value, or `None` when the environment has none (only the name is then
    compared)."""
    value = inputs.outputs.get(name)
    return value or None


def expected_settings(site: str, inputs: SettingsInputs) -> dict[str, str | None]:
    """Section 8.9's settings for one site (`api`, `ingestion` or `modelStub`): each name with
    the value the catalogue states, or `None` where no deploy input holds it (the Application
    Insights connection string, the Document Intelligence endpoint, the host account's
    endpoints)."""
    if site == STUB_DEFAULT_KEYS:
        stub: dict[str, str | None] = dict(inputs.defaults[STUB_DEFAULT_KEYS])
        stub.update(
            {
                "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "false",
                "WEBSITES_PORT": "8000",
                "WEBSITES_CONTAINER_START_TIME_LIMIT": "300",
                "WEBSITE_HEALTHCHECK_MAXPINGFAILURES": "3",
            }
        )
        return stub
    config = inputs.config
    identity = "API" if site == "api" else "INGESTION"
    client_id = _output(inputs, f"DOCCHAT_{identity}_IDENTITY_CLIENT_ID")
    settings: dict[str, str | None] = dict(inputs.defaults[site])
    settings.update(
        {
            "APPLICATIONINSIGHTS_CONNECTION_STRING": None,
            "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": (
                f"Authorization=AAD;ClientId={client_id}" if client_id else None
            ),
            "DOCCHAT_TELEMETRY_SAMPLING_RATIO": _text(_path(config, "telemetrySamplingRatio")),
            "DOCCHAT_CORE_ENVIRONMENT": inputs.environment,
            "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": _output(
                inputs, f"DOCCHAT_{identity}_IDENTITY_PRINCIPAL_ID"
            ),
            "DOCCHAT_CORE_IDENTITY_CLIENT_ID": client_id,
            "DOCCHAT_SQL_SERVER": _output(inputs, "DOCCHAT_SQL_SERVER"),
            "DOCCHAT_SQL_DATABASE": "docchat",
            "DOCCHAT_STORAGE_BLOB_ENDPOINT": _output(inputs, "DOCCHAT_STORAGE_BLOB_ENDPOINT"),
            "DOCCHAT_STORAGE_QUEUE_ENDPOINT": _output(inputs, "DOCCHAT_STORAGE_QUEUE_ENDPOINT"),
            "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": _text(
                _path(config, "storage.malwareScanning")
            ),
            "DOCCHAT_SEARCH_ENDPOINT": _output(inputs, "DOCCHAT_SEARCH_ENDPOINT"),
            "DOCCHAT_OPENAI_ENDPOINT": _output(inputs, "DOCCHAT_OPENAI_ENDPOINT"),
            "DOCCHAT_OPENAI_EMBEDDING_MODELS": _output(inputs, "DOCCHAT_OPENAI_EMBEDDING_MODELS"),
            "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "false",
        }
    )
    if site == "api":
        settings.update(_api_settings(inputs))
    else:
        settings.update(_ingestion_settings(inputs, client_id))
    return settings


def _api_settings(inputs: SettingsInputs) -> dict[str, str | None]:
    config = inputs.config
    tenant = _output(inputs, "AZURE_TENANT_ID")
    settings: dict[str, str | None] = {
        "DOCCHAT_COSMOS_ENDPOINT": _output(inputs, "DOCCHAT_COSMOS_ENDPOINT"),
        "DOCCHAT_COSMOS_DATABASE": "docchat",
        "DOCCHAT_SEARCH_MAX_INDEXES": str(SEARCH_MAX_INDEXES[_path(config, "search.sku")]),
        "DOCCHAT_CONTENTSAFETY_ENDPOINT": _output(inputs, "DOCCHAT_CONTENTSAFETY_ENDPOINT"),
        "DOCCHAT_AUTH_TENANT_ID": tenant,
        "DOCCHAT_AUTH_API_CLIENT_ID": _output(inputs, "API_CLIENT_ID"),
        "DOCCHAT_AUTH_WEB_CLIENT_ID": _output(inputs, "WEB_CLIENT_ID"),
        # Empty in `prod`, where the test client doesn't exist.
        "DOCCHAT_AUTH_TEST_CLIENT_ID": inputs.outputs.get("TEST_CLIENT_ID", ""),
        "WEBSITE_AUTH_AAD_ALLOWED_TENANTS": tenant.lower() if tenant else None,
        "DOCCHAT_API_WORKERS": str(API_WORKERS[_path(config, "api.sku")]),
        "WEBSITES_PORT": "8000",
        "WEBSITES_CONTAINER_START_TIME_LIMIT": "300",
        "WEBSITES_CONTAINER_STOP_TIME_LIMIT": "120",
        "WEBSITE_HEALTHCHECK_MAXPINGFAILURES": "3",
    }
    for prefix, deployment in (("CHAT", "chat"), ("SMALL", "chatSmall")):
        settings[f"DOCCHAT_OPENAI_{prefix}_DEPLOYMENT"] = _text(
            _path(config, f"openAi.deployments.{deployment}.name")
        )
        settings[f"DOCCHAT_OPENAI_{prefix}_MODEL"] = _text(
            _path(config, f"openAi.deployments.{deployment}.model")
        )
        settings[f"DOCCHAT_OPENAI_{prefix}_MODEL_VERSION"] = _text(
            _path(config, f"openAi.deployments.{deployment}.version")
        )
        settings[f"DOCCHAT_OPENAI_{prefix}_ENCODING"] = _text(
            _path(config, f"openAi.deployments.{deployment}.encoding")
        )
    return settings


def _ingestion_settings(inputs: SettingsInputs, client_id: str | None) -> dict[str, str | None]:
    config = inputs.config
    sku = _path(config, "ingestion.sku")
    durable = "AzureFunctionsJobHost__extensions__durableTask__"
    return {
        "DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT": _text(
            _path(config, "openAi.deployments.tableSummary.name")
        ),
        "DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING": _text(
            _path(config, "openAi.deployments.tableSummary.encoding")
        ),
        "DOCCHAT_DOCINTEL_ENDPOINT": None,
        "DOCCHAT_INGESTION_QUEUE__queueServiceUri": _output(
            inputs, "DOCCHAT_STORAGE_QUEUE_ENDPOINT"
        ),
        "DOCCHAT_INGESTION_QUEUE__credential": "managedidentity",
        "DOCCHAT_INGESTION_QUEUE__clientId": client_id,
        "AzureWebJobsStorage__blobServiceUri": None,
        "AzureWebJobsStorage__queueServiceUri": None,
        "AzureWebJobsStorage__tableServiceUri": None,
        "AzureWebJobsStorage__credential": "managedidentity",
        "AzureWebJobsStorage__clientId": client_id,
        "FUNCTIONS_EXTENSION_VERSION": "~4",
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "FUNCTIONS_WORKER_PROCESS_COUNT": str(WORKER_PROCESSES[sku]),
        f"{durable}maxConcurrentActivityFunctions": str(DURABLE_CONCURRENCY[sku]),
        f"{durable}maxConcurrentOrchestratorFunctions": str(DURABLE_CONCURRENCY[sku]),
        f"{durable}hubName": f"docchat{inputs.environment}",
        "WEBSITES_CONTAINER_START_TIME_LIMIT": "900",
    }


def _same_value(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    try:
        # Bicep writes numbers and JSON in its own spelling ("1" for 1.0, spaces in JSON).
        return bool(json.loads(expected) == json.loads(actual))
    except json.JSONDecodeError:
        return expected.lower() == actual.lower() and expected.lower() in {"true", "false"}


def compare_settings(
    site: str, expected: Mapping[str, str | None], actual: Mapping[str, str]
) -> list[str]:
    """Each difference as `<site>: <setting> <missing|extra|value differs|holds a secret>`."""
    differences = [f"{site}: {name} missing" for name in sorted(set(expected) - set(actual))]
    differences += [f"{site}: {name} extra" for name in sorted(set(actual) - set(expected))]
    for name in sorted(set(expected) & set(actual)):
        wanted = expected[name]
        if wanted is not None and not _same_value(wanted, actual[name]):
            differences.append(f"{site}: {name} value differs")
    for name, value in sorted(actual.items()):
        if any(marker in value for marker in SECRET_MARKERS):
            differences.append(f"{site}: {name} holds a secret")
    return differences


type AzRunner = Callable[[Sequence[str]], Any]


def run_az(arguments: Sequence[str]) -> Any:
    """`az <arguments> -o json`, parsed."""
    executable = shutil.which("az")
    if executable is None:
        raise FileNotFoundError("az")
    completed = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [executable, *arguments, "--only-show-errors", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=AZ_TIMEOUT_SECONDS,
    )
    return json.loads(completed.stdout or "null")


def site_name(az: AzRunner, resource_group: str, service: str) -> str:
    """The site tagged `azd-service-name=<service>`."""
    found = az(
        [
            "resource",
            "list",
            "--resource-group",
            resource_group,
            "--tag",
            f"azd-service-name={service}",
            "--resource-type",
            "Microsoft.Web/sites",
        ]
    )
    if not isinstance(found, list) or len(found) != 1:
        raise LookupError(f"one site tagged azd-service-name={service}")
    return str(found[0]["name"])


def site_settings(az: AzRunner, resource_group: str, name: str) -> dict[str, str]:
    listed = az(
        [
            "webapp",
            "config",
            "appsettings",
            "list",
            "--resource-group",
            resource_group,
            "--name",
            name,
        ]
    )
    return {str(item["name"]): str(item.get("value") or "") for item in listed}


def check_app_settings(
    environment: Environment,
    *,
    az: AzRunner = run_az,
    root: Path = REPOSITORY,
    outputs: Mapping[str, str] | None = None,
) -> list[str]:
    values = read_azd_environment(environment, root) if outputs is None else dict(outputs)
    inputs = SettingsInputs(
        environment=environment,
        defaults=json.loads((root / "infra" / "config" / "app-settings.json").read_text("utf-8")),
        config=json.loads((root / "infra" / "env" / f"{environment}.json").read_text("utf-8")),
        outputs=values,
    )
    resource_group = values.get("AZURE_RESOURCE_GROUP") or f"rg-docchat-{environment}"
    sites = [("api", site_name(az, resource_group, "api"))]
    sites.append(("ingestion", site_name(az, resource_group, "ingestion")))
    if environment == "load":
        # The model stub is no azd service; Bicep outputs its site's name.
        sites.append((STUB_DEFAULT_KEYS, values["DOCCHAT_MODEL_STUB_APP_NAME"]))
    differences: list[str] = []
    for site, name in sites:
        actual = site_settings(az, resource_group, name)
        differences.extend(compare_settings(site, expected_settings(site, inputs), actual))
    return differences


# --- grants ---------------------------------------------------------------------------------

LOGIN_RETRY_ERRORS: Final = frozenset({40613, 40615})
LOGIN_BUDGET_SECONDS: Final = 300.0
LOGIN_STEADY_DELAY_SECONDS: Final = 5.0
LOGIN_STEADY_PERIOD_SECONDS: Final = 60.0
LOGIN_MAX_DELAY_SECONDS: Final = 30.0
BUILT_IN_USERS: Final = frozenset({"dbo", "guest", "INFORMATION_SCHEMA", "sys"})
USER_TYPES: Final = frozenset({"S", "U", "G", "E", "X", "C", "K"})


class SqlUnreachableError(Exception):
    """No login within the budget, or a login error that isn't worth waiting for."""


@dataclass(frozen=True)
class Permission:
    class_desc: str
    grantee: str
    schema_name: str | None
    object_name: str | None
    column_name: str | None
    permission_name: str
    state_desc: str

    def describe(self) -> str:
        target = "the database"
        if self.object_name is not None:
            target = f"{self.schema_name}.{self.object_name}"
            if self.column_name is not None:
                target += f"({self.column_name})"
        elif self.schema_name is not None:
            target = f"schema {self.schema_name}"
        state = "" if self.state_desc == "GRANT" else f" ({self.state_desc})"
        return f"{self.permission_name}{state} on {target}"


@dataclass(frozen=True)
class GrantState:
    permissions: Sequence[Permission]
    principals: Sequence[tuple[str, str]] = field(default_factory=tuple)
    memberships: Sequence[tuple[str, str]] = field(default_factory=tuple)


def expected_permissions() -> set[Permission]:
    expected: set[Permission] = set()
    for table, row in GRANT_MATRIX.items():
        for role, cell in row.items():
            for verb in cell.verbs:
                expected.add(
                    Permission("OBJECT_OR_COLUMN", role, SCHEMA, table, None, verb, "GRANT")
                )
            for column in cell.update_columns:
                expected.add(
                    Permission("OBJECT_OR_COLUMN", role, SCHEMA, table, column, "UPDATE", "GRANT")
                )
    return expected


def compare_grants(environment: Environment, state: GrantState) -> list[str]:
    """Each difference from the matrix and the principal table, naming principal, object and
    right."""
    roles = set(DATABASE_ROLES)
    expected = expected_permissions()
    actual = {
        permission
        for permission in state.permissions
        if permission.grantee in roles or permission.schema_name == SCHEMA
    }
    differences = [
        f"{permission.grantee} missing {permission.describe()}"
        for permission in sorted(expected - actual, key=repr)
    ]
    differences += [
        f"{permission.grantee} extra {permission.describe()}"
        for permission in sorted(actual - expected, key=repr)
    ]
    names = user_names(environment)
    expected_users = dict(names.values())
    principal_types = dict(state.principals)
    for role in DATABASE_ROLES:
        if principal_types.get(role) != "R":
            differences.append(f"{role} missing role")
    for name, user_type in sorted(expected_users.items()):
        if principal_types.get(name) != user_type:
            differences.append(f"{name} missing contained user of type {user_type}")
    for name, principal_type in sorted(state.principals):
        if principal_type not in USER_TYPES or name in BUILT_IN_USERS:
            continue
        if name not in expected_users:
            differences.append(f"{name} extra contained user")
    wanted: set[tuple[str, str]] = {(role, names[role][0]) for role in DATABASE_ROLES}
    present = set(state.memberships)
    for group, member in sorted(wanted - present):
        differences.append(f"{member} missing member of {group}")
    for group, member in sorted(present - wanted):
        if group in roles:
            differences.append(f"{member} extra member of {group}")
        elif group.startswith("db_") and member != "dbo":
            differences.append(f"{member} member of built-in role {group}")
    return differences


def read_grant_state(connection: Connection) -> GrantState:
    permissions = [
        Permission(
            class_desc=row.class_desc,
            grantee=row.grantee,
            schema_name=row.schema_name,
            object_name=row.object_name,
            column_name=row.column_name,
            permission_name=row.permission_name,
            state_desc=row.state_desc,
        )
        for row in connection.execute(statements.DATABASE_PERMISSIONS)
    ]
    principals = [
        (row.name, row.type) for row in connection.execute(statements.DATABASE_PRINCIPALS)
    ]
    memberships = [
        (row.role_name, row.member_name) for row in connection.execute(statements.ROLE_MEMBERS)
    ]
    return GrantState(permissions, principals, memberships)


def login_delay(elapsed: float, previous: float) -> float:
    """Every 5 s for the first minute, then doubling to 30 s (section 10.9 step 2)."""
    if elapsed < LOGIN_STEADY_PERIOD_SECONDS:
        return LOGIN_STEADY_DELAY_SECONDS
    return min(LOGIN_MAX_DELAY_SECONDS, previous * 2)


def wait_for_login(
    probe: Callable[[], None],
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retry errors 40613 and 40615 until a login succeeds, for at most 5 minutes."""
    started = monotonic()
    delay = LOGIN_STEADY_DELAY_SECONDS
    while True:
        try:
            probe()
        except Exception as error:  # noqa: BLE001 - any login failure is classified by number
            driver = driver_error_of(error)
            number = describe_driver_error(driver).number if driver is not None else 0
            if number not in LOGIN_RETRY_ERRORS:
                raise SqlUnreachableError(type(error).__name__) from None
            elapsed = monotonic() - started
            delay = login_delay(elapsed, delay)
            if elapsed + delay > LOGIN_BUDGET_SECONDS:
                raise SqlUnreachableError(f"error {number} after {int(elapsed)} s") from None
            sleep(delay)
        else:
            return


def check_grants(environment: Environment) -> list[str]:
    engine = create_sync_engine("grants", access_token=access_token())

    def probe() -> None:
        with engine.connect() as connection:
            connection.execute(statements.LOGIN_PROBE)

    wait_for_login(probe)
    with engine.connect() as connection:
        state = read_grant_state(connection)
    engine.dispose()
    return compare_grants(environment, state)


# --- static-site ------------------------------------------------------------------------------

ARM: Final = "https://management.azure.com"
STATIC_SITES_API: Final = "2025-03-01"
STATIC_SITE_TYPE: Final = "Microsoft.Web/staticSites"


@dataclass(frozen=True)
class StaticSiteState:
    """What Resource Manager shows of the static site."""

    site: Mapping[str, Any]
    linked_backends: Sequence[Mapping[str, Any]] = ()
    functions: Sequence[Mapping[str, Any]] = ()
    app_settings: Mapping[str, Any] = field(default_factory=dict)
    function_app_settings: Mapping[str, Any] = field(default_factory=dict)


def static_site_problems(state: StaticSiteState) -> list[str]:
    """Each way the site does more than serve files, by name and never by value."""
    properties = state.site.get("properties") or {}
    problems = [
        f"{key} is set"
        for key in ("repositoryUrl", "branch", "buildProperties")
        if properties.get(key)
    ]
    if properties.get("linkedBackends") or state.linked_backends:
        backends = list(properties.get("linkedBackends") or []) + list(state.linked_backends)
        problems.append(f"{len(backends)} linked backend(s)")
    if properties.get("userProvidedFunctionApps"):
        problems.append(f"{len(properties['userProvidedFunctionApps'])} linked function app(s)")
    if properties.get("stagingEnvironmentPolicy") != "Disabled":
        problems.append("stagingEnvironmentPolicy isn't Disabled")
    if state.functions:
        names = sorted(str(item.get("name", "")) for item in state.functions)
        problems.append(f"managed functions: {', '.join(names)}")
    # A custom authentication provider takes its registration's client ID and secret from the
    # site's app settings, so a site without app settings has none.
    if state.app_settings:
        problems.append(f"app settings: {', '.join(sorted(state.app_settings))}")
    if state.function_app_settings:
        problems.append(f"function app settings: {', '.join(sorted(state.function_app_settings))}")
    return problems


def _arm(az: AzRunner, method: str, url: str) -> Any:
    return az(["rest", "--method", method, "--url", url])


def read_static_site(az: AzRunner, resource_group: str) -> StaticSiteState:
    found = az(
        [
            "resource",
            "list",
            "--resource-group",
            resource_group,
            "--tag",
            "azd-service-name=web",
            "--resource-type",
            STATIC_SITE_TYPE,
        ]
    )
    if not isinstance(found, list) or len(found) != 1:
        raise LookupError("one static site tagged azd-service-name=web")
    site_id = str(found[0]["id"])
    query = f"api-version={STATIC_SITES_API}"
    backends = _arm(az, "get", f"{ARM}{site_id}/linkedBackends?{query}") or {}
    functions = _arm(az, "get", f"{ARM}{site_id}/functions?{query}") or {}
    settings = _arm(az, "post", f"{ARM}{site_id}/listAppSettings?{query}") or {}
    function_settings = _arm(az, "post", f"{ARM}{site_id}/listFunctionAppSettings?{query}") or {}
    return StaticSiteState(
        site=_arm(az, "get", f"{ARM}{site_id}?{query}") or {},
        linked_backends=list(backends.get("value") or []),
        functions=list(functions.get("value") or []),
        app_settings=dict(settings.get("properties") or {}),
        function_app_settings=dict(function_settings.get("properties") or {}),
    )


def check_static_site(
    environment: Environment,
    *,
    az: AzRunner = run_az,
    root: Path = REPOSITORY,
    outputs: Mapping[str, str] | None = None,
) -> list[str]:
    values = read_azd_environment(environment, root) if outputs is None else dict(outputs)
    resource_group = values.get("AZURE_RESOURCE_GROUP") or f"rg-docchat-{environment}"
    return static_site_problems(read_static_site(az, resource_group))


# --- Command line -----------------------------------------------------------------------------

FAILURES: Final = {
    "app-settings": APP_SETTINGS_MISMATCH,
    "grants": GRANTS_MISMATCH,
    "static-site": STATIC_SITE_NOT_FILES_ONLY,
}


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-deploy checks.")
    commands = parser.add_subparsers(dest="command", required=True)
    settings = commands.add_parser("app-settings", help="compare the sites' app settings")
    settings.add_argument("--env", required=True, choices=ENVIRONMENTS)
    grants = commands.add_parser("grants", help="compare the database's grants with the matrix")
    grants.add_argument("--env", required=True, choices=("dev",))
    static = commands.add_parser("static-site", help="check that the static site serves files")
    static.add_argument("--env", required=True, choices=ENVIRONMENTS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    environment: Environment = arguments.env
    failure = FAILURES[arguments.command]
    try:
        if arguments.command == "app-settings":
            differences = check_app_settings(environment)
        elif arguments.command == "static-site":
            differences = check_static_site(environment)
        else:
            differences = check_grants(environment)
    except SqlUnreachableError as error:
        report_failure(*SQL_UNREACHABLE, str(error))
        return EXIT_FAILURE
    except Exception as error:  # noqa: BLE001 - the check's boundary: type only, never the text
        report_failure(*failure, f"the check couldn't run: {type(error).__name__}")
        return EXIT_FAILURE
    if differences:
        report_failure(*failure, "; ".join(differences))
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
