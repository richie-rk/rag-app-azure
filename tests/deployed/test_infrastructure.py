"""The deployed infrastructure suite (`84-testing.md` section 8.2), marker `infra`: job
`infra-checks` of `deployed-tests.yaml`, in `dev`, as the `dev` deploy identity.

T-029 here compares the three `dev` registrations with `83-infrastructure.md` sections 7.2 to 7.4
property by property, read from Microsoft Graph v1.0 with the deploy identity's
`Application.ReadWrite.OwnedBy` (it created, and so owns, them). `prod` has no test client,
which the `prod` deploy job checks.

The stage-2 rows: the ingestion app refuses requests from the internet (T-144), the API has no
publishing surface (T-447) and accepts TLS 1.3 only (T-474), and, with `--stage-acceptance`, both
apps' records reach `appi-docchat-dev` with the deployed commit (T-497). The probes run from the
job's runner, a host on the internet; settings are read through Azure Resource Manager with the
deploy identity's Contributor.

The stage-3 rows read what the rows' own commands read, through the job's Azure CLI sign-in:
the role assignments against the call matrices (T-100, `az role assignment list --all`), the
Azure OpenAI deployments and both content-filter policies (T-436), the refusal of key, shared-key
and SAS authentication (T-441), the network refusals of a runner without a rule and the SQL
runner rule's window (T-444), private-endpoint name resolution (T-446) and the diagnostic
settings of every listed resource (T-449). The `prod` deploy job runs T-436 alone against `prod`
(the row's second half), with `AZURE_ENV_NAME=prod` and `.azure/prod/.env`.

The stage-0 and stage-1 rows: the Cosmos DB and storage settings read through Azure Resource
Manager (T-091, T-099); the tightened role-assignment condition (T-438), the SQL password refusal
and token-login audit (T-442) and the Deny policies (T-443), each an attempt the design refuses;
the privileged SQL audit's predicate and application names (T-450) and, with
`--stage-acceptance`, the audit sentinel check (T-451). Three rows need what the `dev` deploy
identity doesn't hold, so their functions are person-run and skip in a workflow job: T-105 needs
AI Search's data plane, which admits only the operator allowlist (`83-infrastructure.md` section
4.5, as T-444 shows for the runner) and a data role the deploy identity lacks, so a member of
`sg-docchat-dev-config-operators` at level `config` runs it from an allowlisted machine, as the
live AI Search suite runs (`84-testing.md` section 1.6); T-450's operator half needs a config
operator's own `SELECT` and a read of `log-docchat-security`, where no pipeline identity has a
right (section 12.4); T-457 reads the GitHub environments and `id-docchat-citest` in
`rg-docchat-identity`, neither of which the job's token or identity can read.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import struct
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity.aio import AzureCliCredential
from azure.keyvault.secrets.aio import SecretClient
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import SearchIndex
from azure.storage.blob import AccountSasPermissions, ResourceTypes, generate_account_sas
from azure.storage.blob.aio import BlobServiceClient
from sqlalchemy import bindparam, func, select

from docchat_core.clients import SEARCH_API_VERSION
from docchat_core.clients.tls import TLS_CONTEXT
from docchat_core.cosmos.containers import CONTAINERS
from docchat_core.search.schema import (
    CHUNK_KEY_MAX_LENGTH,
    DEFAULT_ANALYZER,
    ChunkIndexFields,
    build_chunk_index,
    chunk_key,
)
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import create_sync_engine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import users
from docchat_testing.logquery import Rows, kql_datetime, query_logs
from docchat_testing.tokens import unverified_claims

pytestmark = pytest.mark.deployed

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
GRAPH: Final = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
GRAPH_TIMEOUT_SECONDS: Final = 30.0
MICROSOFT_GRAPH_APP_ID: Final = "00000003-0000-0000-c000-000000000000"
SCOPE_ID: Final = "772eca72-e66c-46c6-b40d-60f539a5f867"
ADMIN_ROLE_ID: Final = "47dc73a8-b97e-4be8-9481-614f6b17a11f"
USER_ROLE_ID: Final = "6f46cf45-f1e3-4812-8003-c51ae0f5f98d"
OPENID: Final = "37f7f235-527c-4136-accd-4a02d197296e"
PROFILE: Final = "14dad69e-099b-42c9-810b-d002981feec1"
OFFLINE_ACCESS: Final = "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"
NOTES: Final = (
    "Managed by the DocChat infrastructure templates. Declared properties are reset on every "
    "deployment."
)
INTEGRATED_APP: Final = "WindowsAzureActiveDirectoryIntegratedApp"
DEV_LOCAL_REDIRECT: Final = "http://localhost:5173/"


def graph_scopes(*ids: str) -> list[dict[str, Any]]:
    return [
        {
            "resourceAppId": MICROSOFT_GRAPH_APP_ID,
            "resourceAccess": [{"id": scope, "type": "Scope"} for scope in ids],
        }
    ]


def api_scope_definition() -> dict[str, Any]:
    return {
        "id": SCOPE_ID,
        "value": "access_as_user",
        "type": "User",
        "isEnabled": True,
        "adminConsentDisplayName": "Use DocChat as the signed-in user",
        "adminConsentDescription": (
            "Lets the DocChat web client call the DocChat API for the signed-in user, with only "
            "that user's access."
        ),
        "userConsentDisplayName": "Use DocChat as you",
        "userConsentDescription": (
            "Lets the DocChat web client call the DocChat API for you, with only your access."
        ),
    }


def app_roles() -> list[dict[str, Any]]:
    return [
        {
            "id": ADMIN_ROLE_ID,
            "value": "Admin",
            "displayName": "DocChat administrator",
            "description": (
                "Manages every project, its members, index mappings, indexes and documents, and "
                "sees ingestion runs."
            ),
            "allowedMemberTypes": ["User"],
            "isEnabled": True,
        },
        {
            "id": USER_ROLE_ID,
            "value": "User",
            "displayName": "DocChat user",
            "description": (
                "Chats in the projects they belong to and manages their own conversations, "
                "feedback and memory."
            ),
            "allowedMemberTypes": ["User"],
            "isEnabled": True,
        },
    ]


def expected_applications(values: Mapping[str, str], custom_domains: list[str]) -> dict[str, Any]:
    """Section 7.2's table for `dev`: role (`web`, `api`, `test`) to the declared properties."""
    environment = "dev"
    tenant = values["AZURE_TENANT_ID"].lower()
    token = values["DOCCHAT_RESOURCE_TOKEN"]
    prefix = f"docchat-{environment}-{token}"
    web_base = values["WEB_BASE_URL"].rstrip("/")
    no_grant = {"enableIdTokenIssuance": False, "enableAccessTokenIssuance": False}
    common = {"signInAudience": "AzureADMyOrg", "notes": NOTES}
    return {
        "web": common
        | {
            "appId": values["WEB_CLIENT_ID"],
            "uniqueName": f"{prefix}-web",
            "displayName": f"DocChat ({environment})",
            "identifierUris": [],
            "scopes": [],
            "preAuthorized": [],
            "appRoles": [],
            "spa": sorted(
                {f"{web_base}/", *(f"https://{host}/" for host in custom_domains)}
                | {DEV_LOCAL_REDIRECT}
            ),
            "homePageUrl": f"{web_base}/",
            "isFallbackPublicClient": False,
            "requiredResourceAccess": graph_scopes(OPENID, PROFILE, OFFLINE_ACCESS),
        },
        "api": common
        | {
            "appId": values["API_CLIENT_ID"],
            "uniqueName": f"{prefix}-api",
            "displayName": f"DocChat API ({environment})",
            "identifierUris": [f"api://{tenant}/{prefix}-api"],
            "scopes": [api_scope_definition()],
            "preAuthorized": sorted(
                [values["WEB_CLIENT_ID"], values["TEST_CLIENT_ID"]],
            ),
            "appRoles": app_roles(),
            "spa": [],
            "homePageUrl": None,
            "isFallbackPublicClient": False,
            "requiredResourceAccess": [],
            "optionalClaims": [{"name": "email", "essential": False}],
        },
        "test": common
        | {
            "appId": values["TEST_CLIENT_ID"],
            "uniqueName": f"{prefix}-test",
            "displayName": f"DocChat test client ({environment})",
            "identifierUris": [],
            "scopes": [],
            "preAuthorized": [],
            "appRoles": [],
            "spa": [],
            "homePageUrl": None,
            "isFallbackPublicClient": True,
            "requiredResourceAccess": graph_scopes(OPENID, PROFILE),
        },
        "implicitGrant": no_grant,
    }


def _subset(items: list[dict[str, Any]], keys: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        ({key: item.get(key) for key in keys} for item in items), key=lambda item: str(item["id"])
    )


def compare_application(
    role: str, app: Mapping[str, Any], expected: Mapping[str, Any], grant: Mapping[str, bool]
) -> list[str]:
    """Each property of `app` that differs from its declaration, named for the report."""
    api = app.get("api") or {}
    web = app.get("web") or {}
    scopes = api.get("oauth2PermissionScopes") or []
    pre_authorized = api.get("preAuthorizedApplications") or []
    observed: dict[str, Any] = {
        "appId": app.get("appId"),
        "uniqueName": app.get("uniqueName"),
        "displayName": app.get("displayName"),
        "signInAudience": app.get("signInAudience"),
        "notes": app.get("notes"),
        "identifierUris": app.get("identifierUris") or [],
        "scopes": _subset(scopes, api_scope_definition()) if scopes else [],
        "preAuthorized": sorted(entry.get("appId") for entry in pre_authorized),
        "appRoles": _subset(app.get("appRoles") or [], app_roles()[0]),
        "spa": sorted((app.get("spa") or {}).get("redirectUris") or []),
        "homePageUrl": web.get("homePageUrl"),
        "isFallbackPublicClient": bool(app.get("isFallbackPublicClient")),
        "requiredResourceAccess": app.get("requiredResourceAccess") or [],
    }
    if "optionalClaims" in expected:
        claims = (app.get("optionalClaims") or {}).get("accessToken") or []
        observed["optionalClaims"] = [
            {"name": claim.get("name"), "essential": claim.get("essential")} for claim in claims
        ]
    problems = [f"{role}.{name}" for name, value in expected.items() if observed.get(name) != value]
    if api.get("requestedAccessTokenVersion") != 2:
        problems.append(f"{role}.api.requestedAccessTokenVersion")
    if any(entry.get("delegatedPermissionIds") != [SCOPE_ID] for entry in pre_authorized):
        problems.append(f"{role}.api.preAuthorizedApplications.delegatedPermissionIds")
    if (web.get("redirectUris") or []) or (
        (app.get("publicClient") or {}).get("redirectUris") or []
    ):
        problems.append(f"{role}.redirectUris")
    if web.get("implicitGrantSettings") != grant:
        problems.append(f"{role}.web.implicitGrantSettings")
    if app.get("passwordCredentials") or app.get("keyCredentials"):
        problems.append(f"{role}.credentials")
    if "optionalClaims" not in expected and any((app.get("optionalClaims") or {}).values()):
        problems.append(f"{role}.optionalClaims")
    return problems


def compare_service_principal(role: str, principal: Mapping[str, Any]) -> list[str]:
    tags = {INTEGRATED_APP} if role == "web" else {INTEGRATED_APP, "HideApp"}
    checks = {
        "appRoleAssignmentRequired": principal.get("appRoleAssignmentRequired") is True,
        "accountEnabled": principal.get("accountEnabled") is True,
        "tags": set(principal.get("tags") or []) == tags,
        "notes": principal.get("notes") == NOTES,
    }
    return [f"{role}.servicePrincipal.{name}" for name, passed in checks.items() if not passed]


async def graph_get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(f"{GRAPH}{path}")
    assert response.status_code == 200, f"GET {path} answered {response.status_code}"
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.infra
async def test_t029_graph_registration_properties(azd_environment: dict[str, str]) -> None:
    environment_file = json.loads(
        (REPOSITORY_ROOT / "infra" / "env" / "dev.json").read_text(encoding="utf-8")
    )
    expected = expected_applications(azd_environment, list(environment_file["webCustomDomains"]))
    grant = expected.pop("implicitGrant")
    async with AzureCliCredential() as credential:
        bearer = (await credential.get_token(GRAPH_SCOPE)).token
    problems: list[str] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(GRAPH_TIMEOUT_SECONDS),
        follow_redirects=False,
        headers={"Authorization": f"Bearer {bearer}"},
    ) as client:
        for role, declared in expected.items():
            app = await graph_get(client, f"/applications(uniqueName='{declared['uniqueName']}')")
            problems += compare_application(role, app, declared, grant)
            principal = await graph_get(client, f"/servicePrincipals(appId='{app['appId']}')")
            problems += compare_service_principal(role, principal)
    assert problems == [], f"registration properties differ: {', '.join(problems)}"


# --- Stage 2: the ingestion app, the API's publishing surface and TLS floor, telemetry ---------

ARM: Final = "https://management.azure.com"
ARM_SCOPE: Final = "https://management.azure.com/.default"
SITES_API: Final = "2025-03-01"
COMPONENTS_API: Final = "2020-02-02"
ARM_TIMEOUT_SECONDS: Final = 30.0
PROBE_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
TLS_TIMEOUT_SECONDS: Final = 15.0
HTTPS_PORT: Final = 443
HTTP_OK: Final = 200
HTTP_FORBIDDEN: Final = 403
RESOURCE_GROUP: Final = "rg-docchat-dev"
COMMIT: Final = re.compile(r"^[0-9a-f]{40}$")
APP_ROLES: Final = ("docchat-api", "docchat-ingestion")
RELEASE_TAG: Final = "docchat-release-sha"

INGESTION_PROBES: Final = (
    ("GET", "/"),
    ("GET", "/admin/host/status"),
    ("POST", "/runtime/webhooks/durabletask/orchestrators/probe"),
    ("GET", "/api/probe"),
)
"""The Functions host's own routes and an HTTP-trigger path: the app has no HTTP trigger, and its
host must answer nothing at all to the internet."""

SCM_PROBES: Final = (
    ("GET", "/"),
    ("GET", "/api/settings"),
    ("POST", "/api/zipdeploy"),
    ("POST", "/api/publish?type=zip"),
    ("GET", "/basicauth"),
)
"""The deployment site's Kudu routes: deploy, settings and basic-authentication sign-in."""


async def refused_from_the_internet(host: str, probes: tuple[tuple[str, str], ...]) -> list[str]:
    """Each probe of `host` that got anything but the platform's `403` or no connection."""
    answered: list[str] = []
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT, follow_redirects=False, verify=TLS_CONTEXT
    ) as client:
        for method, path in probes:
            try:
                response = await client.request(method, f"https://{host}{path}")
            except httpx.TransportError:
                continue
            if response.status_code != HTTP_FORBIDDEN:
                answered.append(f"{method} {path}: {response.status_code}")
    return answered


async def arm_get(path: str, api_version: str) -> dict[str, Any]:
    async with AzureCliCredential() as credential:
        bearer = (await credential.get_token(ARM_SCOPE)).token
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(ARM_TIMEOUT_SECONDS),
        follow_redirects=False,
        headers={"Authorization": f"Bearer {bearer}"},
        verify=TLS_CONTEXT,
    ) as client:
        response = await client.get(f"{ARM}{path}", params={"api-version": api_version})
    assert response.status_code == HTTP_OK, f"GET {path} answered {response.status_code}"
    body: dict[str, Any] = response.json()
    return body


def site_path(values: Mapping[str, str], name: str) -> str:
    return (
        f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Web/sites/{name}"
    )


@pytest.mark.infra
async def test_t144_the_ingestion_app_refuses_every_request_from_the_internet(
    azd_environment: dict[str, str],
) -> None:
    name = azd_environment["DOCCHAT_INGESTION_APP_NAME"]
    answered = await refused_from_the_internet(f"{name}.azurewebsites.net", INGESTION_PROBES)
    assert answered == [], f"the ingestion app answered from the internet: {answered}"
    site = await arm_get(site_path(azd_environment, name), SITES_API)
    assert site["properties"]["publicNetworkAccess"] == "Disabled"


@pytest.mark.infra
async def test_t447_the_api_has_no_publishing_surface(azd_environment: dict[str, str]) -> None:
    name = azd_environment["DOCCHAT_API_APP_NAME"]
    answered = await refused_from_the_internet(f"{name}.scm.azurewebsites.net", SCM_PROBES)
    assert answered == [], f"the API's SCM site answered: {answered}"
    path = site_path(azd_environment, name)
    config = await arm_get(f"{path}/config/web", SITES_API)
    assert config["properties"]["ftpsState"] == "Disabled"
    assert config["properties"]["scmIpSecurityRestrictionsDefaultAction"] == "Deny"
    for policy in ("ftp", "scm"):
        body = await arm_get(f"{path}/basicPublishingCredentialsPolicies/{policy}", SITES_API)
        assert body["properties"]["allow"] is False, f"basic publishing over {policy} is allowed"


@pytest.fixture(scope="module")
def tls_contexts() -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """A TLS 1.2-at-most and a TLS 1.3-at-least client context, built outside any event loop
    because loading the certificate store takes tens of milliseconds."""
    old = ssl.create_default_context()
    old.maximum_version = ssl.TLSVersion.TLSv1_2
    new = ssl.create_default_context()
    new.minimum_version = ssl.TLSVersion.TLSv1_3
    return old, new


async def handshake(host: str, context: ssl.SSLContext) -> str | None:
    """The negotiated TLS version, or `None` when the handshake fails."""
    try:
        async with asyncio.timeout(TLS_TIMEOUT_SECONDS):
            _, writer = await asyncio.open_connection(
                host, HTTPS_PORT, ssl=context, server_hostname=host
            )
    except ssl.SSLError:
        return None
    version = writer.get_extra_info("ssl_object").version()
    writer.close()
    await writer.wait_closed()
    return str(version)


@pytest.mark.infra
async def test_t474_the_api_accepts_tls_1_3_only(
    azd_environment: dict[str, str], tls_contexts: tuple[ssl.SSLContext, ssl.SSLContext]
) -> None:
    host = urlsplit(azd_environment["API_BASE_URL"]).hostname or ""
    old, new = tls_contexts
    assert await handshake(host, old) is None, "a TLS 1.2 client completed its handshake"
    assert await handshake(host, new) == "TLSv1.3"


def telemetry_query(commit: str) -> str:
    """Records of both apps carrying the deployed commit as `AppVersion`, by role."""
    roles = ", ".join(f'"{role}"' for role in APP_ROLES)
    return f"""union AppRequests, AppDependencies, AppTraces, AppEvents, AppMetrics
| where TimeGenerated > ago(1d) and AppRoleName in ({roles}) and AppVersion == "{commit}"
| summarize records = count() by AppRoleName"""


@pytest.mark.infra
async def test_t497_both_apps_report_to_application_insights_with_the_deployed_commit(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    site = await arm_get(site_path(values, values["DOCCHAT_API_APP_NAME"]), SITES_API)
    commit = str((site.get("tags") or {}).get(RELEASE_TAG, ""))
    assert COMMIT.fullmatch(commit), f"the API site's {RELEASE_TAG} tag isn't a commit"
    component = await arm_get(
        f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Insights/components/{values['DOCCHAT_APPINSIGHTS_NAME']}",
        COMPONENTS_API,
    )
    assert component["properties"]["DisableLocalAuth"] is True
    rows = await query_logs(
        values["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"], telemetry_query(commit), "P1D"
    )
    reporting = {str(row["AppRoleName"]) for row in rows if row["records"]}
    assert reporting == set(APP_ROLES), f"records with {commit}: {sorted(reporting)}"


# --- Stage 3: role assignments, AI deployments, keys, network paths, DNS and diagnostics -------

ENVIRONMENT_FILE: Final = REPOSITORY_ROOT / "infra" / "env" / "dev.json"
FIREWALL_SCRIPT: Final = REPOSITORY_ROOT / "scripts" / "deploy" / "firewall.py"
AZ_TIMEOUT_SECONDS: Final = 300.0
SHARED_GROUP: Final = "rg-docchat-shared"
VNET: Final = "vnet-docchat-dev"
PRIVATE_ENDPOINT_SUBNET: Final = "snet-private-endpoints"
API_SUBNET: Final = "snet-api-integration"
HTTP_UNAUTHORIZED: Final = 401
REFUSED_STATUSES: Final = frozenset({HTTP_UNAUTHORIZED, HTTP_FORBIDDEN})


def load_firewall_script() -> ModuleType:
    # scripts/deploy holds command-line scripts, not a package. The suite reuses the script's
    # runner-address check and TDS login probe instead of keeping copies of them.
    name = "docchat_deployed_firewall_script"
    spec = importlib.util.spec_from_file_location(name, FIREWALL_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIREWALL: Final = load_firewall_script()


async def az_json(*args: str) -> Any:
    """One Azure CLI command as the job's identity, its JSON output parsed; a failure names the
    command and the CLI's last error line."""
    executable = shutil.which("az")
    assert executable is not None, "the Azure CLI isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, "--output", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        output, errors = await process.communicate()
    command = "az " + " ".join(word for word in args[:4] if not word.startswith("-"))
    last = (errors.decode(errors="replace").strip().splitlines() or ["no error output"])[-1]
    assert process.returncode == 0, f"{command} exited {process.returncode}: {last[:300]}"
    return json.loads(output) if output.strip() else None


async def bearer_token(scope: str) -> str:
    async with AzureCliCredential() as credential:
        token: str = (await credential.get_token(scope)).token
    return token


def dev_groups() -> dict[str, str]:
    """The `dev` Entra group object IDs of the environment file, lower-cased."""
    environment = json.loads(ENVIRONMENT_FILE.read_text(encoding="utf-8"))
    return {role: str(object_id).lower() for role, object_id in environment["groups"].items()}


def under(scope: str, parent: str) -> bool:
    child, ancestor = scope.casefold(), parent.casefold()
    return child == ancestor or child.startswith(ancestor + "/")


# --- T-100 --------------------------------------------------------------------------------------

BLOB_CONTRIBUTOR: Final = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
BLOB_READER: Final = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
BLOB_OWNER: Final = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
QUEUE_SENDER: Final = "c6a89b2d-59bc-44d0-9896-0f6e12d7b80a"
QUEUE_READER: Final = "19e7f393-937e-4f77-808e-94535e297925"
QUEUE_PROCESSOR: Final = "8a0f0c08-91a1-4084-bc3d-661d67233fed"
QUEUE_CONTRIBUTOR: Final = "974c5e8b-45b9-4653-ba55-5f855dd0fb88"
TABLE_CONTRIBUTOR: Final = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
SEARCH_READER: Final = "1407120a-92aa-4202-b7e9-c0e197c71c8f"
SEARCH_CONTRIBUTOR: Final = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
SEARCH_SERVICE_CONTRIBUTOR: Final = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
SEARCH_INDEX_MANAGER: Final = "e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d"
BLOB_TAG_READER: Final = "a5f5ab92-08e9-42d6-b9f2-31ca2b30a345"
OPENAI_USER: Final = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
COGNITIVE_USER: Final = "a97b65f3-24c7-4388-baec-2e87135dc908"
METRICS_PUBLISHER: Final = "3913510d-42f4-4e42-8a64-420c390055eb"
ACR_PULL: Final = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
SECRETS_USER: Final = "4633458b-17de-408a-b874-0445c86b69e6"
LOG_ANALYTICS_READER: Final = "73c42c96-874c-492b-b04d-ab87d138a893"
READER: Final = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
CONTRIBUTOR: Final = "b24988ac-6180-42a0-ab88-20f7382dd24c"
RBAC_ADMINISTRATOR: Final = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
TAG_WRITE: Final = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/write"
TAG_READ: Final = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read"
CONTENT_SHARE_SECRET: Final = "ingestion-content-share-connection"

type Assignment = tuple[str, str]
"""A role definition GUID and the scope it is assigned at, both lower-cased."""


@dataclass(frozen=True, slots=True)
class Scopes:
    """The resource IDs the call matrices of `83-infrastructure.md` sections 6.5 to 6.9 name."""

    group: str
    data: str
    host: str
    search: str
    openai: str
    content_safety: str
    document_intelligence: str
    insights: str
    workspace: str
    vault: str
    registry: str

    def container(self, name: str) -> str:
        return f"{self.data}/blobServices/default/containers/{name}"

    def queue(self, name: str) -> str:
        return f"{self.data}/queueServices/default/queues/{name}"


async def environment_scopes(values: Mapping[str, str]) -> Scopes:
    group = f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
    token = values["DOCCHAT_RESOURCE_TOKEN"]
    registry = await az_json("acr", "list", "--resource-group", SHARED_GROUP, "--query", "[0].id")
    assert registry, f"no registry in {SHARED_GROUP}"

    def provider(kind: str, name: str) -> str:
        return f"{group}/providers/{kind}/{name}"

    accounts = "Microsoft.CognitiveServices/accounts"
    return Scopes(
        group=group,
        data=provider("Microsoft.Storage/storageAccounts", values["DOCCHAT_STORAGE_ACCOUNT_NAME"]),
        host=provider("Microsoft.Storage/storageAccounts", f"stdocchathostdev{token}"),
        search=provider("Microsoft.Search/searchServices", values["DOCCHAT_SEARCH_SERVICE_NAME"]),
        openai=provider(accounts, values["DOCCHAT_OPENAI_ACCOUNT_NAME"]),
        content_safety=provider(accounts, f"cs-docchat-dev-{token}"),
        document_intelligence=provider(accounts, f"di-docchat-dev-{token}"),
        insights=provider("Microsoft.Insights/components", values["DOCCHAT_APPINSIGHTS_NAME"]),
        workspace=values["DOCCHAT_LOG_ANALYTICS_RESOURCE_ID"],
        vault=provider("Microsoft.KeyVault/vaults", values["DOCCHAT_KEY_VAULT_NAME"]),
        registry=str(registry),
    )


def assignment_set(*pairs: tuple[str, str]) -> set[Assignment]:
    return {(role.lower(), scope.casefold()) for role, scope in pairs}


def workload_matrix(scopes: Scopes) -> dict[str, set[Assignment]]:
    """Sections 6.5 and 6.6 in `dev`, where the Azure OpenAI account exists."""
    s = scopes
    return {
        "api": assignment_set(
            (BLOB_CONTRIBUTOR, s.container("documents")),
            (BLOB_READER, s.container("renditions")),
            (QUEUE_SENDER, s.queue("ingestion-requests")),
            (SEARCH_READER, s.search),
            (OPENAI_USER, s.openai),
            (COGNITIVE_USER, s.content_safety),
            (METRICS_PUBLISHER, s.insights),
            (ACR_PULL, s.registry),
        ),
        "ingestion": assignment_set(
            (BLOB_OWNER, s.host),
            (QUEUE_CONTRIBUTOR, s.host),
            (TABLE_CONTRIBUTOR, s.host),
            (QUEUE_READER, s.queue("ingestion-requests")),
            (QUEUE_PROCESSOR, s.queue("ingestion-requests")),
            (QUEUE_SENDER, s.queue("ingestion-requests-poison")),
            (QUEUE_READER, s.queue("ingestion-requests-poison")),
            (BLOB_CONTRIBUTOR, s.container("documents")),
            (BLOB_TAG_READER, s.container("documents")),
            (BLOB_CONTRIBUTOR, s.container("renditions")),
            (BLOB_CONTRIBUTOR, s.container("ingestion-work")),
            (SEARCH_INDEX_MANAGER, s.search),
            (SEARCH_CONTRIBUTOR, s.search),
            (OPENAI_USER, s.openai),
            (COGNITIVE_USER, s.document_intelligence),
            (METRICS_PUBLISHER, s.insights),
            (ACR_PULL, s.registry),
        ),
    }


def group_matrix(scopes: Scopes) -> dict[str, set[Assignment]]:
    """Section 6.9's Azure RBAC rows of each environment-file group at or below
    `rg-docchat-dev`; the groups not named here hold no Azure role there."""
    s = scopes
    return {
        "operators": assignment_set((LOG_ANALYTICS_READER, s.workspace), (READER, s.group)),
        "configOperators": assignment_set(
            (QUEUE_PROCESSOR, s.queue("ingestion-requests-poison")),
            (COGNITIVE_USER, s.group),
            (SEARCH_SERVICE_CONTRIBUTOR, s.group),
            (SEARCH_CONTRIBUTOR, s.group),
        ),
        "userDataOperators": assignment_set((BLOB_READER, s.container("documents"))),
    }


def by_principal(listing: list[dict[str, Any]]) -> dict[str, set[Assignment]]:
    found: dict[str, set[Assignment]] = {}
    for item in listing:
        role = str(item["roleDefinitionId"]).rsplit("/", 1)[-1].lower()
        found.setdefault(str(item["principalId"]).lower(), set()).add(
            (role, str(item["scope"]).casefold())
        )
    return found


def differences(who: str, found: set[Assignment], expected: set[Assignment]) -> list[str]:
    extra = [f"{who} holds {role} at {scope}" for role, scope in sorted(found - expected)]
    missing = [f"{who} lacks {role} at {scope}" for role, scope in sorted(expected - found)]
    return extra + missing


async def role_permissions(role: str) -> list[dict[str, Any]]:
    definitions = await az_json("role", "definition", "list", "--name", role)
    assert definitions, f"no role definition {role}"
    permissions: list[dict[str, Any]] = definitions[0]["permissions"]
    return permissions


def grants(permissions: list[dict[str, Any]], action: str) -> bool:
    """Whether a role's data actions cover `action`, its not-data-actions taken out."""
    wanted = action.casefold()

    def covered(patterns: list[str] | None) -> bool:
        return any(fnmatch.fnmatchcase(wanted, str(item).casefold()) for item in patterns or [])

    return any(
        covered(entry.get("dataActions")) and not covered(entry.get("notDataActions"))
        for entry in permissions
    )


def ci_test_identity_differences(
    listing: list[dict[str, Any]],
    found: dict[str, set[Assignment]],
    scopes: Scopes,
    *,
    known: set[str],
) -> list[str]:
    """`id-docchat-citest`'s one right in `rg-docchat-dev`: Log Analytics Reader on the workspace
    (section 6.8). The job can't read `rg-docchat-identity`, so the identity is recognised as the
    one service principal, other than the environment's own identities, holding that role there."""
    readers = {
        str(item["principalId"]).lower()
        for item in listing
        if str(item.get("principalType", "")).casefold() == "serviceprincipal"
        and str(item["roleDefinitionId"]).lower().endswith(LOG_ANALYTICS_READER)
        and str(item["scope"]).casefold() == scopes.workspace.casefold()
    } - known
    if len(readers) != 1:
        found_count = len(readers)
        return [f"expected one CI test identity reading the workspace, found {found_count}"]
    (citest,) = readers
    held = {item for item in found.get(citest, set()) if under(item[1], scopes.group)}
    expected = assignment_set((LOG_ANALYTICS_READER, scopes.workspace))
    return differences("the CI test identity", held, expected)


@pytest.mark.infra
async def test_t100_the_deployed_role_assignments_equal_the_call_matrices(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    scopes = await environment_scopes(values)
    listing = await az_json("role", "assignment", "list", "--all")
    found = by_principal(listing)
    api = values["DOCCHAT_API_IDENTITY_PRINCIPAL_ID"].lower()
    ingestion = values["DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID"].lower()
    expected = workload_matrix(scopes)
    problems = differences("the API identity", found.get(api, set()), expected["api"])
    # Content-share fallback 1 adds one secret role (section 6.6); otherwise the vault grants none.
    fallback = assignment_set((SECRETS_USER, f"{scopes.vault}/secrets/{CONTENT_SHARE_SECRET}"))
    held = found.get(ingestion, set()) - fallback
    problems += differences("the ingestion identity", held, expected["ingestion"])

    groups = dev_groups()
    matrix = group_matrix(scopes)
    for role, object_id in groups.items():
        in_group = {item for item in found.get(object_id, set()) if under(item[1], scopes.group)}
        problems += differences(f"group {role}", in_group, matrix.get(role, set()))

    deployer = str(unverified_claims(await bearer_token(ARM_SCOPE))["oid"]).lower()
    in_group = {item for item in found.get(deployer, set()) if under(item[1], scopes.group)}
    problems += differences(
        "the dev deploy identity",
        in_group,
        assignment_set((CONTRIBUTOR, scopes.group), (RBAC_ADMINISTRATOR, scopes.group)),
    )
    administration = [
        item
        for item in listing
        if str(item["principalId"]).lower() == deployer
        and str(item["roleDefinitionId"]).lower().endswith(RBAC_ADMINISTRATOR)
        and str(item["scope"]).casefold() == scopes.group.casefold()
    ]
    if not all(item.get("condition") for item in administration):
        problems.append("the dev deploy identity's role-assignment right carries no condition")
    problems += ci_test_identity_differences(
        listing, found, scopes, known={api, ingestion, deployer, *groups.values()}
    )

    for who, principal in (("API", api), ("ingestion", ingestion)):
        for role, scope in sorted(found.get(principal, set())):
            on_data = under(scopes.data, scope) or under(scope, scopes.data)
            if on_data and grants(await role_permissions(role), TAG_WRITE):
                problems.append(f"the {who} identity can write blob tags through {role}")
    tag_reader = (BLOB_TAG_READER, scopes.container("documents").casefold())
    if tag_reader not in found.get(ingestion, set()) or not grants(
        await role_permissions(BLOB_TAG_READER), TAG_READ
    ):
        problems.append("the ingestion identity can't read blob tags on documents")
    operators = found.get(groups["userDataOperators"], set())
    on_data_account = {item for item in operators if under(item[1], scopes.data)}
    if on_data_account != assignment_set((BLOB_READER, scopes.container("documents"))):
        problems.append("user-data operators hold more than Storage Blob Data Reader on documents")
    assert problems == [], "\n".join(problems)


# --- T-436 --------------------------------------------------------------------------------------

AI_DEPLOYMENTS_API: Final = "2026-07-01"
HARM_CATEGORIES: Final = ("Hate", "Sexual", "Violence", "Selfharm")
TEXT_GUARD: Final = "docchat-text-guard"
TABLE_SUMMARY_GUARD: Final = "docchat-table-summary-guard"


def expected_filters(policy: str) -> dict[tuple[str, str], dict[str, object]]:
    """`82-security.md` SEC-37 and `83-infrastructure.md` section 5.6 per filter and source."""
    blocking: dict[str, object] = {"enabled": True, "blocking": True}
    off: dict[str, object] = {"enabled": False, "blocking": False}
    filters: dict[tuple[str, str], dict[str, object]] = {
        (category, source): {**blocking, "severityThreshold": "Medium"}
        for category in HARM_CATEGORIES
        for source in ("Prompt", "Completion")
    }
    filters |= {
        ("Jailbreak", "Prompt"): blocking,
        ("Protected Material Text", "Completion"): blocking,
        ("Protected Material Code", "Completion"): blocking,
        ("Profanity", "Prompt"): off,
        ("Profanity", "Completion"): off,
        ("Indirect Attack", "Prompt"): blocking if policy == TABLE_SUMMARY_GUARD else off,
    }
    return filters


def deployed_filters(
    policy: Mapping[str, Any], expected: Mapping[tuple[str, str], Mapping[str, object]]
) -> dict[tuple[str, str], dict[str, object]]:
    """The policy's filters, each with the settings its expectation names."""
    found: dict[tuple[str, str], dict[str, object]] = {}
    for entry in policy.get("contentFilters") or []:
        key = (str(entry["name"]), str(entry["source"]))
        wanted = expected.get(key, {"enabled": None})
        found[key] = {name: entry.get(name) for name in wanted}
    return found


def expected_deployments(open_ai: Mapping[str, Any]) -> dict[str, tuple[object, ...]]:
    """Each deployment of the environment file: type, model, version, capacity, upgrade option
    and content-filter policy (`83-infrastructure.md` sections 5.2 and 5.3)."""
    expected: dict[str, tuple[object, ...]] = {}
    for item in open_ai["deployments"].values():
        expected[str(item["name"])] = (
            item["type"], item["model"], item["version"], item["capacity"],
            item["versionUpgradeOption"], item["raiPolicyName"],
        )  # fmt: skip
    for model in open_ai["embeddingModels"]:
        for kind, type_field, capacity in (
            ("query", "queryType", "queryCapacity"),
            ("ingest", "ingestionType", "ingestionCapacity"),
        ):
            expected[f"embed-{kind}-{model['key']}"] = (
                model[type_field], model["model"], model["version"], model[capacity],
                "NoAutoUpgrade", None,
            )  # fmt: skip
    return expected


@pytest.mark.infra
async def test_t436_openai_deployments_and_their_content_filters_follow_the_design(
    azd_environment: dict[str, str], azd_environment_name: str
) -> None:
    values = azd_environment
    environment = azd_environment_name
    group = f"rg-docchat-{environment}"
    environment_file = REPOSITORY_ROOT / "infra" / "env" / f"{environment}.json"
    open_ai = json.loads(environment_file.read_text(encoding="utf-8"))["openAi"]
    listing = await az_json(
        "cognitiveservices", "account", "deployment", "list",
        "--name", values["DOCCHAT_OPENAI_ACCOUNT_NAME"], "--resource-group", group,
    )  # fmt: skip
    observed = {
        str(item["name"]): (
            item["sku"]["name"],
            item["properties"]["model"]["name"],
            item["properties"]["model"]["version"],
            item["sku"]["capacity"],
            item["properties"].get("versionUpgradeOption"),
            item["properties"].get("raiPolicyName") or None,
        )
        for item in listing
    }
    assert observed == expected_deployments(open_ai)

    account_id = (
        f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{values['DOCCHAT_OPENAI_ACCOUNT_NAME']}"
    )
    for policy in (TEXT_GUARD, TABLE_SUMMARY_GUARD):
        body = await az_json(
            "rest", "--method", "get",
            "--url", f"{ARM}{account_id}/raiPolicies/{policy}?api-version={AI_DEPLOYMENTS_API}",
        )  # fmt: skip
        properties = body["properties"]
        assert properties["basePolicyName"] == "Microsoft.DefaultV2", policy
        assert properties["mode"] == "Default", policy
        assert not properties.get("customBlocklists"), policy
        expected = expected_filters(policy)
        assert deployed_filters(properties, expected) == expected, policy


# --- T-441 --------------------------------------------------------------------------------------

SEARCH_API: Final = "2026-04-01"
CONTENT_SAFETY_API: Final = "2024-09-01"
DI_API: Final = "2024-11-30"
"""The Document Intelligence API version (`84-testing.md` section 1.5)."""
STORAGE_KEY_BYTES: Final = 64
COSMOS_KEY_BYTES: Final = 64
SEARCH_KEY_BYTES: Final = 26
COGNITIVE_KEY_BYTES: Final = 16


def made_up_key(size: int) -> str:
    return base64.b64encode(os.urandom(size)).decode("ascii")


def local_auth_off(settings: Mapping[str, Any], *paths: tuple[str, ...]) -> bool:
    """Whether any of the settings at `paths` is `True`."""
    for path in paths:
        value: Any = settings
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if value is True:
            return True
    return False


async def storage_shared_key_status(account: str) -> int | None:
    credential = {"account_name": account, "account_key": made_up_key(STORAGE_KEY_BYTES)}
    async with BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=credential, retry_total=0
    ) as client:
        try:
            await client.get_service_properties()
        except HttpResponseError as error:
            return error.status_code
        except ServiceRequestError:
            return None
    return HTTP_OK


def account_sas(account: str) -> str:
    return generate_account_sas(
        account,
        made_up_key(STORAGE_KEY_BYTES),
        ResourceTypes(service=True),
        AccountSasPermissions(list=True),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


async def cosmos_key_status(endpoint: str) -> int | None:
    try:
        async with CosmosClient(endpoint, credential=made_up_key(COSMOS_KEY_BYTES)) as client:
            async for _ in client.list_databases():
                break
    except CosmosHttpResponseError as error:
        return error.status_code
    except ServiceRequestError:
        return None
    return HTTP_OK


async def http_status(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> int | None:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TransportError:
        return None
    return response.status_code


@pytest.mark.infra
async def test_t441_key_shared_key_and_sas_authentication_is_refused(
    azd_environment: dict[str, str],
) -> None:
    # No list-keys call: each one raises docchat-alert-key-access (A45). The keys are made up,
    # so each refusal is read beside the setting that turns key authentication off.
    values = azd_environment
    data = values["DOCCHAT_STORAGE_ACCOUNT_NAME"]
    search = values["DOCCHAT_SEARCH_SERVICE_NAME"]
    openai = values["DOCCHAT_OPENAI_ACCOUNT_NAME"]
    content_safety = urlsplit(values["DOCCHAT_CONTENTSAFETY_ENDPOINT"]).hostname or ""
    group = ("--resource-group", RESOURCE_GROUP)
    cosmos = values["DOCCHAT_COSMOS_ACCOUNT_NAME"]
    settings = {
        "storage": await az_json("storage", "account", "show", "--name", data, *group),
        "cosmos": await az_json("cosmosdb", "show", "--name", cosmos, *group),
        "search": await az_json("search", "service", "show", "--name", search, *group),
        "openai": await az_json("cognitiveservices", "account", "show", "--name", openai, *group),
        "content safety": await az_json(
            "cognitiveservices", "account", "show", "--name", content_safety.split(".")[0], *group
        ),
    }
    problems: list[str] = []
    if settings["storage"].get("allowSharedKeyAccess") is not False:
        problems.append("the data account allows shared-key authorisation")
    for name, paths in (
        ("cosmos", (("disableLocalAuth",),)),
        ("search", (("disableLocalAuth",), ("properties", "disableLocalAuth"))),
        ("openai", (("properties", "disableLocalAuth"),)),
        ("content safety", (("properties", "disableLocalAuth"),)),
    ):
        if not local_auth_off(settings[name], *paths):
            problems.append(f"{name} keeps local authentication on")

    statuses = {
        "storage shared key": await storage_shared_key_status(data),
        "Cosmos DB key": await cosmos_key_status(values["DOCCHAT_COSMOS_ENDPOINT"]),
    }
    openai_models = f"{values['DOCCHAT_OPENAI_ENDPOINT'].rstrip('/')}/openai/v1/models"
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT, follow_redirects=False, verify=TLS_CONTEXT
    ) as client:
        statuses["storage SAS"] = await http_status(
            client, "GET", f"https://{data}.blob.core.windows.net/?comp=list&{account_sas(data)}"
        )
        statuses["Search admin key"] = await http_status(
            client, "GET", f"https://{search}.search.windows.net/indexes",
            params={"api-version": SEARCH_API},
            headers={"api-key": secrets.token_hex(SEARCH_KEY_BYTES)},
        )  # fmt: skip
        statuses["Azure OpenAI key"] = await http_status(
            client, "GET", openai_models,
            headers={"api-key": secrets.token_hex(COGNITIVE_KEY_BYTES)},
        )  # fmt: skip
        statuses["Content Safety key"] = await http_status(
            client, "POST", f"https://{content_safety}/contentsafety/text:shieldPrompt",
            params={"api-version": CONTENT_SAFETY_API},
            headers={"Ocp-Apim-Subscription-Key": secrets.token_hex(COGNITIVE_KEY_BYTES)},
            json={"userPrompt": "", "documents": []},
        )  # fmt: skip
    problems += [
        f"a {name} got {status}"
        for name, status in statuses.items()
        if status not in REFUSED_STATUSES
    ]
    assert problems == [], "\n".join(problems)


# --- T-444 --------------------------------------------------------------------------------------

SQL_SCOPE: Final = "https://database.windows.net/.default"
VAULT_REFUSALS: Final = ("ForbiddenByFirewall", "ForbiddenByConnection")
SQL_FIREWALL_REFUSAL: Final = 40615
SQL_PROBE_SECONDS: Final = 15.0
SQL_CLOSE_LIMIT_SECONDS: Final = 300.0
"""A SQL firewall change can take five minutes to take effect (section 4.6 item 2)."""


def admits(settings: Mapping[str, Any], address: str) -> bool:
    """Whether a resource's public network settings let `address` in: public access on, and a
    default that isn't deny, or no default and no rule at all, or a rule naming the address."""
    nested = settings.get("properties")
    properties: Mapping[str, Any] = nested if isinstance(nested, Mapping) else {}
    access = settings.get("publicNetworkAccess") or properties.get("publicNetworkAccess")
    if str(access or "").casefold() == "disabled":
        return False
    rule_sets: list[Mapping[str, Any]] = [
        settings,
        settings.get("networkRuleSet") or {},
        properties.get("networkAcls") or {},
    ]
    listed = {
        str(rule.get("ipAddressOrRange") or rule.get("value") or "").removesuffix("/32")
        for item in rule_sets
        for rule in item.get("ipRules") or []
    }
    defaults = [str(item["defaultAction"]) for item in rule_sets if item.get("defaultAction")]
    if defaults:
        return any(action.casefold() != "deny" for action in defaults) or address in listed
    return not listed or address in listed


def vault_refused(error: HttpResponseError) -> bool:
    """A Key Vault answer that refuses the caller's network path, not its rights."""
    if error.status_code != HTTP_FORBIDDEN:
        return False
    texts = [str(error.message or "")]
    if error.error is not None:
        texts += [str(error.error.code or ""), str(error.error.innererror or "")]
    return any(code in text for text in texts for code in VAULT_REFUSALS)


async def vault_answer(vault_uri: str) -> str | None:
    """`None` when the environment vault refuses this runner's address, else what it answered."""
    async with (
        AzureCliCredential() as credential,
        SecretClient(vault_uri, credential, retry_total=0) as client,
    ):
        try:
            async for _ in client.list_properties_of_secrets():
                break
        except HttpResponseError as error:
            return None if vault_refused(error) else str(error.status_code)
        except ServiceRequestError:
            return None
    return str(HTTP_OK)


def anonymous_probes(values: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """One request per data and AI endpoint, with no credential (name, method, URL)."""
    token = values["DOCCHAT_RESOURCE_TOKEN"]
    data = values["DOCCHAT_STORAGE_ACCOUNT_NAME"]
    host = f"stdocchathostdev{token}"
    services = ((data, ("blob", "queue")), (host, ("blob", "queue", "table")))
    search = values["DOCCHAT_SEARCH_ENDPOINT"].rstrip("/")
    openai = values["DOCCHAT_OPENAI_ENDPOINT"].rstrip("/")
    content_safety = f"https://cs-docchat-dev-{token}.cognitiveservices.azure.com"
    intelligence = f"https://di-docchat-dev-{token}.cognitiveservices.azure.com"
    probes = [("Cosmos DB", "GET", values["DOCCHAT_COSMOS_ENDPOINT"])]
    probes += [
        (f"{account} {kind}", "GET", f"https://{account}.{kind}.core.windows.net/?comp=list")
        for account, kinds in services
        for kind in kinds
    ]
    probes += [
        ("AI Search", "GET", f"{search}/indexes?api-version={SEARCH_API}"),
        ("Azure OpenAI", "GET", f"{openai}/openai/v1/models"),
        (
            "Content Safety",
            "POST",
            f"{content_safety}/contentsafety/text:shieldPrompt?api-version={CONTENT_SAFETY_API}",
        ),
        (
            "Document Intelligence",
            "GET",
            f"{intelligence}/documentintelligence/documentModels?api-version={DI_API}",
        ),
    ]
    return probes


@pytest.mark.infra
async def test_t444_data_and_ai_resources_refuse_a_runner_without_a_rule(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    address = await asyncio.to_thread(FIREWALL.resolve_runner_address)
    token = values["DOCCHAT_RESOURCE_TOKEN"]
    group = ("--resource-group", RESOURCE_GROUP)
    show = ("show", "--name")
    resources: dict[str, tuple[str, ...]] = {
        "Cosmos DB": ("cosmosdb", *show, values["DOCCHAT_COSMOS_ACCOUNT_NAME"]),
        "data storage": ("storage", "account", *show, values["DOCCHAT_STORAGE_ACCOUNT_NAME"]),
        "host storage": ("storage", "account", *show, f"stdocchathostdev{token}"),
        "AI Search": ("search", "service", *show, values["DOCCHAT_SEARCH_SERVICE_NAME"]),
        "Azure OpenAI": (
            "cognitiveservices",
            "account",
            *show,
            values["DOCCHAT_OPENAI_ACCOUNT_NAME"],
        ),
        "Content Safety": ("cognitiveservices", "account", *show, f"cs-docchat-dev-{token}"),
        "Document Intelligence": ("cognitiveservices", "account", *show, f"di-docchat-dev-{token}"),
        "the environment Key Vault": ("keyvault", *show, values["DOCCHAT_KEY_VAULT_NAME"]),
    }
    problems = [
        f"{name} admits the runner's address"
        for name, command in resources.items()
        if admits(await az_json(*command, *group), address)
    ]
    # A service that let the runner through would answer 401 for the missing credential; its
    # network rules answer 403, or drop the connection, before any credential is looked at.
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT, follow_redirects=False, verify=TLS_CONTEXT
    ) as client:
        for name, method, url in anonymous_probes(values):
            status = await http_status(client, method, url)
            if status not in {None, HTTP_FORBIDDEN}:
                problems.append(f"{name} answered {status}")
    # Key Vault asks every caller for a credential first, so it gets the deploy identity's token
    # and must refuse the address, not the identity's rights.
    vault = await vault_answer(values["DOCCHAT_KEY_VAULT_URI"])
    if vault is not None:
        problems.append(f"the environment Key Vault answered {vault}")
    assert problems == [], "\n".join(problems)


def sql_login(server: str, database: str, token: str) -> int | None:
    """The TDS login of `firewall.py`'s probe: `None` when accepted, else the server's error."""
    result: int | None = FIREWALL.tds_login(server, database, token)
    return result


async def run_firewall(action: str) -> int:
    """`firewall.py <action> --target sql --env dev`, as section 8.2's SQL tests run it."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(FIREWALL_SCRIPT), action, "--target", "sql", "--env", "dev",
        cwd=REPOSITORY_ROOT,
    )  # fmt: skip
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS + 60):
        return await process.wait()


@pytest.mark.infra
async def test_t444_sql_admits_the_runner_only_while_firewall_py_holds_its_rule(
    azd_environment: dict[str, str],
) -> None:
    server = azd_environment["DOCCHAT_SQL_SERVER"]
    database = azd_environment["DOCCHAT_SQL_DATABASE"]
    token = await bearer_token(SQL_SCOPE)
    before = await asyncio.to_thread(sql_login, server, database, token)
    assert before == SQL_FIREWALL_REFUSAL, f"before open the login got {before}"
    assert await run_firewall("open") == 0, "firewall.py open failed"
    try:
        inside = await asyncio.to_thread(sql_login, server, database, token)
    finally:
        closed = await run_firewall("close")
    assert inside is None, f"inside the rule's window the login got {inside}"
    assert closed == 0, "firewall.py close failed"
    deadline = asyncio.get_running_loop().time() + SQL_CLOSE_LIMIT_SECONDS
    after = inside
    while after != SQL_FIREWALL_REFUSAL and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(SQL_PROBE_SECONDS)
        after = await asyncio.to_thread(sql_login, server, database, token)
    assert after == SQL_FIREWALL_REFUSAL, f"after close the login got {after}"


# --- T-446 --------------------------------------------------------------------------------------


def zone_group_records(zone_groups: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each host name and address of an endpoint's DNS zone groups."""
    return [
        (str(record["fqdn"]), str(ip))
        for item in zone_groups
        for config in item.get("privateDnsZoneConfigs") or []
        for record in config.get("recordSets") or []
        for ip in record.get("ipAddresses") or []
    ]


@pytest.mark.infra
async def test_t446_private_endpoints_resolve_inside_the_private_endpoint_subnet(
    azd_environment: dict[str, str],
) -> None:
    group = ("--resource-group", RESOURCE_GROUP)
    subnet = await az_json(
        "network", "vnet", "subnet", "show", "--vnet-name", VNET,
        "--name", PRIVATE_ENDPOINT_SUBNET, *group,
    )  # fmt: skip
    prefixes = [subnet.get("addressPrefix"), *(subnet.get("addressPrefixes") or [])]
    networks = [ipaddress.ip_network(prefix) for prefix in prefixes if prefix]
    problems: list[str] = []
    endpoints = await az_json("network", "private-endpoint", "list", *group)
    assert endpoints, f"{RESOURCE_GROUP} holds no private endpoint"
    for endpoint in endpoints:
        name = str(endpoint["name"])
        if str(endpoint["subnet"]["id"]).casefold() != str(subnet["id"]).casefold():
            problems.append(f"{name} isn't in {PRIVATE_ENDPOINT_SUBNET}")
        zone_groups = await az_json(
            "network", "private-endpoint", "dns-zone-group", "list", "--endpoint-name", name,
            *group,
        )  # fmt: skip
        if [str(item["name"]) for item in zone_groups] != ["default"]:
            problems.append(f"{name} hasn't exactly the zone group default")
        records = zone_group_records(zone_groups)
        if not records:
            problems.append(f"{name}'s zone group holds no A record")
        problems += [
            f"{fqdn} maps to {ip}, outside {PRIVATE_ENDPOINT_SUBNET}"
            for fqdn, ip in records
            if not any(ipaddress.ip_address(ip) in network for network in networks)
        ]
    vnet = await az_json("network", "vnet", "show", "--name", VNET, *group)
    for zone in await az_json("network", "private-dns", "zone", "list", *group):
        links = await az_json(
            "network", "private-dns", "link", "vnet", "list", "--zone-name", zone["name"], *group
        )
        targets = {str(link["virtualNetwork"]["id"]).casefold() for link in links}
        if targets != {str(vnet["id"]).casefold()}:
            problems.append(f"{zone['name']} isn't linked to {VNET} alone")
        if any(link.get("registrationEnabled") for link in links):
            problems.append(f"{zone['name']} registers records automatically")
    # 83-infrastructure.md section 2.4, which owns the site's settings, keeps route-all off (ADR
    # 0109): private-endpoint traffic is RFC 1918 and enters the network without it.
    site = azd_environment["DOCCHAT_API_APP_NAME"]
    config = await az_json("webapp", "config", "show", "--name", site, *group)
    if config.get("vnetRouteAllEnabled") is not False:
        problems.append("the API site routes all its traffic through the network")
    details = await az_json("webapp", "show", "--name", site, *group)
    if not str(details.get("virtualNetworkSubnetId") or "").endswith(f"/subnets/{API_SUBNET}"):
        problems.append(f"the API site isn't integrated into {API_SUBNET}")
    assert problems == [], "\n".join(problems)


# --- T-449 --------------------------------------------------------------------------------------

DIAGNOSTIC_SETTING: Final = "to-log-analytics"
SQL_AUDIT_SETTINGS: Final = ("sql-audit", "sql-audit-security")
SCAN_SETTING: Final = "scan-results"
DEFENDER_SETTINGS: Final = "providers/Microsoft.Security/defenderForStorageSettings/current"
type Categories = tuple[frozenset[str], frozenset[str]]


def categories(logs: set[str], metrics: set[str]) -> Categories:
    return frozenset(logs), frozenset(metrics)


API_SITE: Final = categories(
    {"AppServicePlatformLogs", "AppServiceAuditLogs", "AppServiceIPSecAuditLogs"}, {"AllMetrics"}
)
INGESTION_SITE: Final = categories({"FunctionAppLogs"}, {"AllMetrics"})
STUB_SITE: Final = categories({"AppServicePlatformLogs"}, {"AllMetrics"})
PLAN: Final = categories(set(), {"AllMetrics"})
SQL_MASTER: Final = categories({"SQLSecurityAuditEvents", "DevOpsOperationsAudit"}, set())
SQL_DATABASE: Final = categories(
    {"SQLSecurityAuditEvents", "Errors", "Timeouts", "Blocks", "Deadlocks"},
    {"Basic", "InstanceAndAppAdvanced"},
)
COSMOS: Final = categories(
    {"DataPlaneRequests", "ControlPlaneRequests", "PartitionKeyRUConsumption"}, {"Requests"}
)
DATA_SERVICE: Final = categories({"StorageRead", "StorageWrite", "StorageDelete"}, {"Transaction"})
HOST_SERVICE: Final = categories({"StorageWrite", "StorageDelete"}, {"Transaction"})
STORAGE_ACCOUNT: Final = categories(set(), {"Transaction"})
SEARCH: Final = categories({"OperationLogs"}, {"AllMetrics"})
AI_ACCOUNT: Final = categories({"Audit", "RequestResponse"}, {"AllMetrics"})
VAULT: Final = categories({"AuditEvent"}, {"AllMetrics"})
AUTOSCALE: Final = categories({"AutoscaleScaleActionsLog"}, {"AllMetrics"})
SCAN_RESULTS: Final = categories({"ScanResults"}, set())
BY_TYPE: Final[dict[str, Categories]] = {
    "microsoft.web/serverfarms": PLAN,
    "microsoft.documentdb/databaseaccounts": COSMOS,
    "microsoft.search/searchservices": SEARCH,
    "microsoft.cognitiveservices/accounts": AI_ACCOUNT,
    "microsoft.keyvault/vaults": VAULT,
    "microsoft.insights/autoscalesettings": AUTOSCALE,
}


@dataclass(frozen=True, slots=True)
class Coverage:
    """One resource (or storage service) of section 2.19 and what its setting must collect;
    the first name is the setting it must have, the others the ones it may have beside it."""

    resource_id: str
    expected: Categories
    names: tuple[str, ...] = (DIAGNOSTIC_SETTING,)


def storage_coverage(resource_id: str, *, data: bool, scanning: bool) -> list[Coverage]:
    services = ("blob", "queue") if data else ("blob", "queue", "table")
    service = DATA_SERVICE if data else HOST_SERVICE
    targets = [Coverage(resource_id, STORAGE_ACCOUNT)]
    targets += [Coverage(f"{resource_id}/{kind}Services/default", service) for kind in services]
    if data and scanning:
        targets.append(
            Coverage(f"{resource_id}/{DEFENDER_SETTINGS}", SCAN_RESULTS, (SCAN_SETTING,))
        )
    return targets


def coverage(resources: list[dict[str, Any]], values: Mapping[str, str]) -> list[Coverage]:
    """The resources of `rg-docchat-dev` whose type section 2.19 lists, with the storage
    services' sub-resource IDs and both SQL databases."""
    sites = {
        values["DOCCHAT_API_APP_NAME"]: API_SITE,
        values["DOCCHAT_INGESTION_APP_NAME"]: INGESTION_SITE,
    }
    if values.get("DOCCHAT_MODEL_STUB_APP_NAME"):
        sites[values["DOCCHAT_MODEL_STUB_APP_NAME"]] = STUB_SITE
    scanning = bool(
        json.loads(ENVIRONMENT_FILE.read_text(encoding="utf-8"))["storage"]["malwareScanning"]
    )
    targets: list[Coverage] = []
    for resource in resources:
        kind = str(resource["type"]).casefold()
        name, resource_id = str(resource["name"]), str(resource["id"])
        if kind == "microsoft.web/sites" and name in sites:
            targets.append(Coverage(resource_id, sites[name]))
        elif kind in BY_TYPE:
            targets.append(Coverage(resource_id, BY_TYPE[kind]))
        elif kind == "microsoft.sql/servers":
            database = values["DOCCHAT_SQL_DATABASE"]
            targets += [
                Coverage(f"{resource_id}/databases/master", SQL_MASTER, SQL_AUDIT_SETTINGS),
                Coverage(f"{resource_id}/databases/{database}", SQL_DATABASE),
            ]
        elif kind == "microsoft.storage/storageaccounts":
            data = name == values["DOCCHAT_STORAGE_ACCOUNT_NAME"]
            targets += storage_coverage(resource_id, data=data, scanning=scanning)
    return targets


def enabled_categories(entries: list[dict[str, Any]] | None) -> frozenset[str]:
    return frozenset(
        str(entry.get("category") or entry.get("categoryGroup"))
        for entry in entries or []
        if entry.get("enabled")
    )


def setting_problems(target: Coverage, settings: list[dict[str, Any]], workspace: str) -> list[str]:
    where = target.resource_id.split("/providers/", 1)[-1]
    names = sorted(str(item["name"]) for item in settings)
    required = target.names[0]
    if required not in names or not set(names) <= set(target.names):
        return [f"{where} has the settings {names}, not {required}"]
    problems: list[str] = []
    for setting in settings:
        collected = (
            enabled_categories(setting.get("logs")),
            enabled_categories(setting.get("metrics")),
        )
        if collected != target.expected:
            problems.append(f"{where} {setting['name']} collects other categories")
        if setting.get("logAnalyticsDestinationType") != "Dedicated":
            problems.append(f"{where} {setting['name']} doesn't write resource-specific tables")
        destination = str(setting.get("workspaceId")).casefold()
        if setting["name"] == required and destination != workspace:
            problems.append(f"{where} {setting['name']} sends to another workspace")
    return problems


@pytest.mark.infra
async def test_t449_every_listed_resource_has_its_setting_with_exactly_its_categories(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    resources = await az_json("resource", "list", "--resource-group", RESOURCE_GROUP)
    targets = coverage(resources, values)
    assert targets, f"no resource of {RESOURCE_GROUP} has a type of section 2.19"
    workspace = values["DOCCHAT_LOG_ANALYTICS_RESOURCE_ID"].casefold()
    problems: list[str] = []
    for target in targets:
        listing = await az_json(
            "monitor", "diagnostic-settings", "list", "--resource", target.resource_id
        )
        settings = listing.get("value", []) if isinstance(listing, dict) else listing or []
        problems += setting_problems(target, settings, workspace)
    assert problems == [], "\n".join(problems)


# --- Stage 0 and 1: Cosmos DB and storage settings (T-091, T-099) ------------------------------

STORAGE_API: Final = "2025-06-01"
DEFENDER_API: Final = "2025-06-01"
ETAG_PATH: Final = '/"_etag"/?'
"""Cosmos DB adds this excluded path to every indexing policy itself."""


def dev_config() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ENVIRONMENT_FILE.read_text(encoding="utf-8"))
    return loaded


def indexing_shape(policy: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of an indexing policy the design states, in a comparable form."""
    excluded = {str(item["path"]) for item in policy.get("excludedPaths") or []} - {ETAG_PATH}
    return {
        "indexingMode": str(policy.get("indexingMode", "")).lower(),
        "automatic": bool(policy.get("automatic", True)),
        "includedPaths": sorted(str(item["path"]) for item in policy.get("includedPaths") or []),
        "excludedPaths": sorted(excluded),
        "compositeIndexes": [
            [(str(item["path"]), str(item.get("order", "ascending")).lower()) for item in group]
            for group in policy.get("compositeIndexes") or []
        ],
    }


def cosmos_problems(
    account: Mapping[str, Any],
    containers: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[str]:
    """Each difference from `80-data-model.md` section 3 and `83-infrastructure.md` section 2.10."""
    problems: list[str] = []
    consistency = (account.get("consistencyPolicy") or {}).get("defaultConsistencyLevel")
    if consistency != "Session":
        problems.append(f"consistency is {consistency}")
    backup = account.get("backupPolicy") or {}
    tier = (backup.get("continuousModeProperties") or {}).get("tier")
    if backup.get("type") != "Continuous" or tier != config["cosmos"]["backupTier"]:
        problems.append(f"backup is {backup.get('type')} {tier}")
    for definition in CONTAINERS:
        resource = containers.get(definition.name)
        if resource is None:
            problems.append(f"container {definition.name} is missing")
            continue
        key = resource.get("partitionKey") or {}
        expected_key = definition.partition_key_definition()
        if [key.get("paths"), key.get("kind"), key.get("version")] != [
            expected_key["paths"], expected_key["kind"], expected_key["version"],
        ]:  # fmt: skip
            problems.append(f"{definition.name}: partition key {key}")
        days = (
            int(config.get(definition.retention_parameter, 0))
            if (definition.retention_parameter)
            else 0
        )
        if resource.get("defaultTtl") != definition.default_ttl(days):
            problems.append(f"{definition.name}: defaultTtl {resource.get('defaultTtl')}")
        if indexing_shape(resource.get("indexingPolicy") or {}) != indexing_shape(
            definition.indexing_policy()
        ):
            problems.append(f"{definition.name}: indexing policy differs")
    return problems


@pytest.mark.infra
async def test_t091_cosmos_db_account_and_containers_follow_the_design(
    azd_environment: dict[str, str],
) -> None:
    account_name = azd_environment["DOCCHAT_COSMOS_ACCOUNT_NAME"]
    account = await az_json(
        "cosmosdb", "show", "--name", account_name, "--resource-group", RESOURCE_GROUP
    )
    containers: dict[str, Mapping[str, Any]] = {}
    for definition in CONTAINERS:
        shown = await az_json(
            "cosmosdb", "sql", "container", "show", "--account-name", account_name,
            "--resource-group", RESOURCE_GROUP, "--database-name", "docchat",
            "--name", definition.name,
        )  # fmt: skip
        containers[definition.name] = shown["resource"]

    assert cosmos_problems(account, containers, dev_config()) == []


LIFECYCLE_RULE: Final = {
    "name": "ingestion-work-expiry",
    "enabled": True,
    "type": "Lifecycle",
    "definition": {
        "filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["ingestion-work/"]},
        "actions": {"baseBlob": {"delete": {"daysAfterModificationGreaterThan": 7}}},
    },
}
DATA_CONTAINERS: Final = frozenset({"documents", "renditions", "ingestion-work"})
DATA_QUEUES: Final = frozenset({"ingestion-requests", "ingestion-requests-poison"})


def without_nulls(value: Any) -> Any:
    """Resource Manager answers absent options as `null`; the design names only set ones."""
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StorageState:
    """What Resource Manager shows of the data account."""

    account: Mapping[str, Any]
    blob_service: Mapping[str, Any]
    policy: Mapping[str, Any]
    containers: Sequence[Mapping[str, Any]]
    queues: Sequence[Mapping[str, Any]]
    defender: Mapping[str, Any]


def storage_problems(state: StorageState, config: Mapping[str, Any]) -> list[str]:
    """Each difference from `83-infrastructure.md` section 2.11 and `80-data-model.md` 4.5."""
    account, blob_service, policy = state.account, state.blob_service, state.policy
    containers, queues, defender = state.containers, state.queues, state.defender
    problems: list[str] = []
    if account.get("accessTier") != "Hot":
        problems.append(f"accessTier is {account.get('accessTier')}")
    blob = blob_service
    if without_nulls(blob.get("deleteRetentionPolicy")) != {
        "enabled": True, "days": 14, "allowPermanentDelete": False,
    }:  # fmt: skip
        problems.append(f"blob soft delete is {blob.get('deleteRetentionPolicy')}")
    retention = without_nulls(blob.get("containerDeleteRetentionPolicy")) or {}
    if (retention.get("enabled"), retention.get("days")) != (True, 30):
        problems.append(f"container soft delete is {retention}")
    if blob.get("isVersioningEnabled"):
        problems.append("versioning is on")
    rules = without_nulls(policy.get("policy", policy).get("rules") or [])
    if rules != [LIFECYCLE_RULE]:
        problems.append(f"lifecycle rules are {[rule.get('name') for rule in rules]}")
    found = {str(item["name"]): item for item in containers}
    if set(found) != DATA_CONTAINERS:
        problems.append(f"containers are {sorted(found)}")
    for name, item in found.items():
        access = (item.get("properties") or {}).get("publicAccess")
        if access not in {None, "None"}:
            problems.append(f"{name} has public access {access}")
    if {str(item["name"]) for item in queues} != DATA_QUEUES:
        problems.append(f"queues are {sorted(str(item['name']) for item in queues)}")
    scanning = bool(config["storage"]["malwareScanning"])
    properties = defender.get("properties") or {}
    upload = (properties.get("malwareScanning") or {}).get("onUpload") or {}
    filters = without_nulls(upload.get("filters")) or {}
    expected_defender = {
        "isEnabled": scanning,
        "onUpload.isEnabled": scanning,
        "capGBPerMonth": config["storage"]["malwareScanCapGbPerMonth"],
        "excludeBlobsWithPrefix": ["renditions/", "ingestion-work/"],
        "excludeBlobsWithSuffix": ["/converted.pdf"],
        "blobScanResultsOptions": "blobIndexTags",
        "sensitiveDataDiscovery": False,
        "overrideSubscriptionLevelSettings": True,
    }
    actual_defender = {
        "isEnabled": properties.get("isEnabled"),
        "onUpload.isEnabled": upload.get("isEnabled"),
        "capGBPerMonth": upload.get("capGBPerMonth"),
        "excludeBlobsWithPrefix": filters.get("excludeBlobsWithPrefix"),
        "excludeBlobsWithSuffix": filters.get("excludeBlobsWithSuffix"),
        "blobScanResultsOptions": (properties.get("malwareScanning") or {}).get(
            "blobScanResultsOptions"
        ),
        "sensitiveDataDiscovery": (properties.get("sensitiveDataDiscovery") or {}).get("isEnabled"),
        "overrideSubscriptionLevelSettings": properties.get("overrideSubscriptionLevelSettings"),
    }
    problems += [
        f"Defender {key} is {actual_defender[key]}"
        for key, value in expected_defender.items()
        if actual_defender[key] != value
    ]
    return problems


@pytest.mark.infra
async def test_t099_the_data_account_keeps_its_retention_lifecycle_and_scanning_settings(
    azd_environment: dict[str, str],
) -> None:
    name = azd_environment["DOCCHAT_STORAGE_ACCOUNT_NAME"]
    account = await az_json(
        "storage", "account", "show", "--name", name, "--resource-group", RESOURCE_GROUP
    )
    blob_service = await az_json(
        "storage", "account", "blob-service-properties", "show",
        "--account-name", name, "--resource-group", RESOURCE_GROUP,
    )  # fmt: skip
    policy = await az_json(
        "storage", "account", "management-policy", "show",
        "--account-name", name, "--resource-group", RESOURCE_GROUP,
    )  # fmt: skip
    account_id = str(account["id"])
    containers = await arm_list(f"{account_id}/blobServices/default/containers", STORAGE_API)
    queues = await arm_list(f"{account_id}/queueServices/default/queues", STORAGE_API)
    defender = await arm_get(
        f"{account_id}/providers/Microsoft.Security/defenderForStorageSettings/current",
        DEFENDER_API,
    )

    state = StorageState(account, blob_service, policy or {}, containers, queues, defender)
    problems = storage_problems(state, dev_config())

    assert problems == []


async def arm_list(path: str, api_version: str) -> list[dict[str, Any]]:
    """Every item of a Resource Manager collection, all pages."""
    items: list[dict[str, Any]] = []
    url: str | None = f"{ARM}{path}?api-version={api_version}"
    while url:
        page = await az_json("rest", "--method", "get", "--url", url)
        items += list(page.get("value") or [])
        url = page.get("nextLink")
        assert url is None or url.startswith(f"{ARM}/"), "a page links outside Resource Manager"
    return items


# --- Stage 1: the search schema (T-105, person-run) ---------------------------------------------

SEARCH_SCOPE: Final = "https://search.azure.com/.default"
SEARCH_DIMENSIONS: Final = 3072


def field_shape(index: SearchIndex) -> list[tuple[Any, ...]]:
    """Each field's name, type and the attributes `build_chunk_index` sets, in order."""
    return [
        (
            field.name, str(field.type), bool(field.key), bool(field.searchable),
            bool(field.filterable), bool(field.sortable), bool(field.facetable),
            field.analyzer_name, field.vector_search_dimensions,
            field.vector_search_profile_name,
        )
        for field in index.fields or []
    ]  # fmt: skip


def search_settings_shape(index: SearchIndex) -> tuple[Any, ...]:
    vector = index.vector_search
    semantic = index.semantic_search
    return (
        sorted(profile.name for profile in (vector.profiles or [])) if vector else [],
        sorted(algorithm.name for algorithm in (vector.algorithms or [])) if vector else [],
        sorted(item.name for item in (semantic.configurations or [])) if semantic else [],
        semantic.default_configuration_name if semantic else None,
    )


@pytest.mark.infra
async def test_t105_a_built_chunk_index_is_accepted_read_back_and_takes_a_79_character_key(
    azd_environment: dict[str, str],
) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "person-run: AI Search admits only the operator allowlist and the deploy identity "
            "holds no index data role; a config operator runs it from an allowlisted machine"
        )
    endpoint = azd_environment["DOCCHAT_SEARCH_ENDPOINT"]
    name = f"docchat-{secrets.token_hex(16)}-g1"
    built = build_chunk_index(name, SEARCH_DIMENSIONS, DEFAULT_ANALYZER)
    fields = ChunkIndexFields.for_version(1)
    key = chunk_key(uuid.uuid4(), uuid.uuid4(), 12345)
    assert len(key) == CHUNK_KEY_MAX_LENGTH
    async with (
        AzureCliCredential() as credential,
        SearchIndexClient(endpoint, credential, api_version=SEARCH_API_VERSION) as indexes,
    ):
        await indexes.create_index(built)
        try:
            read_back = await indexes.get_index(name)
            async with SearchClient(
                endpoint, name, credential, api_version=SEARCH_API_VERSION
            ) as documents:
                results = await documents.upload_documents(
                    [
                        {
                            fields.key: key,
                            fields.document_id: str(uuid.uuid4()),
                            fields.document_version_id: str(uuid.uuid4()),
                            fields.chunk_set_id: str(uuid.uuid4()),
                            fields.chunk_ordinal: 12345,
                            fields.title: "Deployed schema test",
                            fields.heading_path: ["Deployed schema test"],
                            fields.chunk_text: "One chunk with the longest key the design allows.",
                            fields.page_number: 1,
                            fields.content_type: "application/pdf",
                            fields.vector: [0.001] * SEARCH_DIMENSIONS,
                        }
                    ]
                )
        finally:
            await indexes.delete_index(name)

    assert field_shape(read_back) == field_shape(built)
    assert search_settings_shape(read_back) == search_settings_shape(built)
    assert [(result.key, result.succeeded) for result in results] == [(key, True)]


# --- Stage 0: refusals the design expects (T-438, T-443) ---------------------------------------

STORAGE_BLOB_DATA_READER: Final = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
OWNER: Final = "8e3af657-a8ff-443c-a75f-2fe8c4bcb635"


async def az_attempt(*args: str) -> tuple[int, str, str]:
    """One Azure CLI command the design expects to be refused: its exit code and output."""
    executable = shutil.which("az")
    assert executable is not None, "the Azure CLI isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, "--output", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        output, errors = await process.communicate()
    returncode = process.returncode if process.returncode is not None else -1
    return returncode, output.decode(errors="replace"), errors.decode(errors="replace")


async def own_principal_id() -> str:
    """The signed-in identity's object ID, from its Resource Manager token."""
    return str(unverified_claims(await bearer_token(ARM_SCOPE))["oid"])


async def refused_assignment(
    label: str, principal: str, principal_type: str, role: str, scope: str
) -> str | None:
    """None when the condition refuses the assignment with `AuthorizationFailed`; otherwise what
    happened, after deleting an assignment that was wrongly created."""
    code, output, errors = await az_attempt(
        "role", "assignment", "create", "--assignee-object-id", principal,
        "--assignee-principal-type", principal_type, "--role", role, "--scope", scope,
    )  # fmt: skip
    if code == 0:
        created = json.loads(output or "{}").get("id")
        if created:
            await az_json("role", "assignment", "delete", "--ids", str(created))
        return f"{label}: the assignment was created (and deleted again)"
    if "AuthorizationFailed" not in errors:
        return f"{label}: refused with another error: {errors.strip().splitlines()[-1:]}"
    return None


@pytest.mark.infra
async def test_t438_the_tightened_condition_refuses_grants_outside_its_table(
    azd_environment: dict[str, str],
) -> None:
    scopes = await environment_scopes(azd_environment)
    deploy_identity = await own_principal_id()
    readers = dev_groups()["conversationReaders"]
    api_principal = azd_environment["DOCCHAT_API_IDENTITY_PRINCIPAL_ID"]
    attempts = [
        ("Storage Blob Data Reader to the deploy identity on documents", deploy_identity,
         "ServicePrincipal", STORAGE_BLOB_DATA_READER, scopes.container("documents")),
        ("Storage Blob Data Reader to sg-docchat-dev-conversation-readers", readers, "Group",
         STORAGE_BLOB_DATA_READER, scopes.data),
        ("Owner to the API identity", api_principal, "ServicePrincipal", OWNER, scopes.group),
    ]  # fmt: skip

    outcomes = [await refused_assignment(*attempt) for attempt in attempts]

    assert [outcome for outcome in outcomes if outcome] == []


POLICY_REFUSAL: Final = "RequestDisallowedByPolicy"
COSMOS_API: Final = "2026-03-15"


async def refused_by_policy(
    label: str, attempt: tuple[str, ...], undo: tuple[str, ...]
) -> str | None:
    """None when Azure Policy refuses the change; otherwise what happened, after undoing a change
    that was wrongly applied (section 8.2: a test that changes a setting restores it)."""
    code, _, errors = await az_attempt(*attempt)
    if code == 0:
        await az_attempt(*undo)
        return f"{label}: the change was applied (and undone)"
    if POLICY_REFUSAL not in errors:
        return f"{label}: refused with another error: {errors.strip().splitlines()[-1:]}"
    return None


@pytest.mark.infra
async def test_t443_the_deny_policies_refuse_local_auth_public_blobs_and_federated_credentials(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    group = f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
    accounts = f"{group}/providers/Microsoft.DocumentDB/databaseAccounts"
    cosmos = f"{accounts}/{values['DOCCHAT_COSMOS_ACCOUNT_NAME']}"
    storage = values["DOCCHAT_STORAGE_ACCOUNT_NAME"]
    credential = f"docchat-policy-probe-{secrets.token_hex(4)}"
    attempts = [
        ("disableLocalAuth false on the Cosmos DB account",
         ("resource", "update", "--ids", cosmos, "--api-version", COSMOS_API,
          "--set", "properties.disableLocalAuth=false"),
         ("resource", "update", "--ids", cosmos, "--api-version", COSMOS_API,
          "--set", "properties.disableLocalAuth=true")),
        ("allowSharedKeyAccess true on the data account",
         ("storage", "account", "update", "--name", storage, "--resource-group", RESOURCE_GROUP,
          "--allow-shared-key-access", "true"),
         ("storage", "account", "update", "--name", storage, "--resource-group", RESOURCE_GROUP,
          "--allow-shared-key-access", "false")),
        ("allowBlobPublicAccess true on the data account",
         ("storage", "account", "update", "--name", storage, "--resource-group", RESOURCE_GROUP,
          "--allow-blob-public-access", "true"),
         ("storage", "account", "update", "--name", storage, "--resource-group", RESOURCE_GROUP,
          "--allow-blob-public-access", "false")),
        ("a federated credential on id-docchat-api-dev",
         ("identity", "federated-credential", "create", "--name", credential,
          "--identity-name", "id-docchat-api-dev", "--resource-group", RESOURCE_GROUP,
          "--issuer", "https://token.actions.githubusercontent.com",
          "--subject", "repo:docchat/policy-probe:environment:none",
          "--audiences", "api://AzureADTokenExchange"),
         ("identity", "federated-credential", "delete", "--name", credential,
          "--identity-name", "id-docchat-api-dev", "--resource-group", RESOURCE_GROUP, "--yes")),
    ]  # fmt: skip

    outcomes = [await refused_by_policy(*attempt) for attempt in attempts]

    assert [outcome for outcome in outcomes if outcome] == []


# --- Stage 0: SQL logins and the SQL audits (T-442, T-450, T-451) -------------------------------

AUDIT_LIMIT: Final = timedelta(minutes=20)
AUDIT_POLL_SECONDS: Final = 60.0
API_USER: Final = "id-docchat-api-dev"
INGESTION_USER: Final = "id-docchat-ingest-dev"
AUTHENTICATION_SUCCEEDED: Final = "DATABASE AUTHENTICATION SUCCEEDED"
PRIVILEGED: Final = '_ResourceId endswith "/databases/master"'
WORKLOAD: Final = '_ResourceId endswith "/databases/docchat"'
SQL_LOGIN_PROBE_USER: Final = "docchat-password-probe"
TDS_OPTION_FLAGS3_NO_EXTENSION: Final = 0x00


def password_login7(server: str, database: str, user: str, password: str) -> bytes:
    """A LOGIN7 message with SQL authentication (a user name and an obfuscated password) and no
    feature extension, in the layout `firewall.py`'s token login uses."""
    fw = FIREWALL
    password_bytes = bytes(
        (((byte << 4) & 0xF0) | (byte >> 4)) ^ 0xA5 for byte in password.encode("utf-16-le")
    )
    user_bytes, server_bytes = user.encode("utf-16-le"), server.encode("utf-16-le")
    database_bytes = database.encode("utf-16-le")
    user_at = fw.LOGIN7_FIXED_LENGTH
    password_at = user_at + len(user_bytes)
    server_at = password_at + len(password_bytes)
    database_at = server_at + len(server_bytes)
    total = database_at + len(database_bytes)
    empty = user_at
    head = struct.pack(
        "<IIIIII", total, fw.TDS_8_VERSION, fw.TDS_PACKET_SIZE, 0, os.getpid() & 0xFFFF, 0
    )
    flags = bytes(
        [fw.LOGIN7_OPTION_FLAGS1, fw.LOGIN7_OPTION_FLAGS2, 0, TDS_OPTION_FLAGS3_NO_EXTENSION]
    )
    locale = struct.pack("<iI", 0, fw.LOGIN7_LCID)
    first_fields = struct.pack(
        "<12H", empty, 0, user_at, len(user_bytes) // 2, password_at, len(password_bytes) // 2,
        empty, 0, server_at, len(server_bytes) // 2, empty, 0,
    )  # fmt: skip
    second_fields = struct.pack("<6H", empty, 0, empty, 0, database_at, len(database_bytes) // 2)
    last_fields = struct.pack("<6HI", empty, 0, empty, 0, empty, 0, 0)
    fixed = head + flags + locale + first_fields + second_fields + bytes(6) + last_fields
    return fixed + user_bytes + password_bytes + server_bytes + database_bytes


def password_login(server: str, database: str) -> int | None:
    """One SQL-authentication login: `None` when accepted, else the server's error number."""
    fw = FIREWALL
    stream = fw.open_tds_stream(server, fw.TDS_PORT)
    try:
        stream.sendall(fw.tds_packets(fw.TDS_PRELOGIN, fw.build_prelogin()))
        fw.parse_prelogin(fw.read_tds_message(stream))
        login = password_login7(server, database, SQL_LOGIN_PROBE_USER, secrets.token_urlsafe(24))
        stream.sendall(fw.tds_packets(fw.TDS_LOGIN7, login))
        reply = fw.parse_login_reply(fw.read_tds_message(stream))
    finally:
        stream.close()
    if reply.error_number is not None:
        result: int | None = reply.error_number
        return result
    return None if reply.accepted else -1


async def poll_logs(workspace: str, query: str, *, found: Any) -> Rows:
    """The query's rows once `found(rows)` holds, polled every 60 s for at most 20 minutes."""
    rows: Rows = []
    deadline = asyncio.get_running_loop().time() + AUDIT_LIMIT.total_seconds()
    while True:
        rows = await query_logs(workspace, query, "P1D")
        if found(rows) or asyncio.get_running_loop().time() >= deadline:
            return rows
        await asyncio.sleep(AUDIT_POLL_SECONDS)


@pytest.mark.infra
@pytest.mark.timeout(int(AUDIT_LIMIT.total_seconds()) + 900)
async def test_t442_password_logins_fail_and_token_logins_are_audited(
    azd_environment: dict[str, str],
) -> None:
    values = azd_environment
    server, database = values["DOCCHAT_SQL_SERVER"], values["DOCCHAT_SQL_DATABASE"]
    token = await bearer_token(SQL_SCOPE)
    runner = await asyncio.to_thread(FIREWALL.resolve_runner_address)
    started = datetime.now(UTC)
    assert await run_firewall("open") == 0, "firewall.py open failed"
    try:
        password = await asyncio.to_thread(password_login, server, database)
        token_login = await asyncio.to_thread(sql_login, server, database, token)
    finally:
        closed = await run_firewall("close")
    assert password is not None, "a SQL login with a user name and password was accepted"
    assert token_login is None, f"the deploy identity's token login got {token_login}"
    assert closed == 0, "firewall.py close failed"
    query = f"""SQLSecurityAuditEvents
| where TimeGenerated > {kql_datetime(started - timedelta(minutes=1))}
| where ActionName == "{AUTHENTICATION_SUCCEEDED}" and ClientIp == "{runner}"
| summarize logins = count()"""
    rows = await poll_logs(
        values["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"], query,
        found=lambda rows: bool(rows and rows[0]["logins"]),
    )  # fmt: skip
    assert rows, "the SQLSecurityAuditEvents query answered no row"
    assert rows[0]["logins"], "the token login isn't in SQLSecurityAuditEvents after 20 minutes"


def audit_query(where: str, since: datetime) -> str:
    return f"""SQLSecurityAuditEvents
| where TimeGenerated > {kql_datetime(since)}
| where {where}
| summarize rows = count() by audit = iff({PRIVILEGED}, "privileged", "workload")"""


def counts(rows: Rows) -> dict[str, int]:
    return {str(row["audit"]): int(row["rows"]) for row in rows}


@pytest.mark.infra
async def test_t450_the_privileged_audit_skips_the_api_and_names_grants_and_migrations(
    azd_environment: dict[str, str],
) -> None:
    workspace = azd_environment["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"]
    since = datetime.now(UTC) - timedelta(days=7)
    api = counts(await query_logs(
        workspace, audit_query(f'DatabasePrincipalName == "{API_USER}"', since), "P7D"
    ))  # fmt: skip
    applications = await query_logs(
        workspace,
        f"""SQLSecurityAuditEvents
| where TimeGenerated > {kql_datetime(since)} and {PRIVILEGED}
| where ApplicationName in ("docchat-grants", "docchat-migrations")
| summarize rows = count() by ApplicationName""",
        "P7D",
    )

    assert api.get("privileged", 0) == 0, "the privileged audit recorded the API identity"
    # The postprovision hook runs the migrations and the grants step on every provision, and
    # grants-check connects as docchat-grants on every dev deploy.
    assert {str(row["ApplicationName"]) for row in applications} >= {"docchat-grants"}


SECURITY_WORKSPACE_LOOKUP: Final = (
    "monitor", "log-analytics", "workspace", "show", "--resource-group", "rg-docchat-identity",
    "--workspace-name", "log-docchat-security", "--query", "customerId",
)  # fmt: skip
CONFIG_OPERATORS_USER: Final = "sg-docchat-dev-config-operators"


@pytest.mark.infra
@pytest.mark.timeout(int(AUDIT_LIMIT.total_seconds()) + 600)
async def test_t450_a_config_operators_select_reaches_the_security_workspace() -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "person-run: a config operator's own SELECT and a read of log-docchat-security, "
            "where no pipeline identity has a right (83-infrastructure.md section 12.4)"
        )
    # The performer, at level config, ran `docchat-ops --env dev config show` just before.
    workspace = str(await az_json(*SECURITY_WORKSPACE_LOOKUP))
    since = datetime.now(UTC) - timedelta(minutes=30)
    query = f"""SQLSecurityAuditEvents
| where TimeGenerated > {kql_datetime(since)}
| where DatabasePrincipalName == "{CONFIG_OPERATORS_USER}" and ApplicationName == "docchat-ops"
| where Statement has "ops_config"
| summarize rows = count()"""
    rows = await poll_logs(workspace, query, found=lambda rows: bool(rows and rows[0]["rows"]))
    assert rows, "the log-docchat-security query answered no row"
    assert rows[0]["rows"], "no config operator SELECT with its text in log-docchat-security"


SENTINEL_PREFIX: Final = "docchat-audit-sentinel-"
SENTINEL_STATEMENT: Final = (
    select(func.count()).select_from(users).where(users.c.display_name == bindparam("sentinel"))
)
"""`SELECT COUNT(*) FROM app.users WHERE display_name = ?` as a Core construct: raw SQL lives
only in `docchat_core.sql.statements`."""
MASTER_SECURITY_SETTING: Final = "sql-audit-security"


def run_sentinel(server: str, token: str, sentinel: str) -> None:
    """The sentinel statement with its value bound, as the deploy identity."""
    engine = create_sync_engine(
        "grants",
        access_token=token,
        sql=SqlSettings(sql_server=server, sql_database="docchat"),
        local=LocalSettings(),
    )
    try:
        with engine.connect() as connection:
            connection.execute(SENTINEL_STATEMENT, {"sentinel": sentinel})
    finally:
        engine.dispose()


def write_summary(lines: list[str]) -> None:
    """The reading the stage-0 record copies into ADR 0108, in the job summary when there is
    one and on standard output."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in lines)
    sys.stdout.writelines(f"{line}\n" for line in lines)


@pytest.mark.infra
@pytest.mark.timeout(int(AUDIT_LIMIT.total_seconds()) + 900)
async def test_t451_the_sql_audit_sentinel_check(azd_environment: dict[str, str]) -> None:
    values = azd_environment
    server = values["DOCCHAT_SQL_SERVER"]
    workspace = values["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"]
    sentinel = f"{SENTINEL_PREFIX}{uuid.uuid4()}"
    token = await bearer_token(SQL_SCOPE)
    runner = await asyncio.to_thread(FIREWALL.resolve_runner_address)
    started = datetime.now(UTC)
    assert await run_firewall("open") == 0, "firewall.py open failed"
    try:
        await asyncio.to_thread(run_sentinel, server, token, sentinel)
    finally:
        closed = await run_firewall("close")
    assert closed == 0, "firewall.py close failed"
    await asyncio.sleep(AUDIT_LIMIT.total_seconds())

    bound = await query_logs(
        workspace,
        f"""SQLSecurityAuditEvents
| where Statement has '{sentinel}' or AdditionalInformation has '{sentinel}'
| count""",
        "P1D",
    )
    statement = counts(await query_logs(
        workspace,
        audit_query(
            f'ClientIp == "{runner}" and Statement has "app.users" '
            'and Statement has "display_name"',
            started,
        ),
        "P1D",
    ))  # fmt: skip
    ingestion = counts(await query_logs(
        workspace,
        audit_query(
            f'DatabasePrincipalName == "{INGESTION_USER}" '
            f'and ActionName == "{AUTHENTICATION_SUCCEEDED}"',
            started - timedelta(days=1),
        ),
        "P2D",
    ))  # fmt: skip
    master = (
        f"/subscriptions/{values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.Sql/servers/{values['DOCCHAT_SQL_SERVER_NAME']}/databases/master"
    )
    settings = await az_json("monitor", "diagnostic-settings", "list", "--resource", master)
    listed = settings.get("value", []) if isinstance(settings, dict) else settings or []
    sentinel_rows = int(bound[0]["Count"]) if bound else 0
    groups = "the change and authentication groups" if sentinel_rows else "BATCH_COMPLETED_GROUP"
    write_summary([
        f"SQL audit sentinel check: {sentinel_rows} row(s) hold the bound value;",
        f"the workload audit's action groups are those with {groups} (83-infrastructure.md 2.9).",
    ])  # fmt: skip

    assert statement.get("privileged", 0) >= 1, (
        "the sentinel statement isn't in the privileged audit"
    )
    assert statement.get("workload", 0) == 0, "the sentinel statement is in the workload audit"
    assert ingestion.get("workload", 0) >= 1, "no ingestion login in the workload audit"
    assert ingestion.get("privileged", 0) == 0, "the ingestion login is in the privileged audit"
    assert MASTER_SECURITY_SETTING in {str(item.get("name")) for item in listed}


# --- Stage 0: the GitHub environments (T-457, person-run) ---------------------------------------

PROTECTED_ENVIRONMENTS: Final = {
    "prod": "docchat-release-approvers",
    "load-access": "docchat-tenant-admins",
}
CITEST_ENVIRONMENTS: Final = frozenset({"dev-tests", "load-tests"})


async def gh_json(*args: str) -> Any:
    executable = shutil.which("gh")
    assert executable is not None, "the GitHub CLI isn't on PATH"
    process = await asyncio.create_subprocess_exec(
        executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    async with asyncio.timeout(AZ_TIMEOUT_SECONDS):
        output, errors = await process.communicate()
    assert process.returncode == 0, f"gh {args[0]} failed: {errors.decode(errors='replace')[:300]}"
    return json.loads(output)


def environment_problems(name: str, environment: Mapping[str, Any], team: str) -> list[str]:
    problems: list[str] = []
    rules = [
        rule
        for rule in environment.get("protection_rules") or []
        if rule.get("type") == "required_reviewers"
    ]
    reviewers = {
        str((reviewer.get("reviewer") or {}).get("slug"))
        for rule in rules
        for reviewer in rule.get("reviewers") or []
        if reviewer.get("type") == "Team"
    }
    if reviewers != {team}:
        problems.append(f"{name}: reviewers {sorted(reviewers)}")
    if not rules or not all(rule.get("prevent_self_review") is True for rule in rules):
        problems.append(f"{name}: prevent_self_review isn't true")
    if environment.get("can_admins_bypass") is not False:
        problems.append(f"{name}: can_admins_bypass isn't false")
    return problems


def credential_environments(credentials: list[Mapping[str, Any]]) -> list[str]:
    """The GitHub environment each federated credential's subject names (`:environment:<name>`),
    or the whole subject when it names none."""
    found: list[str] = []
    for credential in credentials:
        subject = str(credential.get("subject", ""))
        _, marker, environment = subject.rpartition(":environment:")
        found.append(environment if marker else subject)
    return found


@pytest.mark.infra
async def test_t457_the_protected_environments_and_the_test_identitys_credentials() -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.skip(
            "person-run: the job's token can't read the GitHub environments and no pipeline "
            "identity has a right in rg-docchat-identity (83-infrastructure.md section 12.4)"
        )
    repository = str((await gh_json("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"])
    problems: list[str] = []
    for name, team in PROTECTED_ENVIRONMENTS.items():
        environment = await gh_json("api", f"/repos/{repository}/environments/{name}")
        problems += environment_problems(name, environment, team)
    credentials = await az_json(
        "identity", "federated-credential", "list", "--identity-name", "id-docchat-citest",
        "--resource-group", "rg-docchat-identity",
    )  # fmt: skip
    named = credential_environments(credentials or [])

    assert problems == []
    assert sorted(named) == sorted(CITEST_ENVIRONMENTS)
