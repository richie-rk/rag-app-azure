"""Region, quota and model preflight of one environment (`83-infrastructure.md` section 3).

    uv run --frozen python scripts/preflight/preflight.py --env <env> [--check all|models]

Python 3.13 standard library plus `az` CLI calls through `subprocess`. Reads
`infra/env/<env>.json`, `infra/config/regions.json`, `DOCCHAT_OPERATOR_IP_ALLOWLIST` (check 9)
and `DOCCHAT_OPS_ALERT_EMAILS` (check 12); prints one JSON report, writes it to
`reports/preflight-<env>.json` and the job summary, and exits 2 when any check fails.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = ("dev", "prod", "load")
ARM = "https://management.azure.com"
COMMAND_TIMEOUT_SECONDS = 300.0
DETAIL_LIMIT = 500

PROVIDERS = (
    "Microsoft.Web",
    "Microsoft.Sql",
    "Microsoft.DocumentDB",
    "Microsoft.Storage",
    "Microsoft.Search",
    "Microsoft.CognitiveServices",
    "Microsoft.KeyVault",
    "Microsoft.ContainerRegistry",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.Security",
    "Microsoft.EventGrid",
    "Microsoft.AlertsManagement",
    "Microsoft.ContainerInstance",
)
REGIONAL_TYPES = (
    "Microsoft.Web/sites",
    "Microsoft.Web/serverFarms",
    "Microsoft.Sql/servers",
    "Microsoft.DocumentDB/databaseAccounts",
    "Microsoft.Storage/storageAccounts",
    "Microsoft.Search/searchServices",
    "Microsoft.KeyVault/vaults",
    "Microsoft.Network/privateEndpoints",
)
STATIC_SITES_TYPE = "Microsoft.Web/staticSites"
AI_KINDS = ("OpenAI", "ContentSafety", "FormRecognizer")
COGNITIVE_API = "2026-07-01"
GEO_REGIONS_API = "2025-03-01"
MODELS_API = "2024-10-01"
LOCATIONS_API = "2022-12-01"
CONTAINER_INSTANCE_API = "2026-07-01"
RETIRES_SOON_DAYS = 183
RETIREMENT_WARNING_DAYS = 274
ALLOWLIST_LIMIT = 20
NON_PUBLIC_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
    )
)
FIC_UNSUPPORTED_REGION = "malaysiasouth"
LOAD_GENERATOR_CPU = 4
LOAD_GENERATOR_MEMORY_GB = 8
LOAD_GENERATOR_FREE_CORES = 4
CUSTOM_DOMAIN_LIMITS = {"Free": 2, "Standard": 5}

MESSAGES = {
    "provider_not_registered": (
        "A required resource provider isn't registered in this subscription; "
        "run the bootstrap's `providers` step."
    ),
    "service_not_in_region": (
        "A required service isn't offered in the chosen region; pick another region."
    ),
    "ai_sku_unavailable": "An Azure AI account kind can't be created with S0 in this region.",
    "linux_premium_unavailable": (
        "Linux Elastic Premium isn't offered here, or the burst limit exceeds the region's maximum."
    ),
    "prompt_shields_unavailable": "A required AI feature isn't offered in this region.",
    "semantic_ranker_unavailable": "A required AI feature isn't offered in this region.",
    "document_intelligence_unavailable": "A required AI feature isn't offered in this region.",
    "model_version_not_offered": (
        "A pinned model version isn't offered for its deployment type in this region."
    ),
    "model_sku_not_offered": (
        "A pinned model version isn't offered for its deployment type in this region."
    ),
    "model_retired": (
        "A pinned model version is retired, closed to new deployments, or retires within "
        "6 months; pin a newer version."
    ),
    "model_deprecated": (
        "A pinned model version is retired, closed to new deployments, or retires within "
        "6 months; pin a newer version."
    ),
    "model_retires_soon": (
        "A pinned model version is retired, closed to new deployments, or retires within "
        "6 months; pin a newer version."
    ),
    "quota_insufficient": (
        "Not enough model quota for the requested capacity; the detail names the shortfall "
        "in units."
    ),
    "availability_zones_unavailable": (
        "The region has no availability zones, which this environment needs."
    ),
    "allowlist_invalid": (
        "The operator allowlist holds an entry that isn't a single public IPv4 address, "
        "or more than 20 entries."
    ),
    "fic_region_unsupported": "Managed identities in this region can't hold federated credentials.",
    "alert_receivers_missing": "Production needs at least one ops alert address.",
    "container_capacity_unavailable": (
        "Linux container groups of 4 vCPU and 8 GB aren't available in this region for the "
        "load generator."
    ),
    "custom_domains_exceed_plan": (
        "The web client lists more custom domains than its Static Web Apps plan allows "
        "(2 on Free, 5 on Standard)."
    ),
}
INPUTS_MESSAGE = "The preflight's inputs couldn't be read; no check ran."


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class AzCallError(Exception):
    """An Azure call that failed; its text names the call, never a response body."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    code: str | None
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "detail": self.detail[:DETAIL_LIMIT],
        }


def passed(name: str, detail: str) -> Check:
    return Check(name, "pass", None, detail)


def failed(name: str, code: str, detail: str) -> Check:
    return Check(name, "fail", code, detail)


@dataclass(frozen=True)
class Deployment:
    name: str
    model: str
    version: str
    sku: str
    capacity: int


@dataclass
class Context:
    env: str
    config: dict[str, Any]
    regions: dict[str, Any]
    today: date
    _existing: dict[str, Deployment] | None = field(default=None, repr=False)

    @property
    def subscription(self) -> str:
        return str(self.config["subscriptionId"])

    @property
    def location(self) -> str:
        return normalise_region(str(self.config["location"]))

    @property
    def openai_enabled(self) -> bool:
        return bool(self.config.get("openAi", {}).get("enabled")) and self.env != "load"

    def existing_deployments(self) -> dict[str, Deployment]:
        """Deployments already in the environment's Azure OpenAI account, by name."""
        if self._existing is None:
            self._existing = read_existing_deployments(self.env, self.subscription)
        return self._existing


# --- seams to the outside world -------------------------------------------------------------


def run_command(args: Sequence[str]) -> CommandResult:
    executable = shutil.which(args[0])
    if executable is None:
        return CommandResult(127, "", f"{args[0]} is not on PATH")
    try:
        # Fixed command words plus values read from the reviewed environment file; no shell.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{args[0]} timed out")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def today() -> date:
    return datetime.now(UTC).date()


def now() -> datetime:
    return datetime.now(UTC)


# --- Azure calls ----------------------------------------------------------------------------


def az_json(description: str, *args: str) -> Any:
    result = run_command(["az", *args, "--output", "json"])
    if result.returncode != 0:
        last = (result.stderr.strip().splitlines() or ["no error output"])[-1]
        raise AzCallError(f"{description} failed: {last[:200]}")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise AzCallError(f"{description} answered no JSON") from error


def arm(method: str, path: str, description: str, body: str | None = None) -> Any:
    args = ["rest", "--method", method, "--url", f"{ARM}{path}"]
    if body is not None:
        args += ["--body", body, "--headers", "Content-Type=application/json"]
    return az_json(description, *args)


def arm_list(path: str, description: str) -> list[Any]:
    """Every `value` entry of a Resource Manager list, following `nextLink`."""
    items: list[Any] = []
    url = f"{ARM}{path}"
    while url:
        page = az_json(description, "rest", "--method", "get", "--url", url)
        if not isinstance(page, dict):
            raise AzCallError(f"{description} answered no list")
        value = page.get("value")
        items.extend(value if isinstance(value, list) else [])
        next_link = page.get("nextLink")
        url = next_link if isinstance(next_link, str) and next_link.startswith(ARM) else ""
    return items


def read_existing_deployments(env: str, subscription: str) -> dict[str, Deployment]:
    group = f"rg-docchat-{env}"
    accounts = az_json(
        "az cognitiveservices account list",
        "cognitiveservices", "account", "list", "--resource-group", group,
        "--subscription", subscription,
    )  # fmt: skip
    openai = [a for a in accounts or [] if isinstance(a, dict) and a.get("kind") == "OpenAI"]
    found: dict[str, Deployment] = {}
    for account in openai:
        deployments = az_json(
            "az cognitiveservices account deployment list",
            "cognitiveservices", "account", "deployment", "list", "--resource-group", group,
            "--name", str(account.get("name")), "--subscription", subscription,
        )  # fmt: skip
        for item in deployments or []:
            model = item.get("properties", {}).get("model", {})
            sku = item.get("sku", {})
            found[str(item.get("name"))] = Deployment(
                name=str(item.get("name")),
                model=str(model.get("name")),
                version=str(model.get("version")),
                sku=str(sku.get("name")),
                capacity=int(sku.get("capacity") or 0),
            )
    return found


# --- helpers --------------------------------------------------------------------------------


def normalise_region(name: str) -> str:
    """Resource Manager mixes `eastus2` and `East US 2`; compare them as one."""
    return name.replace(" ", "").lower()


def region_list(regions: dict[str, Any], key: str) -> set[str]:
    value = regions.get(key)
    if isinstance(value, dict):
        value = value.get("regions")
    if not isinstance(value, list):
        raise KeyError(f"infra/config/regions.json has no region list {key}")
    return {normalise_region(str(item)) for item in value}


def linux_premium_maximum(regions: dict[str, Any], location: str) -> int | None:
    """The Linux Elastic Premium maximum for a region, or None where the plan has none.

    `regions.json` holds the named maxima under `regions`, the value for every other region
    where the plan exists under `otherRegions`, and the regions without one under `absent`
    (83-infrastructure.md section 3.5).
    """
    value = regions.get("linuxPremiumMaxInstances")
    if not isinstance(value, dict) or not isinstance(value.get("regions"), dict):
        raise KeyError("infra/config/regions.json has no region table linuxPremiumMaxInstances")
    named = {normalise_region(str(name)): int(limit) for name, limit in value["regions"].items()}
    if location in named:
        return named[location]
    absent = {normalise_region(str(name)) for name in value.get("absent", [])}
    other = value.get("otherRegions")
    if location in absent or not isinstance(other, int):
        return None
    return other


def split_list(raw: str) -> list[str]:
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def model_deployments(config: dict[str, Any]) -> list[Deployment]:
    """The five (or more) deployments, each with its own deployment type (section 5.2)."""
    openai = config.get("openAi", {})
    found: list[Deployment] = []
    for role in ("chat", "chatSmall", "tableSummary"):
        item = openai["deployments"][role]
        found.append(
            Deployment(
                name=str(item["name"]),
                model=str(item["model"]),
                version=str(item["version"]),
                sku=str(item.get("type", "GlobalStandard")),
                capacity=int(item["capacity"]),
            )
        )
    for item in openai.get("embeddingModels", []):
        for prefix, sku_key, capacity_key in (
            ("embed-query", "queryType", "queryCapacity"),
            ("embed-ingest", "ingestionType", "ingestionCapacity"),
        ):
            found.append(
                Deployment(
                    name=f"{prefix}-{item['key']}",
                    model=str(item["model"]),
                    version=str(item["version"]),
                    sku=str(item.get(sku_key, "GlobalStandard")),
                    capacity=int(item[capacity_key]),
                )
            )
    return found


def retirement_date(entry: dict[str, Any], sku: dict[str, Any] | None) -> date | None:
    """The earlier of the matching SKU's deprecationDate and model.deprecation.inference."""
    candidates = [
        (sku or {}).get("deprecationDate"),
        entry.get("model", {}).get("deprecation", {}).get("inference"),
    ]
    dates: list[date] = []
    for text in candidates:
        if isinstance(text, str) and len(text) >= len("yyyy-mm-dd"):
            with contextlib.suppress(ValueError):
                dates.append(date.fromisoformat(text[:10]))
    return min(dates) if dates else None


# --- checks 1 and 2 -------------------------------------------------------------------------


def check_providers_and_regions(ctx: Context) -> list[Check]:
    providers: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for namespace in PROVIDERS:
        try:
            data = az_json(
                f"az provider show -n {namespace}",
                "provider", "show", "--namespace", namespace, "--subscription", ctx.subscription,
            )  # fmt: skip
        except AzCallError as error:
            errors[namespace] = str(error)
            continue
        providers[namespace] = data if isinstance(data, dict) else {}
    return [_check_registered(providers, errors), _check_offered(ctx, providers, errors)]


def _check_registered(providers: dict[str, dict[str, Any]], errors: dict[str, str]) -> Check:
    name = "resource_providers_registered"
    if errors:
        return failed(name, "provider_not_registered", "; ".join(errors.values()))
    missing = [
        ns for ns, data in providers.items() if data.get("registrationState") != "Registered"
    ]
    if missing:
        return failed(name, "provider_not_registered", "not registered: " + ", ".join(missing))
    return passed(name, f"{len(providers)} providers registered")


def _resource_type_regions(provider: dict[str, Any], type_name: str) -> set[str] | None:
    for item in provider.get("resourceTypes", []):
        if str(item.get("resourceType", "")).lower() == type_name.lower():
            return {normalise_region(str(loc)) for loc in item.get("locations", [])}
    return None


def _check_offered(
    ctx: Context, providers: dict[str, dict[str, Any]], errors: dict[str, str]
) -> Check:
    name = "services_in_region"
    wanted = [(full, ctx.location) for full in REGIONAL_TYPES]
    wanted.append((STATIC_SITES_TYPE, normalise_region(str(ctx.config["staticWebAppLocation"]))))
    problems: list[str] = []
    for full, region in wanted:
        namespace, type_name = full.split("/", 1)
        if namespace in errors:
            problems.append(errors[namespace])
            continue
        regions = _resource_type_regions(providers.get(namespace, {}), type_name)
        if regions is None or region not in regions:
            problems.append(f"{full} not offered in {region}")
    if problems:
        return failed(name, "service_not_in_region", "; ".join(dict.fromkeys(problems)))
    return passed(name, f"every required service is offered in {ctx.location}")


# --- checks 3 to 5, 8 to 14 -----------------------------------------------------------------


def check_ai_skus(ctx: Context) -> list[Check]:
    name = "ai_account_skus"
    path = (
        f"/subscriptions/{ctx.subscription}/providers/Microsoft.CognitiveServices/locations/"
        f"{ctx.location}/checkSkuAvailability?api-version={COGNITIVE_API}"
    )
    unavailable: list[str] = []
    for kind in AI_KINDS:
        body = json.dumps(
            {"skus": ["S0"], "kind": kind, "type": "Microsoft.CognitiveServices/accounts"}
        )
        try:
            answer = arm("post", path, f"checkSkuAvailability for {kind}", body)
        except AzCallError as error:
            return [failed(name, "ai_sku_unavailable", str(error))]
        values = answer.get("value", []) if isinstance(answer, dict) else []
        if not values or not all(item.get("skuAvailable") is True for item in values):
            unavailable.append(kind)
    if unavailable:
        return [failed(name, "ai_sku_unavailable", "S0 unavailable for " + ", ".join(unavailable))]
    return [passed(name, "S0 available for " + ", ".join(AI_KINDS))]


def check_linux_premium(ctx: Context) -> list[Check]:
    name = "linux_elastic_premium"
    path = (
        f"/subscriptions/{ctx.subscription}/providers/Microsoft.Web/geoRegions"
        f"?sku=ElasticPremium&linuxWorkersEnabled=true&api-version={GEO_REGIONS_API}"
    )
    try:
        regions = arm_list(path, "geoRegions for ElasticPremium")
    except AzCallError as error:
        return [failed(name, "linux_premium_unavailable", str(error))]
    offered = set()
    for item in regions:
        offered.add(normalise_region(str(item.get("name", ""))))
        offered.add(normalise_region(str(item.get("properties", {}).get("name", ""))))
    if ctx.location not in offered:
        return [failed(name, "linux_premium_unavailable", f"not offered in {ctx.location}")]
    limit = linux_premium_maximum(ctx.regions, ctx.location)
    burst = int(ctx.config["ingestion"]["maxBurst"])
    if limit is None:
        detail = f"regions.json has no Linux maximum for {ctx.location}"
        return [failed(name, "linux_premium_unavailable", detail)]
    if burst > limit:
        detail = f"ingestion.maxBurst {burst} exceeds {ctx.location}'s maximum of {limit}"
        return [failed(name, "linux_premium_unavailable", detail)]
    return [passed(name, f"maxBurst {burst} within {limit}")]


def check_region_features(ctx: Context) -> list[Check]:
    checks: list[Check] = []
    for check_name, key, code in (
        ("prompt_shields", "promptShieldsRegions", "prompt_shields_unavailable"),
        ("semantic_ranker", "semanticRankerRegions", "semantic_ranker_unavailable"),
    ):
        if ctx.location in region_list(ctx.regions, key):
            checks.append(passed(check_name, f"listed for {ctx.location}"))
        else:
            checks.append(failed(check_name, code, f"{ctx.location} isn't in {key}"))
    return checks


def check_availability_zones(ctx: Context) -> list[Check]:
    name = "availability_zones"
    path = f"/subscriptions/{ctx.subscription}/locations?api-version={LOCATIONS_API}"
    try:
        locations = arm_list(path, "locations list")
    except AzCallError as error:
        return [failed(name, "availability_zones_unavailable", str(error))]
    for item in locations:
        if normalise_region(str(item.get("name", ""))) == ctx.location:
            if item.get("availabilityZoneMappings"):
                return [passed(name, f"{ctx.location} has availability zones")]
            break
    return [failed(name, "availability_zones_unavailable", f"{ctx.location} has no zones")]


def zones_required(ctx: Context) -> bool:
    if ctx.env in {"prod", "load"}:
        return True
    return bool(ctx.config.get("api", {}).get("zoneRedundant")) or bool(
        ctx.config.get("sql", {}).get("zoneRedundant")
    )


def check_allowlist(ctx: Context) -> list[Check]:
    name = "operator_allowlist"
    entries = split_list(os.environ.get("DOCCHAT_OPERATOR_IP_ALLOWLIST", ""))
    if len(entries) > ALLOWLIST_LIMIT:
        detail = f"{len(entries)} entries, at most {ALLOWLIST_LIMIT}"
        return [failed(name, "allowlist_invalid", detail)]
    for position, entry in enumerate(entries, start=1):
        try:
            address = ipaddress.IPv4Address(entry)
        except ValueError:
            # The entry itself may be mistyped personal data, so only its position is named.
            detail = f"entry {position} isn't a dotted-quad IPv4 address"
            return [failed(name, "allowlist_invalid", detail)]
        if any(address in network for network in NON_PUBLIC_NETWORKS):
            detail = f"entry {position} isn't a public address"
            return [failed(name, "allowlist_invalid", detail)]
    return [passed(name, f"{len(entries)} public IPv4 addresses")]


def check_identity_region(ctx: Context) -> list[Check]:
    name = "federated_credential_region"
    try:
        location = az_json(
            "az group show -n rg-docchat-identity",
            "group", "show", "--name", "rg-docchat-identity", "--subscription", ctx.subscription,
            "--query", "location",
        )  # fmt: skip
    except AzCallError as error:
        return [failed(name, "fic_region_unsupported", str(error))]
    region = normalise_region(str(location))
    if region == FIC_UNSUPPORTED_REGION:
        detail = f"rg-docchat-identity is in {region}"
        return [failed(name, "fic_region_unsupported", detail)]
    return [passed(name, f"rg-docchat-identity is in {region}")]


def check_document_intelligence(ctx: Context) -> list[Check]:
    name = "document_intelligence_layout"
    if ctx.location in region_list(ctx.regions, "documentIntelligenceLayoutRegions"):
        return [passed(name, f"layout listed for {ctx.location}")]
    detail = f"{ctx.location} isn't in documentIntelligenceLayoutRegions"
    return [failed(name, "document_intelligence_unavailable", detail)]


def check_alert_receivers(ctx: Context) -> list[Check]:
    name = "alert_receivers"
    entries = split_list(os.environ.get("DOCCHAT_OPS_ALERT_EMAILS", ""))
    addresses = [e for e in entries if e.count("@") == 1 and all(e.split("@"))]
    if not addresses:
        return [failed(name, "alert_receivers_missing", "DOCCHAT_OPS_ALERT_EMAILS is empty")]
    return [passed(name, f"{len(addresses)} ops alert addresses")]


def check_load_generator(ctx: Context) -> list[Check]:
    name = "load_generator_capacity"
    base = (
        f"/subscriptions/{ctx.subscription}/providers/Microsoft.ContainerInstance/locations/"
        f"{ctx.location}"
    )
    try:
        capabilities = arm_list(
            f"{base}/capabilities?api-version={CONTAINER_INSTANCE_API}",
            "Container Instances capabilities",
        )
        usages = arm_list(
            f"{base}/usages?api-version={CONTAINER_INSTANCE_API}", "Container Instances usages"
        )
    except AzCallError as error:
        return [failed(name, "container_capacity_unavailable", str(error))]
    fits = any(
        item.get("resourceType") == "containerGroups"
        and item.get("osType") == "Linux"
        and item.get("gpu") == "None"
        and float(item.get("capabilities", {}).get("maxCpu", 0)) >= LOAD_GENERATOR_CPU
        and float(item.get("capabilities", {}).get("maxMemoryInGB", 0)) >= LOAD_GENERATOR_MEMORY_GB
        for item in capabilities
    )
    if not fits:
        detail = "no Linux container group of 4 vCPU and 8 GB without GPU"
        return [failed(name, "container_capacity_unavailable", detail)]
    cores = next(
        (u for u in usages if u.get("name", {}).get("value") == "StandardCores"),
        None,
    )
    free = int(cores.get("limit", 0)) - int(cores.get("currentValue", 0)) if cores else 0
    if free < LOAD_GENERATOR_FREE_CORES:
        detail = f"{free} standard cores free, {LOAD_GENERATOR_FREE_CORES} needed"
        return [failed(name, "container_capacity_unavailable", detail)]
    return [passed(name, f"{free} standard cores free")]


def check_custom_domains(ctx: Context) -> list[Check]:
    name = "static_web_apps_custom_domains"
    sku = str(ctx.config.get("web", {}).get("sku", "Free"))
    domains = ctx.config.get("webCustomDomains", [])
    limit = CUSTOM_DOMAIN_LIMITS.get(sku, CUSTOM_DOMAIN_LIMITS["Free"])
    if len(domains) > limit:
        detail = f"{len(domains)} custom domains on {sku}, at most {limit}"
        return [failed(name, "custom_domains_exceed_plan", detail)]
    return [passed(name, f"{len(domains)} custom domains on {sku}")]


# --- checks 6 and 7 -------------------------------------------------------------------------


def _models_offered(ctx: Context) -> list[dict[str, Any]]:
    path = (
        f"/subscriptions/{ctx.subscription}/providers/Microsoft.CognitiveServices/locations/"
        f"{ctx.location}/models?api-version={MODELS_API}"
    )
    return [item for item in arm_list(path, "models list") if isinstance(item, dict)]


def _matching_entry(
    entries: list[dict[str, Any]], deployment: Deployment
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    matches = [
        entry
        for entry in entries
        if entry.get("model", {}).get("format") == "OpenAI"
        and entry.get("model", {}).get("name") == deployment.model
        and entry.get("model", {}).get("version") == deployment.version
    ]
    for entry in matches:
        for sku in entry.get("model", {}).get("skus", []):
            if sku.get("name") == deployment.sku:
                return entry, sku
    return (matches[0], None) if matches else None


def check_model_versions(ctx: Context, *, warn: bool) -> list[Check]:
    name = "model_version"
    try:
        entries = _models_offered(ctx)
    except AzCallError as error:
        return [failed(name, "model_version_not_offered", str(error))]
    return [
        _check_one_model(ctx, entries, deployment, warn=warn)
        for deployment in model_deployments(ctx.config)
    ]


def _check_one_model(
    ctx: Context, entries: list[dict[str, Any]], deployment: Deployment, *, warn: bool
) -> Check:
    label = f"{deployment.name}: {deployment.model} {deployment.version} ({deployment.sku})"
    match = _matching_entry(entries, deployment)
    if match is None:
        return failed("model_version", "model_version_not_offered", f"{label} isn't offered")
    entry, sku = match
    refusal = _lifecycle_refusal(ctx, entry, deployment, label)
    if refusal is not None:
        return refusal
    if sku is None:
        detail = f"{label}: no {deployment.sku} SKU"
        return failed("model_version", "model_sku_not_offered", detail)
    return _retirement_check(ctx, retirement_date(entry, sku), label, warn=warn)


def _lifecycle_refusal(
    ctx: Context, entry: dict[str, Any], deployment: Deployment, label: str
) -> Check | None:
    status = entry.get("model", {}).get("lifecycleStatus")
    if status == "Deprecated":  # the Models API's word for retired
        return failed("model_version", "model_retired", f"{label} is retired")
    if status != "Deprecating":
        return None
    # Closed to new customers: only an account that already holds the version may keep it.
    try:
        existing = ctx.existing_deployments().values()
    except AzCallError as error:
        return failed("model_version", "model_deprecated", f"{label} is deprecating; {error}")
    if any(d.model == deployment.model and d.version == deployment.version for d in existing):
        return None
    return failed("model_version", "model_deprecated", f"{label} is closed to new deployments")


def _retirement_check(ctx: Context, retires: date | None, label: str, *, warn: bool) -> Check:
    if retires is None:
        return passed("model_version", f"{label} offered")
    if retires < ctx.today + timedelta(days=RETIRES_SOON_DAYS):
        return failed("model_version", "model_retires_soon", f"{label} retires on {retires}")
    if warn and retires < ctx.today + timedelta(days=RETIREMENT_WARNING_DAYS):
        return Check("model_version", "warn", None, f"{label} retires on {retires}")
    return passed("model_version", f"{label} offered; retires on {retires}")


def check_quota(ctx: Context) -> list[Check]:
    name = "model_quota"
    groups: dict[tuple[str, str, str], list[Deployment]] = defaultdict(list)
    for deployment in model_deployments(ctx.config):
        groups[(deployment.model, deployment.version, deployment.sku)].append(deployment)
    try:
        existing = ctx.existing_deployments()
    except AzCallError as error:
        return [failed(name, "quota_insufficient", str(error))]
    capacities: dict[tuple[str, str], list[Any]] = {}
    checks: list[Check] = []
    for (model, version, sku), members in groups.items():
        label = f"{model} {version} ({sku})"
        wanted = sum(d.capacity for d in members)
        held = sum(existing[d.name].capacity for d in members if d.name in existing)
        needed = wanted - held
        try:
            if (model, version) not in capacities:
                capacities[(model, version)] = _model_capacities(ctx, model, version)
        except AzCallError as error:
            checks.append(failed(name, "quota_insufficient", f"{label}: {error}"))
            continue
        available = _available(capacities[(model, version)], ctx.location, sku)
        if needed > available:
            detail = f"{label}: short by {needed - available} units ({needed} needed)"
            checks.append(failed(name, "quota_insufficient", detail))
        else:
            checks.append(passed(name, f"{label}: {needed} of {available} units"))
    return checks


def _model_capacities(ctx: Context, model: str, version: str) -> list[Any]:
    query = urllib.parse.urlencode(
        {
            "api-version": MODELS_API,
            "modelFormat": "OpenAI",
            "modelName": model,
            "modelVersion": version,
        }
    )
    path = (
        f"/subscriptions/{ctx.subscription}/providers/Microsoft.CognitiveServices"
        f"/modelCapacities?{query}"
    )
    return arm_list(path, f"modelCapacities for {model} {version}")


def _available(entries: list[Any], region: str, sku: str) -> int:
    for item in entries:
        properties = item.get("properties", {}) if isinstance(item, dict) else {}
        if (
            normalise_region(str(item.get("location", ""))) == region
            and properties.get("skuName") == sku
        ):
            return int(properties.get("availableCapacity", 0))
    return 0


# --- the run --------------------------------------------------------------------------------


CheckRunner = tuple[str, "Callable[[Context], list[Check]]"]


def checks_for(ctx: Context, mode: str) -> Iterator[CheckRunner]:
    """The checks of section 3.3 that apply to this environment, in table order."""
    if mode == "models":
        if ctx.openai_enabled:
            yield "model_version", lambda c: check_model_versions(c, warn=True)
        return
    yield "resource_providers", check_providers_and_regions
    yield "ai_account_skus", check_ai_skus
    yield "linux_elastic_premium", check_linux_premium
    yield "region_features", check_region_features
    if ctx.openai_enabled:
        yield "model_version", lambda c: check_model_versions(c, warn=False)
        yield "model_quota", check_quota
    if zones_required(ctx):
        yield "availability_zones", check_availability_zones
    yield "operator_allowlist", check_allowlist
    yield "federated_credential_region", check_identity_region
    yield "document_intelligence_layout", check_document_intelligence
    if ctx.env == "prod":
        yield "alert_receivers", check_alert_receivers
    if ctx.env == "load":
        yield "load_generator_capacity", check_load_generator
    yield "static_web_apps_custom_domains", check_custom_domains


def load_context(env: str) -> Context:
    config = json.loads((REPO_ROOT / "infra" / "env" / f"{env}.json").read_text(encoding="utf-8"))
    regions = json.loads(
        (REPO_ROOT / "infra" / "config" / "regions.json").read_text(encoding="utf-8")
    )
    if not isinstance(config, dict) or not isinstance(regions, dict):
        raise TypeError("an input file isn't a JSON object")
    return Context(env=env, config=config, regions=regions, today=today())


def run(env: str, mode: str) -> dict[str, Any]:
    try:
        ctx = load_context(env)
        location = ctx.location
    except (OSError, ValueError, TypeError, KeyError) as error:
        detail = f"inputs of {env} can't be read: {type(error).__name__}"
        return report_document(env, "", [Check("inputs", "fail", None, detail)])
    results: list[Check] = []
    for name, check in checks_for(ctx, mode):
        try:
            results.extend(check(ctx))
        except (KeyError, TypeError, ValueError) as error:
            # A malformed input fails its check instead of passing on missing data.
            results.append(Check(name, "fail", None, f"input error: {error!r}"))
    return report_document(env, location, results)


def report_document(env: str, location: str, checks: list[Check]) -> dict[str, Any]:
    return {
        "env": env,
        "location": location,
        "generatedAt": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": [check.to_json() for check in checks],
    }


def publish(report: dict[str, Any]) -> int:
    text = json.dumps(report, indent=2)
    sys.stdout.write(text + "\n")
    reports = REPO_ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"preflight-{report['env']}.json").write_text(text + "\n", encoding="utf-8")
    failures = [check for check in report["checks"] if check["status"] == "fail"]
    summary_lines = [f"### Preflight `{report['env']}` in `{report['location']}`", ""]
    summary_lines += ["| Check | Status | Code | Detail |", "|---|---|---|---|"]
    summary_lines += [
        f"| {c['name']} | {c['status']} | {c['code'] or ''} | {c['detail']} |"
        for c in report["checks"]
    ]
    if failures:
        first = failures[0]
        code = first["code"]
        message = MESSAGES.get(code, INPUTS_MESSAGE) if code else INPUTS_MESSAGE
        detail = f"{len(failures)} check(s) failed; first: {first['detail']}"[:DETAIL_LIMIT]
        line = json.dumps({"code": code, "message": message, "detail": detail})
        sys.stderr.write(line + "\n")
        summary_lines += ["", line]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The report file and the exit code carry the result; a summary write mustn't mask them.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(summary_lines) + "\n")
    return 2 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight.py", description=__doc__)
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--check", choices=("all", "models"), default="all")
    args = parser.parse_args(argv)
    return publish(run(args.env, args.check))


if __name__ == "__main__":
    sys.exit(main())
