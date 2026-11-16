"""Post-provision identity steps of a tenant administrator (`83-infrastructure.md` section 7.5).

    az login --tenant <tenantId>
    uv run python scripts/identity_post_provision.py --environment <env> --resource-token <token> \\
      --users-group <object ID> --admins-group <object ID> [--admin <object ID> ...] \\
      [--test-users-group <object ID> --test-admins-group <object ID>] [--test-norole-group <id>]

Run by a holder of Cloud Application Administrator or Application Administrator after the first
provision of `dev` and `prod`, after every `load` creation and after any re-created registration:

1. finds the registrations by `uniqueName`, their service principals and Microsoft Graph's;
2. grants tenant-wide consent (`AllPrincipals`) from the web client, and in `dev` and `load` the
   test client, to Microsoft Graph, patching an existing grant to exactly that scope set;
3. assigns the user and admin groups (and every `--admin`) to the API's roles and the web
   client's default access;
4. in `dev` and `load` assigns the test groups, and in `dev` the no-role group;
5. skips assignments that exist and prints a summary with the run's correlation ID.

A refused or missing argument exits 1 before any Graph call. When Graph refuses the no-role
group's default-access assignment on the API (`400`), every other step completes and the script
exits 2 naming that step. Any other Graph failure exits 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, override

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential

if TYPE_CHECKING:
    from collections.abc import Sequence

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
ADMIN_ROLE = "47dc73a8-b97e-4be8-9481-614f6b17a11f"
USER_ROLE = "6f46cf45-f1e3-4812-8003-c51ae0f5f98d"
DEFAULT_ACCESS = "00000000-0000-0000-0000-000000000000"
WEB_GRANT_SCOPE = "openid profile offline_access"
TEST_GRANT_SCOPE = "openid profile"
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Built once at import, outside the event loop: loading the certificate store inside a coroutine
# blocks the loop for tens of milliseconds.
TLS_CONTEXT = httpx.create_ssl_context()
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
REFUSED_STEP = "no-role group default access on the API"


class UsageError(Exception):
    """An argument refused before any Graph call."""


class GraphError(Exception):
    """A Graph call that failed; the text names the call and the status, never a body."""

    def __init__(self, call: str, status: int) -> None:
        super().__init__(f"{call} answered {status}")
        self.status = status


class Parser(argparse.ArgumentParser):
    """Argument errors exit 1, the code section 7.5 gives a refused or missing argument."""

    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


@dataclass
class Summary:
    correlation_id: str
    grants_created: list[str] = field(default_factory=list)
    grants_updated: list[str] = field(default_factory=list)
    grants_unchanged: list[str] = field(default_factory=list)
    assignments_created: list[str] = field(default_factory=list)
    assignments_existing: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "correlationId": self.correlation_id,
            "grantsCreated": self.grants_created,
            "grantsUpdated": self.grants_updated,
            "grantsUnchanged": self.grants_unchanged,
            "assignmentsCreated": self.assignments_created,
            "assignmentsExisting": self.assignments_existing,
            "refused": self.refused,
        }


@dataclass(frozen=True)
class Assignment:
    resource: str  # service principal the role belongs to
    resource_label: str
    principal: str
    role: str
    role_label: str

    @property
    def label(self) -> str:
        return f"{self.principal} -> {self.resource_label} {self.role_label}"


def object_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} isn't an object ID") from error


def resource_token(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]{6}", value):
        raise argparse.ArgumentTypeError("the resource token is 6 lower-case letters or digits")
    return value


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = Parser(prog="identity_post_provision.py", description="Section 7.5 identity steps.")
    parser.add_argument("--environment", required=True, choices=("dev", "prod", "load"))
    parser.add_argument("--resource-token", required=True, type=resource_token)
    parser.add_argument("--users-group", required=True, type=object_id)
    parser.add_argument("--admins-group", required=True, type=object_id)
    parser.add_argument("--admin", action="append", default=[], type=object_id)
    parser.add_argument("--test-users-group", type=object_id)
    parser.add_argument("--test-admins-group", type=object_id)
    parser.add_argument("--test-norole-group", type=object_id)
    args = parser.parse_args(argv)
    test_groups = (args.test_users_group, args.test_admins_group)
    if args.environment == "prod":
        if any(test_groups) or args.test_norole_group:
            parser.error("prod has no test client: the test groups are refused")
    elif not all(test_groups):
        parser.error("--test-users-group and --test-admins-group are required in dev and load")
    if args.environment == "dev" and not args.test_norole_group:
        parser.error("--test-norole-group is required in dev")
    if args.environment == "load" and args.test_norole_group:
        parser.error("--test-norole-group is refused in load")
    return args


async def graph_token() -> str:
    async with AzureCliCredential() as credential:
        access = await credential.get_token(GRAPH_SCOPE)
    token: str = access.token
    return token


class Graph:
    def __init__(self, client: httpx.AsyncClient, token: str, correlation_id: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}", "client-request-id": correlation_id}

    async def _send(
        self, method: str, path: str, *, body: Any = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self._client.request(
            method, f"{GRAPH}{path}", headers=self._headers, json=body, params=params
        )

    async def get(self, path: str) -> dict[str, Any] | None:
        response = await self._send("GET", path)
        if response.status_code == HTTP_NOT_FOUND:
            return None
        if response.is_error:
            raise GraphError(f"GET {path}", response.status_code)
        data: dict[str, Any] = response.json()
        return data

    async def get_all(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        items: list[Any] = []
        response = await self._send("GET", path, params=params)
        while True:
            if response.is_error:
                raise GraphError(f"GET {path}", response.status_code)
            page = response.json()
            items.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")
            # A next link is followed only while it stays on Microsoft Graph.
            if not isinstance(next_link, str) or not next_link.startswith(GRAPH + "/"):
                return items
            response = await self._client.get(next_link, headers=self._headers)

    async def write(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        response = await self._send(method, path, body=body)
        if response.is_error and response.status_code != HTTP_BAD_REQUEST:
            raise GraphError(f"{method} {path}", response.status_code)
        return response


async def service_principal(graph: Graph, app_id: str, label: str) -> str:
    principal = await graph.get(f"/servicePrincipals(appId='{app_id}')")
    if principal is None:
        raise GraphError(f"the service principal of {label}", HTTP_NOT_FOUND)
    return str(principal["id"])


async def registration(graph: Graph, unique_name: str) -> str:
    application = await graph.get(f"/applications(uniqueName='{unique_name}')")
    if application is None:
        raise GraphError(f"the application {unique_name}", HTTP_NOT_FOUND)
    return await service_principal(graph, str(application["appId"]), unique_name)


async def ensure_grant(
    graph: Graph, *, client: str, label: str, resource: str, scope: str, summary: Summary
) -> None:
    query = (
        f"clientId eq '{client}' and consentType eq 'AllPrincipals' and resourceId eq '{resource}'"
    )
    grants = await graph.get_all("/oauth2PermissionGrants", params={"$filter": query})
    if not grants:
        body = {
            "clientId": client,
            "consentType": "AllPrincipals",
            "resourceId": resource,
            "scope": scope,
        }
        response = await graph.write("POST", "/oauth2PermissionGrants", body)
        if response.is_error:
            raise GraphError("POST /oauth2PermissionGrants", response.status_code)
        summary.grants_created.append(f"{label}: {scope}")
        return
    grant = grants[0]
    if set(str(grant.get("scope", "")).split()) == set(scope.split()):
        summary.grants_unchanged.append(f"{label}: {scope}")
        return
    path = f"/oauth2PermissionGrants/{grant['id']}"
    response = await graph.write("PATCH", path, {"scope": scope})
    if response.is_error:
        raise GraphError(f"PATCH {path}", response.status_code)
    summary.grants_updated.append(f"{label}: {scope}")


async def ensure_assignments(
    graph: Graph, wanted: list[Assignment], summary: Summary, *, may_refuse: Assignment | None
) -> None:
    existing: dict[str, set[tuple[str, str]]] = {}
    for item in wanted:
        if item.resource not in existing:
            assigned = await graph.get_all(f"/servicePrincipals/{item.resource}/appRoleAssignedTo")
            existing[item.resource] = {
                (str(a.get("principalId")), str(a.get("appRoleId"))) for a in assigned
            }
        if (item.principal, item.role) in existing[item.resource]:
            summary.assignments_existing.append(item.label)
            continue
        path = f"/servicePrincipals/{item.resource}/appRoleAssignedTo"
        body = {"principalId": item.principal, "resourceId": item.resource, "appRoleId": item.role}
        response = await graph.write("POST", path, body)
        if not response.is_error:
            existing[item.resource].add((item.principal, item.role))
            summary.assignments_created.append(item.label)
        elif item == may_refuse:
            # The API declares app roles, so Graph may refuse a default-access assignment.
            summary.refused.append(REFUSED_STEP)
        else:
            raise GraphError(f"POST {path}", response.status_code)


def wanted_assignments(
    args: argparse.Namespace, api: str, web: str, test: str | None
) -> tuple[list[Assignment], Assignment | None]:
    def to(resource: str, label: str, principal: str, role: str, role_label: str) -> Assignment:
        return Assignment(resource, label, principal, role, role_label)

    wanted: list[Assignment] = [to(api, "API", args.users_group, USER_ROLE, "User")]
    wanted.extend(to(api, "API", a, ADMIN_ROLE, "Admin") for a in [args.admins_group, *args.admin])
    wanted.extend(
        to(web, "web client", principal, DEFAULT_ACCESS, "default access")
        for principal in [args.users_group, args.admins_group, *args.admin]
    )
    norole: Assignment | None = None
    if test is not None:
        wanted.append(to(api, "API", args.test_users_group, USER_ROLE, "User"))
        wanted.append(to(api, "API", args.test_admins_group, ADMIN_ROLE, "Admin"))
        groups = [args.test_users_group, args.test_admins_group]
        if args.test_norole_group:
            norole = to(api, "API", args.test_norole_group, DEFAULT_ACCESS, "default access")
            groups.append(args.test_norole_group)
        for group in groups:
            wanted.append(to(web, "web client", group, DEFAULT_ACCESS, "default access"))
            wanted.append(to(test, "test client", group, DEFAULT_ACCESS, "default access"))
        if norole is not None:
            # Last, so every other assignment completes even when Graph refuses this one.
            wanted.append(norole)
    return wanted, norole


async def run(args: argparse.Namespace, token: str, client: httpx.AsyncClient) -> Summary:
    summary = Summary(correlation_id=str(uuid.uuid4()))
    graph = Graph(client, token, summary.correlation_id)
    prefix = f"docchat-{args.environment}-{args.resource_token}"
    with_test = args.environment != "prod"
    web = await registration(graph, f"{prefix}-web")
    api = await registration(graph, f"{prefix}-api")
    test = await registration(graph, f"{prefix}-test") if with_test else None
    microsoft_graph = await service_principal(graph, GRAPH_APP_ID, "Microsoft Graph")
    await ensure_grant(
        graph, client=web, label="web client", resource=microsoft_graph,
        scope=WEB_GRANT_SCOPE, summary=summary,
    )  # fmt: skip
    if test is not None:
        await ensure_grant(
            graph, client=test, label="test client", resource=microsoft_graph,
            scope=TEST_GRANT_SCOPE, summary=summary,
        )  # fmt: skip
    wanted, norole = wanted_assignments(args, api, web, test)
    await ensure_assignments(graph, wanted, summary, may_refuse=norole)
    return summary


async def main_async(args: argparse.Namespace) -> int:
    try:
        token = await graph_token()
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, verify=TLS_CONTEXT
        ) as client:
            summary = await run(args, token, client)
    except (GraphError, ClientAuthenticationError, httpx.HTTPError) as error:
        sys.stderr.write(f"identity_post_provision.py failed: {type(error).__name__}: {error}\n")
        return 1
    sys.stdout.write(json.dumps(summary.to_json(), indent=2) + "\n")
    if summary.refused:
        sys.stderr.write(
            f"Graph refused the step '{REFUSED_STEP}'; set DOCCHAT_TEST_NOROLE_SKIP_REASON to "
            "graph_default_access_refused (runbook 'First admins and test identities').\n"
        )
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
