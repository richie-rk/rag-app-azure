"""Unit tests of `scripts/preflight/preflight.py` (T-433, T-435's logic; section 3)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
TODAY = date(2026, 10, 30)
SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_script("scripts/preflight/preflight.py", "docchat_script_preflight")

# A copy of the content `83-infrastructure.md` section 3.5 requires of infra/config/regions.json.
REGIONS: dict[str, Any] = {
    "linuxPremiumMaxInstances": {
        "regions": {"eastus": 100, "eastus2": 100, "francecentral": 60, "australiaeast": 40},
        "otherRegions": 20,
        "absent": ["canadaeast", "southindia"],
    },
    "promptShieldsRegions": ["canadaeast", "eastus", "eastus2", "francecentral", "japaneast"],
    "semanticRankerRegions": {
        "msDate": "10/14/2026",
        "regions": ["canadaeast", "eastus", "eastus2", "francecentral"],
    },
    "documentIntelligenceLayoutRegions": {
        "msDate": "10/14/2026",
        "regions": ["canadaeast", "eastus", "eastus2", "francecentral", "japaneast"],
    },
    "githubPrivateNetworkingRegions": ["eastus", "eastus2"],
}


def _deployment(name: str, model: str, capacity: int) -> dict[str, Any]:
    return {
        "name": name,
        "model": model,
        "version": "2026-07-09",
        "type": "GlobalStandard",
        "capacity": capacity,
        "versionUpgradeOption": "OnceCurrentVersionExpired",
        "raiPolicyName": "docchat-text-guard",
        "encoding": "o200k_base",
    }


ENV_FILE: dict[str, Any] = {
    "subscriptionId": SUBSCRIPTION,
    "tenantId": "00000000-0000-4000-8000-000000000002",
    "location": "eastus2",
    "staticWebAppLocation": "eastus2",
    "webCustomDomains": [],
    "web": {"sku": "Free"},
    "api": {"sku": "B1", "zoneRedundant": False},
    "sql": {"zoneRedundant": False},
    "ingestion": {"sku": "EP1", "maxBurst": 2},
    "openAi": {
        "enabled": True,
        "deployments": {
            "chat": _deployment("chat", "gpt-5.6-sol", 30),
            "chatSmall": _deployment("chat-small", "gpt-5.6-terra", 30),
            "tableSummary": _deployment("ingest-summary", "gpt-5.6-terra", 30),
        },
        "embeddingModels": [
            {
                "key": "te3-large-v1",
                "model": "text-embedding-3-large",
                "version": "1",
                "queryType": "GlobalStandard",
                "ingestionType": "GlobalStandard",
                "queryCapacity": 30,
                "ingestionCapacity": 60,
            }
        ],
    },
    "modelStub": {"enabled": False},
}


def _model(
    name: str,
    version: str,
    *,
    skus: Sequence[str] = ("GlobalStandard",),
    status: str = "GenerallyAvailable",
    retires: date = date(2028, 1, 11),
) -> dict[str, Any]:
    stamp = f"{retires.isoformat()}T00:00:00Z"
    return {
        "kind": "OpenAI",
        "skuName": "S0",
        "model": {
            "format": "OpenAI",
            "name": name,
            "version": version,
            "lifecycleStatus": status,
            "deprecation": {"inference": stamp},
            "skus": [{"name": sku, "deprecationDate": stamp} for sku in skus],
        },
    }


class FakeAzure:
    """Recorded Resource Manager and CLI answers for one region, mutable per test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registration: dict[str, str] = {}
        self.failing: set[str] = set()
        self.type_regions: dict[str, list[str]] = {}
        self.sku_available: dict[str, bool] = {}
        self.geo_regions = ["East US 2", "East US"]
        self.models = [
            _model("gpt-5.6-sol", "2026-07-09"),
            _model("gpt-5.6-terra", "2026-07-09"),
            _model("text-embedding-3-large", "1"),
        ]
        self.available: dict[tuple[str, str], int] = {}
        self.capacity_regions = ["eastus2"]
        self.zones = True
        self.identity_region = "eastus2"
        self.accounts: list[dict[str, Any]] = []
        self.account_deployments: list[dict[str, Any]] = []
        self.container_cpu = 4.0
        self.container_memory = 16.0
        self.core_limit = 100
        self.cores_used = 0

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        joined = " ".join(words)
        self.calls.append(joined)
        for marker in self.failing:
            if marker in joined:
                return preflight.CommandResult(1, "", "ERROR: (AuthorizationFailed) refused")
        return preflight.CommandResult(0, json.dumps(self._answer(words, joined)), "")

    def _answer(self, words: list[str], joined: str) -> Any:
        routes: list[tuple[str, Callable[[list[str]], Any]]] = [
            ("az provider show", self._provider),
            ("checkSkuAvailability", self._sku),
            ("geoRegions", lambda w: {"value": [{"name": n} for n in self.geo_regions]}),
            ("/models?", lambda w: {"value": self.models}),
            ("modelCapacities", self._capacity),
            ("/locations?api-version", self._locations),
            ("az group show", lambda w: self.identity_region),
            ("az cognitiveservices account list", lambda w: self.accounts),
            ("account deployment list", lambda w: self.account_deployments),
            ("/capabilities?", self._capabilities),
            ("/usages?", self._usages),
        ]
        for marker, answer in routes:
            if marker in joined:
                return answer(words)
        raise AssertionError(f"unexpected command {joined}")

    def _provider(self, words: list[str]) -> Any:
        namespace = words[words.index("--namespace") + 1]
        types = [
            {"resourceType": full.split("/", 1)[1], "locations": regions}
            for full, regions in self._regions_by_type().items()
            if full.startswith(namespace + "/")
        ]
        state = self.registration.get(namespace, "Registered")
        return {"namespace": namespace, "registrationState": state, "resourceTypes": types}

    def _regions_by_type(self) -> dict[str, list[str]]:
        default = ["East US 2", "East US", "Central US"]
        names = [*preflight.REGIONAL_TYPES, preflight.STATIC_SITES_TYPE]
        return {name: self.type_regions.get(name, default) for name in names}

    def _sku(self, words: list[str]) -> Any:
        body = json.loads(words[words.index("--body") + 1])
        kind = body["kind"]
        available = self.sku_available.get(kind, True)
        return {"value": [{"kind": kind, "skuName": "S0", "skuAvailable": available}]}

    def _capacity(self, words: list[str]) -> Any:
        url = words[words.index("--url") + 1]
        offers = [
            (sku, units)
            for (model, sku), units in self.available.items()
            if f"modelName={model}&" in url
        ]
        values = [
            {"location": region, "properties": {"skuName": sku, "availableCapacity": units}}
            for region in self.capacity_regions
            for sku, units in offers or [("GlobalStandard", 1000)]
        ]
        return {"value": values}

    def _locations(self, words: list[str]) -> Any:
        mappings = [{"logicalZone": "1"}] if self.zones else []
        return {"value": [{"name": "eastus2", "availabilityZoneMappings": mappings}]}

    def _capabilities(self, words: list[str]) -> Any:
        return {
            "value": [
                {
                    "resourceType": "containerGroups",
                    "osType": "Linux",
                    "gpu": "None",
                    "capabilities": {
                        "maxCpu": self.container_cpu,
                        "maxMemoryInGB": self.container_memory,
                    },
                }
            ]
        }

    def _usages(self, words: list[str]) -> Any:
        return {
            "value": [
                {
                    "name": {"value": "StandardCores"},
                    "limit": self.core_limit,
                    "currentValue": self.cores_used,
                }
            ]
        }


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> FakeAzure:
    fake = FakeAzure()
    monkeypatch.setattr(preflight, "run_command", fake)
    monkeypatch.setattr(preflight, "today", lambda: TODAY)
    return fake


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / "infra" / "env").mkdir(parents=True)
    (tmp_path / "infra" / "config").mkdir(parents=True)
    (tmp_path / "infra" / "config" / "regions.json").write_text(json.dumps(REGIONS))
    for env in ("dev", "prod", "load"):
        _write_env(tmp_path, env, ENV_FILE)
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DOCCHAT_OPERATOR_IP_ALLOWLIST", "203.0.113.10")
    monkeypatch.setenv("DOCCHAT_OPS_ALERT_EMAILS", "ops@contoso.example")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return tmp_path


def _write_env(root: Path, env: str, content: dict[str, Any]) -> None:
    data = copy.deepcopy(content)
    if env == "load":
        data["openAi"]["enabled"] = False
        data["modelStub"]["enabled"] = True
    (root / "infra" / "env" / f"{env}.json").write_text(json.dumps(data))


def _edit_env(root: Path, env: str, change: Callable[[dict[str, Any]], None]) -> None:
    data = copy.deepcopy(ENV_FILE)
    change(data)
    _write_env(root, env, data)


def _run(env: str = "dev", check: str = "all") -> tuple[int, dict[str, Any]]:
    status = preflight.main(["--env", env, "--check", check])
    report: dict[str, Any] = json.loads(
        (preflight.REPO_ROOT / "reports" / f"preflight-{env}.json").read_text()
    )
    return status, report


def _codes(report: dict[str, Any]) -> list[str]:
    return [c["code"] for c in report["checks"] if c["status"] == "fail"]


def _names(report: dict[str, Any]) -> set[str]:
    return {c["name"] for c in report["checks"]}


# --- the whole run --------------------------------------------------------------------------


def test_t433_dev_passes_with_the_shipped_shape_of_inputs(
    repo: Path, azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    status, report = _run("dev")

    assert status == 0
    assert _codes(report) == []
    assert report["env"] == "dev"
    assert report["location"] == "eastus2"
    assert report["generatedAt"].endswith("Z")
    assert json.loads(capsys.readouterr().out) == report
    # Check 8 runs in dev only for a zone-redundant parameter; 12 only in prod; 13 only in load.
    assert "availability_zones" not in _names(report)
    assert "alert_receivers" not in _names(report)
    assert "load_generator_capacity" not in _names(report)
    for check in report["checks"]:
        assert set(check) == {"name", "status", "code", "detail"}
        assert len(check["detail"]) <= 500


def test_t433_a_failure_exits_2_with_its_json_line(
    repo: Path,
    azure: FakeAzure,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = repo / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    azure.registration["Microsoft.Security"] = "NotRegistered"

    status, report = _run("dev")

    assert status == 2
    assert _codes(report) == ["provider_not_registered"]
    line = json.loads(capsys.readouterr().err.strip())
    assert line["code"] == "provider_not_registered"
    assert line["message"] == preflight.MESSAGES["provider_not_registered"]
    assert "provider_not_registered" in summary.read_text()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda a: a.registration.update({"Microsoft.ContainerInstance": "NotRegistered"}),
            ["provider_not_registered"],
        ),
        (
            lambda a: a.failing.add("--namespace Microsoft.Web "),
            ["provider_not_registered", "service_not_in_region"],
        ),
        (
            lambda a: a.type_regions.update({"Microsoft.Network/privateEndpoints": ["West US"]}),
            ["service_not_in_region"],
        ),
        (
            lambda a: a.type_regions.update({"Microsoft.Web/staticSites": ["West US 2"]}),
            ["service_not_in_region"],
        ),
        (lambda a: a.sku_available.update({"ContentSafety": False}), ["ai_sku_unavailable"]),
        (lambda a: a.failing.add("checkSkuAvailability"), ["ai_sku_unavailable"]),
        (lambda a: setattr(a, "geo_regions", ["West Europe"]), ["linux_premium_unavailable"]),
        (lambda a: a.failing.add("geoRegions"), ["linux_premium_unavailable"]),
        (lambda a: setattr(a, "identity_region", "malaysiasouth"), ["fic_region_unsupported"]),
        (lambda a: a.failing.add("rg-docchat-identity"), ["fic_region_unsupported"]),
    ],
    ids=[
        "provider-not-registered",
        "provider-call-fails",
        "private-endpoints-not-in-region",
        "static-site-region",
        "content-safety-sku",
        "sku-call-fails",
        "no-linux-premium",
        "geo-regions-call-fails",
        "identity-in-malaysiasouth",
        "identity-call-fails",
    ],
)
def test_t433_each_code_exactly_when_its_azure_condition_holds(
    repo: Path, azure: FakeAzure, change: Callable[[FakeAzure], None], expected: list[str]
) -> None:
    change(azure)

    status, report = _run("dev")

    assert status == 2
    assert _codes(report) == expected


def test_t433_a_failed_call_is_named_in_the_detail(repo: Path, azure: FakeAzure) -> None:
    azure.failing.add("/models?")

    _, report = _run("dev")

    failures = [c for c in report["checks"] if c["status"] == "fail"]
    assert [c["code"] for c in failures] == ["model_version_not_offered"]
    assert "models list failed" in failures[0]["detail"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda e: e["ingestion"].update({"maxBurst": 101}), ["linux_premium_unavailable"]),
        (lambda e: e.update({"location": "japaneast"}), ["semantic_ranker_unavailable"]),
        (
            lambda e: e.update({"location": "australiaeast"}),
            [
                "prompt_shields_unavailable",
                "semantic_ranker_unavailable",
                "document_intelligence_unavailable",
            ],
        ),
        (lambda e: e.update({"location": "canadaeast"}), ["linux_premium_unavailable"]),
        (lambda e: e["sql"].update({"zoneRedundant": True}), []),
    ],
    ids=[
        "burst-above-maximum",
        "no-semantic-ranker",
        "no-ai-features",
        "region-without-a-linux-maximum",
        "sql-zone-redundant",
    ],
)
def test_t433_region_facts_from_regions_json(
    repo: Path,
    azure: FakeAzure,
    change: Callable[[dict[str, Any]], None],
    expected: list[str],
) -> None:
    others = ["Japan East", "Australia East", "Canada East"]
    azure.geo_regions.extend(others)
    for name in [*preflight.REGIONAL_TYPES, preflight.STATIC_SITES_TYPE]:
        azure.type_regions[name] = ["East US 2", *others]
    azure.capacity_regions = ["eastus2", "japaneast", "australiaeast", "canadaeast"]
    _edit_env(repo, "dev", change)

    _, report = _run("dev")

    assert _codes(report) == expected


def test_t433_custom_domains_against_the_plan(repo: Path, azure: FakeAzure) -> None:
    three = ["a.example", "b.example", "c.example"]
    _edit_env(repo, "dev", lambda e: e.update({"webCustomDomains": three}))
    assert _codes(_run("dev")[1]) == ["custom_domains_exceed_plan"]

    five = [*three, "d.example", "e.example"]
    _edit_env(
        repo, "dev", lambda e: e.update({"webCustomDomains": five, "web": {"sku": "Standard"}})
    )
    assert _codes(_run("dev")[1]) == []

    _edit_env(
        repo,
        "dev",
        lambda e: e.update({"webCustomDomains": [*five, "f.example"], "web": {"sku": "Standard"}}),
    )
    assert _codes(_run("dev")[1]) == ["custom_domains_exceed_plan"]


# --- check 6: model versions per deployment type --------------------------------------------


@pytest.mark.parametrize(
    ("days", "code"),
    [(182, "model_retires_soon"), (183, None), (184, None)],
)
def test_t433_retirement_182_days_ahead_fails_and_184_passes(
    repo: Path, azure: FakeAzure, days: int, code: str | None
) -> None:
    azure.models[0] = _model("gpt-5.6-sol", "2026-07-09", retires=TODAY + timedelta(days=days))

    status, report = _run("dev")

    assert _codes(report) == ([code] if code else [])
    assert status == (2 if code else 0)


def test_t433_sku_deprecation_date_counts_as_well(repo: Path, azure: FakeAzure) -> None:
    entry = _model("gpt-5.6-terra", "2026-07-09")
    entry["model"]["skus"][0]["deprecationDate"] = (TODAY + timedelta(days=100)).isoformat()
    azure.models[1] = entry

    _, report = _run("dev")

    # Two deployments (chat-small and ingest-summary) pin the same model.
    assert _codes(report) == ["model_retires_soon", "model_retires_soon"]


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (
            [_model("gpt-5.6-sol", "2026-06-25"), _model("gpt-5.6-terra", "2026-07-09")],
            ["model_version_not_offered"],
        ),
        (
            [
                _model("gpt-5.6-sol", "2026-07-09", status="Deprecated"),
                _model("gpt-5.6-terra", "2026-07-09"),
            ],
            ["model_retired"],
        ),
        (
            [
                _model("gpt-5.6-sol", "2026-07-09", status="Deprecating"),
                _model("gpt-5.6-terra", "2026-07-09"),
            ],
            ["model_deprecated"],
        ),
        (
            [
                _model("gpt-5.6-sol", "2026-07-09", skus=("DataZoneStandard",)),
                _model("gpt-5.6-terra", "2026-07-09"),
            ],
            ["model_sku_not_offered"],
        ),
    ],
    ids=["version-not-offered", "retired", "deprecating", "sku-not-offered"],
)
def test_t433_model_lifecycle_codes(
    repo: Path, azure: FakeAzure, models: list[dict[str, Any]], expected: list[str]
) -> None:
    azure.models = [*models, _model("text-embedding-3-large", "1")]

    _, report = _run("dev")

    assert _codes(report) == expected


def test_t433_deprecating_version_passes_when_the_account_already_holds_it(
    repo: Path, azure: FakeAzure
) -> None:
    azure.models[0] = _model("gpt-5.6-sol", "2026-07-09", status="Deprecating")
    azure.accounts = [{"name": "oai-docchat-dev-abc123", "kind": "OpenAI"}]
    azure.account_deployments = [
        {
            "name": "chat",
            "properties": {
                "model": {"format": "OpenAI", "name": "gpt-5.6-sol", "version": "2026-07-09"}
            },
            "sku": {"name": "GlobalStandard", "capacity": 30},
        }
    ]

    _, report = _run("dev")

    assert _codes(report) == []


def test_t433_embedding_deployments_are_checked_against_their_own_types(
    repo: Path, azure: FakeAzure
) -> None:
    def query_on_data_zone(env: dict[str, Any]) -> None:
        env["openAi"]["embeddingModels"][0]["queryType"] = "DataZoneStandard"

    _edit_env(repo, "dev", query_on_data_zone)

    _, report = _run("dev")

    failures = [c for c in report["checks"] if c["status"] == "fail"]
    # Without the offer there is no Data Zone quota entry for the query deployment either.
    assert [c["code"] for c in failures] == ["model_sku_not_offered", "quota_insufficient"]
    assert failures[0]["detail"].startswith("embed-query-te3-large-v1:")
    assert "(DataZoneStandard)" in failures[1]["detail"]

    azure.models[2] = _model(
        "text-embedding-3-large", "1", skus=("GlobalStandard", "DataZoneStandard")
    )
    azure.available[("text-embedding-3-large", "GlobalStandard")] = 1000
    azure.available[("text-embedding-3-large", "DataZoneStandard")] = 1000
    assert _codes(_run("dev")[1]) == []


# --- check 7: quota -------------------------------------------------------------------------


def test_t433_capacities_of_the_same_model_and_type_are_summed(
    repo: Path, azure: FakeAzure
) -> None:
    # chat-small (30) and ingest-summary (30) share gpt-5.6-terra Global Standard.
    azure.available[("gpt-5.6-terra", "GlobalStandard")] = 59

    _, report = _run("dev")

    failures = [c for c in report["checks"] if c["status"] == "fail"]
    assert [c["code"] for c in failures] == ["quota_insufficient"]
    assert "short by 1 units" in failures[0]["detail"]

    azure.available[("gpt-5.6-terra", "GlobalStandard")] = 60
    assert _codes(_run("dev")[1]) == []


def test_t433_existing_same_named_deployments_are_subtracted(repo: Path, azure: FakeAzure) -> None:
    azure.available[("gpt-5.6-terra", "GlobalStandard")] = 30
    azure.accounts = [{"name": "oai-docchat-dev-abc123", "kind": "OpenAI"}]
    azure.account_deployments = [
        {
            "name": "chat-small",
            "properties": {"model": {"name": "gpt-5.6-terra", "version": "2026-07-09"}},
            "sku": {"name": "GlobalStandard", "capacity": 30},
        }
    ]

    _, report = _run("dev")

    assert _codes(report) == []


def test_t433_missing_capacity_entry_is_insufficient(repo: Path, azure: FakeAzure) -> None:
    azure.available[("gpt-5.6-sol", "Standard")] = 1000

    _, report = _run("dev")

    assert _codes(report) == ["quota_insufficient"]


# --- checks 8, 9, 12 and the load checks ----------------------------------------------------


def test_t433_prod_needs_zones_and_alert_receivers(
    repo: Path, azure: FakeAzure, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, report = _run("prod")
    assert {"availability_zones", "alert_receivers"} <= _names(report)
    assert _codes(report) == []

    azure.zones = False
    monkeypatch.setenv("DOCCHAT_OPS_ALERT_EMAILS", " , ")
    _, report = _run("prod")
    assert _codes(report) == ["availability_zones_unavailable", "alert_receivers_missing"]


def test_t433_dev_checks_zones_only_for_a_zone_redundant_parameter(
    repo: Path, azure: FakeAzure
) -> None:
    azure.zones = False
    _edit_env(repo, "dev", lambda e: e["api"].update({"zoneRedundant": True}))

    _, report = _run("dev")

    assert _codes(report) == ["availability_zones_unavailable"]


@pytest.mark.parametrize(
    ("allowlist", "valid"),
    [
        ("", True),
        ("203.0.113.10,198.51.100.4", True),
        (",".join(f"203.0.113.{n}" for n in range(1, 21)), True),
        (",".join(f"203.0.113.{n}" for n in range(1, 22)), False),
        ("203.0.113.0/24", False),
        ("203.0.113.10/32", False),
        ("10.1.2.3", False),
        ("172.20.0.1", False),
        ("192.168.0.10", False),
        ("100.100.0.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("2001:db8::1", False),
        ("ops.example", False),
    ],
)
def test_t433_operator_allowlist(
    repo: Path, azure: FakeAzure, monkeypatch: pytest.MonkeyPatch, allowlist: str, valid: bool
) -> None:
    monkeypatch.setenv("DOCCHAT_OPERATOR_IP_ALLOWLIST", allowlist)

    _, report = _run("dev")

    assert _codes(report) == ([] if valid else ["allowlist_invalid"])
    if not valid:
        detail = next(c["detail"] for c in report["checks"] if c["status"] == "fail")
        assert "203.0.113" not in detail


def test_t433_load_checks_the_container_capacity_and_skips_the_model_checks(
    repo: Path, azure: FakeAzure
) -> None:
    _, report = _run("load")
    assert "load_generator_capacity" in _names(report)
    assert not {"model_version", "model_quota"} & _names(report)
    assert _codes(report) == []
    assert not any("/models?" in call or "modelCapacities" in call for call in azure.calls)

    azure.container_cpu = 2.0
    assert _codes(_run("load")[1]) == ["container_capacity_unavailable"]

    azure.container_cpu = 4.0
    azure.cores_used = 97
    assert _codes(_run("load")[1]) == ["container_capacity_unavailable"]


# --- --check models and inputs --------------------------------------------------------------


def test_t433_models_mode_runs_check_6_alone_and_warns_within_274_days(
    repo: Path, azure: FakeAzure
) -> None:
    azure.models[0] = _model("gpt-5.6-sol", "2026-07-09", retires=TODAY + timedelta(days=270))

    status, report = _run("dev", "models")

    assert status == 0
    assert _names(report) == {"model_version"}
    warnings = [c for c in report["checks"] if c["status"] == "warn"]
    assert len(warnings) == 1
    assert warnings[0]["code"] is None
    assert warnings[0]["detail"].startswith("chat: gpt-5.6-sol 2026-07-09")
    assert not any("provider show" in call for call in azure.calls)

    azure.models[0] = _model("gpt-5.6-sol", "2026-07-09", retires=TODAY + timedelta(days=182))
    status, report = _run("dev", "models")
    assert status == 2
    assert _codes(report) == ["model_retires_soon"]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_t435_the_model_watch_checks_every_shipped_deployment(
    repo: Path, azure: FakeAzure, env: str
) -> None:
    # The nightly model watch reads the committed files, so the chat path's chat, chat-small and
    # embed-query deployments are checked beside the ingestion ones, each against its own type.
    shipped = json.loads((REPO_ROOT / "infra" / "env" / f"{env}.json").read_text("utf-8"))
    (repo / "infra" / "env" / f"{env}.json").write_text(json.dumps(shipped), encoding="utf-8")

    status, report = _run(env, "models")

    assert status == 0
    assert [c["detail"].split(" (", 1)[0] for c in report["checks"]] == [
        "chat: gpt-5.6-sol 2026-07-09",
        "chat-small: gpt-5.6-terra 2026-07-09",
        "ingest-summary: gpt-5.6-terra 2026-07-09",
        "embed-query-te3-large-v1: text-embedding-3-large 1",
        "embed-ingest-te3-large-v1: text-embedding-3-large 1",
    ]
    assert all(
        c["detail"].split(" (", 1)[1].startswith("GlobalStandard)") for c in report["checks"]
    )


def test_full_run_gives_no_retirement_warning(repo: Path, azure: FakeAzure) -> None:
    azure.models[0] = _model("gpt-5.6-sol", "2026-07-09", retires=TODAY + timedelta(days=270))

    _, report = _run("dev")

    assert all(c["status"] == "pass" for c in report["checks"])


def test_an_unreadable_environment_file_fails_without_checks(repo: Path, azure: FakeAzure) -> None:
    (repo / "infra" / "env" / "dev.json").write_text("{not json")

    status, report = _run("dev")

    assert status == 2
    assert [c["name"] for c in report["checks"]] == ["inputs"]
    assert azure.calls == []


def test_linux_premium_maximum_reads_named_other_and_absent_regions() -> None:
    assert preflight.linux_premium_maximum(REGIONS, "eastus2") == 100
    assert preflight.linux_premium_maximum(REGIONS, "japaneast") == 20
    assert preflight.linux_premium_maximum(REGIONS, "canadaeast") is None


def test_the_committed_regions_file_has_the_section_3_5_linux_maxima() -> None:
    committed = json.loads(
        (Path(__file__).parents[4] / "infra" / "config" / "regions.json").read_text("utf-8")
    )
    assert preflight.linux_premium_maximum(committed, "eastus2") == 100
    assert preflight.linux_premium_maximum(committed, "francecentral") == 60
    assert preflight.linux_premium_maximum(committed, "australiaeast") == 40
    assert preflight.linux_premium_maximum(committed, "japaneast") == 20
    assert preflight.linux_premium_maximum(committed, "southindia") is None
