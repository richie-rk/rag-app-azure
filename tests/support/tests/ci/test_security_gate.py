"""T-527: the security gate unit tests (84-testing.md section 9, ADR 0136)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import security_gate  # noqa: E402

TODAY = datetime.now(tz=UTC).date()
APPROVERS = (
    "# docchat-security, kept equal to the team by runbook R16\napprovers: [sec-lead, sec-two]\n"
)
CLEAN_NPM = {"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {"vulnerabilities": {}}}
CLEAN_TRIVY = {
    "SchemaVersion": 2,
    "ArtifactName": "docchat/api:local",
    "Results": [{"Target": "docchat/api (debian 12.12)", "Class": "os-pkgs", "Type": "debian"}],
}


def waiver(**overrides: str) -> dict[str, str]:
    values = {
        "id": "1096460",
        "scanner": "npm-audit",
        "scope": "postcss",
        "reason": "Build-time only; the dev server is never exposed to users.",
        "ticket": "https://github.com/octo/docchat/issues/12",
        "added": (TODAY - timedelta(days=10)).isoformat(),
        "expires": (TODAY + timedelta(days=60)).isoformat(),
        "approved_by": "sec-lead",
    }
    values.update(overrides)
    return values


def waivers_yaml(entries: list[dict[str, str]], key: str = "waivers") -> str:
    if not entries:
        return f"{key}: []\n"
    lines = [f"{key}:"]
    for entry in entries:
        first = True
        for name, value in entry.items():
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{name}: {json.dumps(value)}")
            first = False
    return "\n".join(lines) + "\n"


def exception(**overrides: str) -> dict[str, str]:
    """An entry of exceptions.yaml: the one exception the gate allows, with valid dates."""
    values = {
        "id": "DS002",
        "image": "ingestion",
        "reason": "The parsing child drops to a pool user; the host keeps UID 0 for that switch.",
        "decision": "ADR 0153",
        "added": (TODAY - timedelta(days=10)).isoformat(),
        "review_by": (TODAY + timedelta(days=170)).isoformat(),
    }
    values.update(overrides)
    return values


def misconfiguration(target: str, check: str = "DS002", severity: str = "HIGH") -> dict[str, Any]:
    """A Trivy report holding one failed Dockerfile check on `target`."""
    return {
        "SchemaVersion": 2,
        "Results": [
            {
                "Target": target,
                "Class": "config",
                "Type": "dockerfile",
                "Misconfigurations": [
                    {
                        "Type": "Dockerfile Security Check",
                        "ID": check,
                        "AVDID": f"AVD-DS-{check[2:].zfill(4)}",
                        "Title": "Image user should not be 'root'",
                        "Severity": severity,
                        "Status": "FAIL",
                    }
                ],
            }
        ],
    }


def npm_advisory(source: int, package: str, severity: str) -> dict[str, Any]:
    """An npm audit 2 report holding one advisory (`via` objects carry the advisory)."""
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            package: {
                "name": package,
                "severity": severity,
                "isDirect": False,
                "via": [
                    {
                        "source": source,
                        "name": package,
                        "dependency": package,
                        "title": "Advisory title",
                        "url": "https://github.com/advisories/GHSA-7fh5-64p2-3v2j",
                        "severity": severity,
                        "range": "<8.4.31",
                    }
                ],
                "effects": ["vite"],
                "range": "<8.4.31",
                "fixAvailable": True,
            },
            "vite": {"name": "vite", "severity": severity, "via": [package], "effects": []},
        },
        "metadata": {"vulnerabilities": {severity: 2}},
    }


def pip_audit(package: str, vulnerability_id: str) -> dict[str, Any]:
    return {
        "dependencies": [
            {"name": "fastapi", "version": "0.143.1", "vulns": []},
            {
                "name": package,
                "version": "3.1.0",
                "vulns": [
                    {
                        "id": vulnerability_id,
                        "fix_versions": ["3.1.6"],
                        "aliases": ["CVE-2026-1111"],
                        "description": "...",
                    }
                ],
            },
        ],
        "fixes": [],
    }


class Repository:
    """A fixture repository root with security files and clean reports for every job."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/core", "tests/model-stub"]\n',
            encoding="utf-8",
        )
        self.security = root / ".github" / "security"
        self.security.mkdir(parents=True)
        self.audit = root / "build" / "audit"
        self.audit.mkdir(parents=True)
        self.write_security(APPROVERS, [])
        for source in (
            "apps/web/package.json",
            "tests/e2e/package.json",
            "scripts/deploy/swa-cli/package.json",
            "apps/api/Dockerfile",
            "apps/ingestion/Dockerfile",
            "tests/model-stub/Dockerfile",
        ):
            (root / source).parent.mkdir(parents=True, exist_ok=True)
            (root / source).write_text("", encoding="utf-8")
        self.report("pip-audit.json", {"dependencies": [], "fixes": []})
        for directory in ("web", "e2e", "swa-cli"):
            self.report(f"npm-{directory}.json", CLEAN_NPM)
        self.report("zizmor.json", [])
        for member in ("core", "model-stub"):
            self.report(f"deptry-{member}.json", [])
        for image in ("api", "ingestion", "model-stub"):
            self.report(f"trivy-{image}.json", CLEAN_TRIVY)
        self.report("trivy-config.json", {"SchemaVersion": 2, "Results": []})

    def write_security(
        self,
        approvers: str,
        waivers: list[dict[str, str]],
        exceptions: list[dict[str, str]] | None = None,
    ) -> None:
        (self.security / "approvers.yaml").write_text(approvers, encoding="utf-8")
        (self.security / "waivers.yaml").write_text(waivers_yaml(waivers), encoding="utf-8")
        self.write_exceptions(exceptions or [])

    def write_exceptions(self, exceptions: list[dict[str, str]]) -> None:
        text = waivers_yaml(exceptions, key="exceptions")
        (self.security / "exceptions.yaml").write_text(text, encoding="utf-8")

    def report(self, name: str, content: Any) -> None:
        (self.audit / name).write_text(json.dumps(content), encoding="utf-8")


class FakeOsv:
    """Recorded OSV records; an identifier in `unreachable` fails like a network error."""

    def __init__(
        self, records: dict[str, dict[str, Any]], unreachable: frozenset[str] = frozenset()
    ):
        self.records = records
        self.unreachable = unreachable
        self.requested: list[str] = []

    def __call__(self, vulnerability_id: str) -> dict[str, Any] | None:
        self.requested.append(vulnerability_id)
        if vulnerability_id in self.unreachable:
            raise security_gate.OsvUnavailableError(vulnerability_id)
        return self.records.get(vulnerability_id)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(security_gate, "fetch_osv_record", FakeOsv({}))
    return Repository(tmp_path)


def gate(repository: Repository, *arguments: str) -> int:
    return security_gate.main(["--root", str(repository.root), *arguments])


def test_t527_clean_reports_pass_for_every_job(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    for job in ("security", "images", "build"):
        assert gate(repository, "--job", job) == 0
    assert "**Result: pass**" in capsys.readouterr().out


def test_t527_a_high_trivy_finding_fails_even_when_a_waiver_lists_it(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    report = json.loads(json.dumps(CLEAN_TRIVY))
    report["Results"][0]["Vulnerabilities"] = [
        {"VulnerabilityID": "CVE-2026-0001", "PkgName": "libssl3", "Severity": "HIGH"}
    ]
    repository.report("trivy-api.json", report)
    repository.write_security(
        APPROVERS, [waiver(id="CVE-2026-0001", scanner="trivy", scope="libssl3")]
    )

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert (
        "| trivy | CVE-2026-0001 | libssl3 | HIGH | fails: high and critical findings can't be "
        "waived |"
    ) in output
    assert "scanner must be one of pip-audit, npm-audit, zizmor, deptry" in output


def test_t527_a_waiver_naming_a_high_finding_is_not_applicable(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    osv = FakeOsv({"GHSA-q2x7-8rv6-6q7h": {"database_specific": {"severity": "HIGH"}}})
    monkeypatch.setattr(security_gate, "fetch_osv_record", osv)
    repository.report("pip-audit.json", pip_audit("jinja2", "GHSA-q2x7-8rv6-6q7h"))
    repository.write_security(
        APPROVERS, [waiver(id="GHSA-q2x7-8rv6-6q7h", scanner="pip-audit", scope="jinja2")]
    )

    assert gate(repository) == 1
    output = capsys.readouterr().out
    assert "not applicable: high and critical findings can't be waived" in output
    assert "| pip-audit | GHSA-q2x7-8rv6-6q7h | jinja2 | HIGH | fails:" in output


def test_t527_a_waived_moderate_npm_finding_passes(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("npm-web.json", npm_advisory(1096460, "postcss", "moderate"))
    repository.write_security(APPROVERS, [waiver()])

    assert gate(repository) == 0
    output = capsys.readouterr().out
    assert "| npm-audit | 1096460 | postcss | moderate | waived by 1096460 |" in output
    assert "applies to 1096460" in output


def test_an_unwaived_moderate_npm_finding_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("npm-e2e.json", npm_advisory(1096460, "postcss", "moderate"))

    assert gate(repository) == 1
    assert "| npm-audit | 1096460 | postcss | moderate | fails: no waiver |" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_a_high_npm_finding_fails_with_a_waiver(
    repository: Repository, severity: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("npm-web.json", npm_advisory(1096460, "postcss", severity))
    repository.write_security(APPROVERS, [waiver()])

    assert gate(repository) == 1
    assert "can't be waived" in capsys.readouterr().out


def test_a_waiver_may_name_the_npm_advisory_by_its_ghsa_identifier(
    repository: Repository,
) -> None:
    repository.report("npm-swa-cli.json", npm_advisory(1096460, "postcss", "low"))
    repository.write_security(APPROVERS, [waiver(id="GHSA-7fh5-64p2-3v2j")])

    assert gate(repository) == 0


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        (
            {
                "added": (TODAY - timedelta(days=40)).isoformat(),
                "expires": (TODAY - timedelta(days=1)).isoformat(),
            },
            "expired on",
        ),
        (
            {
                "added": (TODAY - timedelta(days=5)).isoformat(),
                "expires": (TODAY + timedelta(days=86)).isoformat(),
            },
            "expires is more than 90 days after added",
        ),
        ({"reason": "Too short a reason"}, "reason is shorter than 20 characters"),
        ({"approved_by": "dev-one"}, "approved_by dev-one isn't listed in approvers.yaml"),
        ({"ticket": "JIRA-12"}, "ticket must be an https URL"),
        ({"scanner": "gitleaks"}, "scanner must be one of"),
        ({"added": "30/10/2026"}, "added and expires must be dates written YYYY-MM-DD"),
    ],
)
def test_t527_an_invalid_waiver_fails_the_gate(
    repository: Repository,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
    problem: str,
) -> None:
    repository.report("npm-web.json", npm_advisory(1096460, "postcss", "moderate"))
    repository.write_security(APPROVERS, [waiver(**overrides)])

    assert gate(repository) == 1
    output = capsys.readouterr().out
    assert problem in output
    assert "::error::waiver 1096460:" in output


def test_an_invalid_waiver_fails_even_when_no_finding_uses_it(repository: Repository) -> None:
    repository.write_security(APPROVERS, [waiver(reason="short")])

    assert gate(repository) == 1


def test_a_waiver_on_its_ninetieth_day_and_on_its_expiry_date_is_valid(
    repository: Repository,
) -> None:
    added = TODAY - timedelta(days=90)
    repository.report("npm-web.json", npm_advisory(1096460, "postcss", "moderate"))
    repository.write_security(
        APPROVERS, [waiver(added=added.isoformat(), expires=TODAY.isoformat())]
    )

    assert gate(repository) == 0


def test_t527_a_pysec_finding_is_rated_from_its_ghsa_alias(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    osv = FakeOsv(
        {
            "PYSEC-2026-17": {
                "id": "PYSEC-2026-17",
                "aliases": ["CVE-2026-1111", "GHSA-h5c8-rqwp-cp95"],
                "database_specific": {},
            },
            "GHSA-h5c8-rqwp-cp95": {"database_specific": {"severity": "MODERATE"}},
        }
    )
    monkeypatch.setattr(security_gate, "fetch_osv_record", osv)
    repository.report("pip-audit.json", pip_audit("Jinja2", "PYSEC-2026-17"))
    repository.write_security(
        APPROVERS, [waiver(id="PYSEC-2026-17", scanner="pip-audit", scope="jinja2")]
    )

    assert gate(repository) == 0
    assert osv.requested == ["PYSEC-2026-17", "GHSA-h5c8-rqwp-cp95"]
    assert "| pip-audit | PYSEC-2026-17 | jinja2 | MODERATE | waived by PYSEC-2026-17 |" in (
        capsys.readouterr().out
    )


def test_the_highest_alias_rating_wins(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    osv = FakeOsv(
        {
            "PYSEC-2026-18": {"aliases": ["GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"]},
            "GHSA-aaaa-bbbb-cccc": {"database_specific": {"severity": "LOW"}},
            "GHSA-dddd-eeee-ffff": {"database_specific": {"severity": "CRITICAL"}},
        }
    )
    monkeypatch.setattr(security_gate, "fetch_osv_record", osv)
    repository.report("pip-audit.json", pip_audit("jinja2", "PYSEC-2026-18"))
    repository.write_security(
        APPROVERS, [waiver(id="PYSEC-2026-18", scanner="pip-audit", scope="jinja2")]
    )

    assert gate(repository) == 1


@pytest.mark.parametrize(
    "osv",
    [
        FakeOsv({}),
        FakeOsv({"PYSEC-2026-19": {"aliases": ["CVE-2026-2"]}}),
        FakeOsv({}, unreachable=frozenset({"PYSEC-2026-19"})),
    ],
    ids=["no record", "no rating", "unreachable"],
)
def test_t527_a_finding_no_record_rates_fails_even_when_waived(
    repository: Repository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    osv: FakeOsv,
) -> None:
    monkeypatch.setattr(security_gate, "fetch_osv_record", osv)
    repository.report("pip-audit.json", pip_audit("jinja2", "PYSEC-2026-19"))
    repository.write_security(
        APPROVERS, [waiver(id="PYSEC-2026-19", scanner="pip-audit", scope="jinja2")]
    )

    assert gate(repository) == 1
    output = capsys.readouterr().out
    assert "| pip-audit | PYSEC-2026-19 | jinja2 | unrated | fails:" in output
    assert "not applicable" in output


@pytest.mark.parametrize(
    ("name", "job"),
    [
        ("zizmor.json", "security"),
        ("deptry-model-stub.json", "security"),
        ("npm-swa-cli.json", "security"),
        ("trivy-ingestion.json", "images"),
        ("trivy-config.json", "images"),
        ("trivy-model-stub.json", "build"),
    ],
)
def test_t527_a_missing_report_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str], name: str, job: str
) -> None:
    (repository.audit / name).unlink()

    assert gate(repository, "--job", job) == 1
    assert f"{name}: missing report" in capsys.readouterr().out


def test_t527_an_npm_error_object_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report(
        "npm-e2e.json", {"error": {"code": "ENOLOCK", "summary": "This command requires a lock"}}
    )

    assert gate(repository) == 1
    assert "npm-e2e.json: npm audit reported an error instead of a result" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("name", "content", "job"),
    [
        ("pip-audit.json", {"fixes": []}, "security"),
        ("npm-web.json", {"auditReportVersion": 1, "advisories": {}}, "security"),
        ("zizmor.json", {"findings": []}, "security"),
        ("deptry-core.json", {}, "security"),
        ("trivy-api.json", {"SchemaVersion": 2}, "images"),
        ("trivy-config.json", {"Results": []}, "images"),
    ],
)
def test_a_report_of_the_wrong_shape_fails(
    repository: Repository, name: str, content: Any, job: str
) -> None:
    repository.report(name, content)

    assert gate(repository, "--job", job) == 1


def test_a_report_that_is_not_json_fails(repository: Repository) -> None:
    (repository.audit / "zizmor.json").write_text("not json", encoding="utf-8")

    assert gate(repository) == 1


def test_trivy_secrets_and_high_misconfigurations_fail(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    report = json.loads(json.dumps(CLEAN_TRIVY))
    report["Results"][0]["Secrets"] = [{"RuleID": "aws-access-key-id", "Severity": "CRITICAL"}]
    repository.report("trivy-api.json", report)
    repository.report(
        "trivy-config.json",
        {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "apps/api/Dockerfile",
                    "Misconfigurations": [
                        {"ID": "DS002", "Severity": "HIGH", "Status": "FAIL"},
                        {"ID": "DS026", "Severity": "LOW", "Status": "FAIL"},
                    ],
                }
            ],
        },
    )

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert "| trivy | aws-access-key-id |" in output
    assert "| trivy | DS002 | apps/api/Dockerfile | HIGH |" in output
    assert "DS026" not in output


def test_trivy_findings_below_high_are_not_gated(repository: Repository) -> None:
    report = json.loads(json.dumps(CLEAN_TRIVY))
    report["Results"][0]["Vulnerabilities"] = [
        {"VulnerabilityID": "CVE-2026-0002", "PkgName": "zlib1g", "Severity": "MEDIUM"}
    ]
    repository.report("trivy-api.json", report)

    assert gate(repository, "--job", "build") == 0


def _zizmor(severity: str) -> list[dict[str, Any]]:
    return [
        {
            "ident": "excessive-permissions",
            "desc": "overly broad permissions",
            "determinations": {"confidence": "High", "severity": severity, "persona": "Regular"},
            "locations": [
                {
                    "symbolic": {
                        "key": {
                            "Local": {
                                "prefix": ".github/workflows",
                                "given_path": ".github/workflows/nightly.yaml",
                            }
                        }
                    }
                }
            ],
        }
    ]


def test_a_medium_zizmor_finding_passes_with_a_path_waiver(repository: Repository) -> None:
    repository.report("zizmor.json", _zizmor("Medium"))
    repository.write_security(
        APPROVERS,
        [waiver(id="excessive-permissions", scanner="zizmor", scope=".github/workflows/*.yaml")],
    )

    assert gate(repository) == 0


def test_a_high_zizmor_finding_fails(repository: Repository) -> None:
    repository.report("zizmor.json", _zizmor("High"))
    repository.write_security(
        APPROVERS,
        [waiver(id="excessive-permissions", scanner="zizmor", scope=".github/workflows/*")],
    )

    assert gate(repository) == 1


def test_a_deptry_finding_needs_a_waiver(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report(
        "deptry-core.json",
        [
            {
                "error": {
                    "code": "DEP002",
                    "message": "'orjson' defined as a dependency but not used",
                },
                "module": "orjson",
                "location": {"file": "packages/core/pyproject.toml", "line": None, "column": None},
            }
        ],
    )

    assert gate(repository) == 1
    assert "| deptry | DEP002 | orjson | finding | fails: no waiver |" in capsys.readouterr().out
    repository.write_security(APPROVERS, [waiver(id="DEP002", scanner="deptry", scope="orjson")])

    assert gate(repository) == 0


def test_a_gitleaks_report_with_a_finding_fails(repository: Repository) -> None:
    repository.report("gitleaks.json", [{"RuleID": "generic-api-key", "File": "app.py"}])

    assert gate(repository) == 1


def test_the_summary_lists_waivers_expiring_within_14_days(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    soon = (TODAY + timedelta(days=14)).isoformat()
    repository.report("npm-web.json", npm_advisory(1096460, "postcss", "moderate"))
    repository.write_security(APPROVERS, [waiver(expires=soon)])

    assert gate(repository) == 0
    text = summary.read_text(encoding="utf-8")
    assert f"- 1096460 (npm-audit) expires {soon}" in text
    assert "### Findings" in text


def test_waivers_only_validates_and_warns_without_reports(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    for report in repository.audit.iterdir():
        report.unlink()
    soon = (TODAY + timedelta(days=3)).isoformat()
    repository.write_security(APPROVERS, [waiver(expires=soon)])

    assert gate(repository, "--waivers-only") == 0
    output = capsys.readouterr().out
    assert f"::warning::waiver 1096460 (npm-audit) expires {soon}" in output
    assert "valid, matches no finding" in output


def test_waivers_only_fails_on_an_invalid_waiver(repository: Repository) -> None:
    repository.write_security(APPROVERS, [waiver(approved_by="someone-else")])

    assert gate(repository, "--waivers-only") == 1


@pytest.mark.parametrize(
    "approvers",
    ["approvers: []\n", "maintainers: [sec-lead]\n", "approvers: [not a handle]\n", ""],
)
def test_unusable_approvers_fail_the_gate(repository: Repository, approvers: str) -> None:
    repository.write_security(approvers, [])

    assert gate(repository) == 1


def test_a_waivers_file_outside_the_yaml_subset_fails(repository: Repository) -> None:
    (repository.security / "waivers.yaml").write_text("waivers: &all []\n", encoding="utf-8")

    assert gate(repository) == 1


# --- The one exception at high severity (84-testing.md section 9.3, ADR 0153) --------------------

INGESTION_IMAGE_TARGET: Final = "docchat/ingestion:local (dockerfile)"


@pytest.mark.parametrize(
    ("report", "target"),
    [
        ("trivy-ingestion.json", INGESTION_IMAGE_TARGET),
        ("trivy-config.json", "apps/ingestion/Dockerfile"),
    ],
)
def test_t527_the_exception_passes_ds002_on_the_ingestion_image(
    repository: Repository, capsys: pytest.CaptureFixture[str], report: str, target: str
) -> None:
    repository.report(report, misconfiguration(target))
    repository.write_exceptions([exception()])

    assert gate(repository, "--job", "images") == 0
    output = capsys.readouterr().out
    review_by = exception()["review_by"]
    assert (
        f"| trivy | DS002 | {target} | HIGH | passes: exception DS002 on ingestion, "
        f"review by {review_by} |"
    ) in output
    assert f"applies to {report}: {target}" in output
    assert "**Result: pass**" in output


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DS002", "DS002"),
        ("DS-0002", "DS002"),
        ("AVD-DS-0002", "DS002"),
        ("DS-0026", "DS026"),
        ("aws-access-key-id", "aws-access-key-id"),
    ],
)
def test_trivy_check_ids_take_the_spec_form(raw: str, expected: str) -> None:
    assert security_gate.trivy_check_id(raw) == expected


def test_t527_the_exception_passes_trivy_0_76s_ds_0002_on_the_ingestion_dockerfile(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report(
        "trivy-config.json", misconfiguration("apps/ingestion/Dockerfile", check="DS-0002")
    )
    repository.write_exceptions([exception()])

    assert gate(repository, "--job", "images") == 0
    assert "| trivy | DS002 | apps/ingestion/Dockerfile | HIGH | passes:" in capsys.readouterr().out


def test_ds_0002_on_another_dockerfile_still_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("trivy-config.json", misconfiguration("apps/api/Dockerfile", check="DS-0002"))
    repository.write_exceptions([exception()])

    assert gate(repository, "--job", "images") == 1
    assert "| trivy | DS002 | apps/api/Dockerfile | HIGH |" in capsys.readouterr().out


def test_ds002_on_the_ingestion_image_fails_without_the_exception(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("trivy-ingestion.json", misconfiguration(INGESTION_IMAGE_TARGET))

    assert gate(repository, "--job", "build") == 1
    assert "| trivy | DS002 |" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("report", "target"),
    [
        ("trivy-api.json", "docchat/api:local (dockerfile)"),
        ("trivy-model-stub.json", "docchat/model-stub:local (dockerfile)"),
        ("trivy-config.json", "apps/api/Dockerfile"),
        ("trivy-config.json", "tests/model-stub/Dockerfile"),
        ("trivy-config.json", "apps/ingestion/image/Dockerfile.extra"),
    ],
)
def test_t527_the_exception_never_covers_another_image(
    repository: Repository, capsys: pytest.CaptureFixture[str], report: str, target: str
) -> None:
    repository.report(report, misconfiguration(target))
    repository.write_exceptions([exception()])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert (
        f"| trivy | DS002 | {target} | HIGH | fails: high and critical findings can't be waived |"
    ) in output
    assert "valid, matches no finding" in output


def _ingestion_report_with(**finding: Any) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(json.dumps(CLEAN_TRIVY))
    report["Results"][0].update(finding)
    return report


@pytest.mark.parametrize(
    "report",
    [
        misconfiguration(INGESTION_IMAGE_TARGET, check="DS001"),
        misconfiguration(INGESTION_IMAGE_TARGET, check="DS029"),
        misconfiguration(INGESTION_IMAGE_TARGET, severity="CRITICAL"),
        _ingestion_report_with(
            Vulnerabilities=[
                {"VulnerabilityID": "CVE-2026-0003", "PkgName": "libssl3", "Severity": "HIGH"}
            ]
        ),
        # Same identifier, another kind of finding: only the Dockerfile check is excepted.
        _ingestion_report_with(
            Vulnerabilities=[{"VulnerabilityID": "DS002", "PkgName": "bash", "Severity": "HIGH"}]
        ),
        _ingestion_report_with(Secrets=[{"RuleID": "DS002", "Severity": "HIGH"}]),
    ],
    ids=[
        "another check",
        "another high check",
        "critical DS002",
        "a vulnerability",
        "a vulnerability named DS002",
        "a secret named DS002",
    ],
)
def test_t527_the_exception_covers_no_other_finding_of_the_ingestion_image(
    repository: Repository, capsys: pytest.CaptureFixture[str], report: dict[str, Any]
) -> None:
    repository.report("trivy-ingestion.json", report)
    repository.write_exceptions([exception()])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert "fails: high and critical findings can't be waived" in output
    assert "passes: exception" not in output


def test_t527_an_exception_past_its_review_date_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    passed = (TODAY - timedelta(days=1)).isoformat()
    repository.report("trivy-config.json", misconfiguration("apps/ingestion/Dockerfile"))
    repository.write_exceptions(
        [exception(added=(TODAY - timedelta(days=100)).isoformat(), review_by=passed)]
    )

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert f"::error::exception DS002 on ingestion: review date {passed} has passed" in output
    assert "fails: exception DS002 on ingestion is invalid" in output


def test_an_exception_on_its_review_date_is_valid(repository: Repository) -> None:
    repository.report("trivy-config.json", misconfiguration("apps/ingestion/Dockerfile"))
    repository.write_exceptions(
        [exception(added=(TODAY - timedelta(days=184)).isoformat(), review_by=TODAY.isoformat())]
    )

    assert gate(repository, "--job", "images") == 0


ONLY_THE_ALLOWED_PAIRS: Final = (
    "only Trivy's DS002 on the ingestion image, or unfixed-os-packages on the api, ingestion or "
    "model-stub image, can be excepted"
)


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"id": "DS001"}, ONLY_THE_ALLOWED_PAIRS),
        ({"id": "CVE-2026-0003"}, ONLY_THE_ALLOWED_PAIRS),
        ({"id": "os-pkgs"}, ONLY_THE_ALLOWED_PAIRS),
        ({"image": "api"}, ONLY_THE_ALLOWED_PAIRS),
        ({"image": "trivy-ingestion.json"}, "only Trivy's DS002"),
        ({"reason": "UID 0 is needed"}, "reason is shorter than 20 characters"),
        ({"decision": "product owner"}, "decision must name the ADR that records it"),
        ({"review_by": "06/05/2027"}, "added and review_by must be dates written YYYY-MM-DD"),
        ({"review_by": "2027-02-30"}, "added and review_by must be dates written YYYY-MM-DD"),
        ({"added": (TODAY + timedelta(days=1)).isoformat()}, "added is in the future"),
        (
            {
                "added": (TODAY - timedelta(days=1)).isoformat(),
                "review_by": (TODAY + timedelta(days=184)).isoformat(),
            },
            "review_by is more than 184 days after added",
        ),
        ({"review_by": "9999-12-31"}, "review_by is more than 184 days after added"),
        ({"scope": "apps/ingestion/Dockerfile"}, "unknown key scope"),
        ({"decision": ""}, "decision must be a non-empty string"),
    ],
)
def test_t527_a_malformed_exception_is_refused(
    repository: Repository,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
    problem: str,
) -> None:
    repository.report("trivy-ingestion.json", misconfiguration(INGESTION_IMAGE_TARGET))
    repository.write_exceptions([exception(**overrides)])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert problem in output
    assert "::error::exception " in output
    assert "passes: exception" not in output


def test_a_missing_exception_key_is_refused(repository: Repository) -> None:
    entry = exception()
    del entry["review_by"]
    repository.write_exceptions([entry])

    assert gate(repository, "--waivers-only") == 1


def test_a_repeated_exception_is_refused(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.write_exceptions([exception(), exception()])

    assert gate(repository) == 1
    assert "repeats exception 1" in capsys.readouterr().out


def test_an_invalid_exception_fails_even_when_no_finding_uses_it(repository: Repository) -> None:
    repository.write_exceptions([exception(image="model-stub")])

    assert gate(repository) == 1
    assert gate(repository, "--waivers-only") == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "exception:\n  - id: DS002\n",
        "exceptions: []\nwaivers: []\n",
        "exceptions: DS002\n",
        "exceptions:\n  - DS002\n",
        "exceptions: &all []\n",
    ],
    ids=["empty", "misnamed key", "second key", "a scalar", "a non-mapping entry", "an anchor"],
)
def test_a_malformed_exceptions_file_fails(repository: Repository, text: str) -> None:
    (repository.security / "exceptions.yaml").write_text(text, encoding="utf-8")

    assert gate(repository) == 1


def test_a_missing_exceptions_file_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    (repository.security / "exceptions.yaml").unlink()

    assert gate(repository, "--job", "images") == 1
    assert "::error::exceptions.yaml:" in capsys.readouterr().out


def test_a_waiver_never_covers_ds002_on_another_image(repository: Repository) -> None:
    repository.report("trivy-api.json", misconfiguration("docchat/api:local (dockerfile)"))
    repository.write_security(
        APPROVERS, [waiver(id="DS002", scanner="trivy", scope="*")], [exception()]
    )

    assert gate(repository, "--job", "images") == 1


def test_waivers_only_warns_14_days_before_the_review_date(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    for report in repository.audit.iterdir():
        report.unlink()
    due = (TODAY + timedelta(days=14)).isoformat()
    repository.write_exceptions([exception(review_by=due)])

    assert gate(repository, "--waivers-only") == 0
    output = capsys.readouterr().out
    assert f"::warning::exception DS002 on ingestion review due {due}" in output
    assert f"- exception DS002 on ingestion review due {due}" in output


def test_the_committed_exceptions_are_the_ones_the_gate_allows() -> None:
    path = Path(security_gate.REPOSITORY_ROOT) / ".github" / "security" / "exceptions.yaml"
    document = security_gate.parse_yaml_subset(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    entries = document["exceptions"]
    assert [(entry["id"], entry["image"], entry["decision"]) for entry in entries] == [
        ("DS002", "ingestion", "ADR 0153"),
        ("unfixed-os-packages", "api", "ADR 0154"),
        ("unfixed-os-packages", "ingestion", "ADR 0154"),
        ("unfixed-os-packages", "model-stub", "ADR 0154"),
    ]
    # Each judged on the day it was added, so the test holds the format and the limits without
    # failing once a review date passes; the images job and the nightly run are what fail then.
    for index, entry in enumerate(entries):
        added = date.fromisoformat(entry["added"])
        loaded = security_gate.load_exceptions(path, added)[index]
        assert loaded.problems == [], entry
        assert loaded.review_by is not None
        limit = security_gate.EXCEPTION_KINDS[entry["id"]].max_days
        assert loaded.review_by - added <= timedelta(days=limit)


# --- Unfixed OS-package findings (84-testing.md section 9.3, ADR 0154) ---------------------------

UNFIXED_OS: Final = "unfixed-os-packages"


def unfixed_os(image: str, **overrides: str) -> dict[str, str]:
    """An entry of exceptions.yaml for one image's unfixed OS-package findings, with valid dates."""
    values = {
        "id": UNFIXED_OS,
        "image": image,
        "reason": "Debian has published no fix yet for these findings in the base packages.",
        "decision": "ADR 0154",
        "added": (TODAY - timedelta(days=10)).isoformat(),
        "review_by": (TODAY + timedelta(days=80)).isoformat(),
    }
    values.update(overrides)
    return values


def image_report(image: str, result_class: str = "os-pkgs", **vulnerability: str) -> dict[str, Any]:
    """A Trivy image report with one HIGH vulnerability, without a fix unless overridden, in a
    result of the given class (`os-pkgs` for the distribution's packages)."""
    finding = {
        "VulnerabilityID": "CVE-2026-4242",
        "PkgName": "libsqlite3-0",
        "InstalledVersion": "3.46.1-7",
        "Severity": "HIGH",
        "Status": "affected",
    }
    finding.update(vulnerability)
    result = {
        "Target": f"docchat/{image}:local (debian 13.7)",
        "Class": result_class,
        "Type": "debian",
        "Vulnerabilities": [finding],
    }
    if result_class != "os-pkgs":
        result.update(Target="Python", Type="python-pkg")
    return {"SchemaVersion": 2, "ArtifactName": f"docchat/{image}:local", "Results": [result]}


@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
@pytest.mark.parametrize("image", ["api", "ingestion", "model-stub"])
def test_t527_an_unfixed_os_package_finding_passes_under_its_images_exception(
    repository: Repository, capsys: pytest.CaptureFixture[str], image: str, severity: str
) -> None:
    repository.report(f"trivy-{image}.json", image_report(image, Severity=severity))
    repository.write_exceptions([exception(), unfixed_os(image)])

    assert gate(repository, "--job", "images") == 0
    assert gate(repository, "--job", "build") == 0
    output = capsys.readouterr().out
    review_by = unfixed_os(image)["review_by"]
    assert (
        f"| trivy | CVE-2026-4242 | libsqlite3-0 | {severity} | passes: exception "
        f"{UNFIXED_OS} on {image}, review by {review_by} |"
    ) in output
    assert f"applies to 1 unfixed OS-package finding in trivy-{image}.json" in output
    assert "**Result: fail**" not in output


@pytest.mark.parametrize(
    "fix",
    [
        {"FixedVersion": "3.46.1-8", "Status": "fixed"},
        {"FixedVersion": "3.46.1-8"},
        {"Status": "fixed"},
    ],
    ids=["fixed version and status", "a fixed version alone", "the status alone"],
)
def test_t527_an_os_package_finding_with_a_fix_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str], fix: dict[str, str]
) -> None:
    repository.report("trivy-api.json", image_report("api", **fix))
    repository.write_exceptions([unfixed_os("api")])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert (
        "| trivy | CVE-2026-4242 | libsqlite3-0 | HIGH | "
        "fails: high and critical findings can't be waived |"
    ) in output
    assert "passes: exception" not in output


def test_t527_the_nightly_rescan_fails_once_an_os_package_finding_gains_a_fix(
    repository: Repository,
) -> None:
    repository.write_exceptions([unfixed_os("ingestion")])
    repository.report("trivy-ingestion.json", image_report("ingestion"))
    assert gate(repository, "--job", "build") == 0

    repository.report("trivy-ingestion.json", image_report("ingestion", FixedVersion="3.46.1-8"))
    assert gate(repository, "--job", "build") == 1


@pytest.mark.parametrize(
    "report",
    [
        image_report("api", result_class="lang-pkgs", PkgName="urllib3"),
        image_report("api", result_class="lang-pkgs", PkgName="golang.org/x/text"),
        {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "/app/.env",
                    "Class": "secret",
                    "Secrets": [{"RuleID": "aws-access-key-id", "Severity": "CRITICAL"}],
                }
            ],
        },
        misconfiguration("docchat/api:local (dockerfile)", check="DS026"),
    ],
    ids=["a Python package", "a Go module", "a secret", "a misconfiguration"],
)
def test_t527_the_os_package_exception_covers_no_other_kind_of_finding(
    repository: Repository, capsys: pytest.CaptureFixture[str], report: dict[str, Any]
) -> None:
    repository.report("trivy-api.json", report)
    repository.write_exceptions([unfixed_os("api")])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert "fails: high and critical findings can't be waived" in output
    assert "passes: exception" not in output


def test_t527_an_images_os_package_exception_never_covers_another_images_report(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.report("trivy-api.json", image_report("api"))
    repository.write_exceptions([unfixed_os("model-stub"), unfixed_os("ingestion")])

    assert gate(repository, "--job", "images") == 1
    output = capsys.readouterr().out
    assert "| libsqlite3-0 | HIGH | fails: high and critical findings can't be waived |" in output
    assert "valid, matches no finding" in output


def test_t527_an_os_package_exception_past_its_review_date_fails(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    passed = (TODAY - timedelta(days=1)).isoformat()
    repository.report("trivy-api.json", image_report("api"))
    repository.write_exceptions(
        [unfixed_os("api", added=(TODAY - timedelta(days=60)).isoformat(), review_by=passed)]
    )

    assert gate(repository, "--job", "images") == 1
    assert gate(repository, "--waivers-only") == 1
    output = capsys.readouterr().out
    assert f"::error::exception {UNFIXED_OS} on api: review date {passed} has passed" in output
    assert f"fails: exception {UNFIXED_OS} on api is invalid" in output


@pytest.mark.parametrize(("days", "result"), [(90, 0), (91, 1)])
def test_t527_an_os_package_exception_lasts_at_most_90_days(
    repository: Repository, capsys: pytest.CaptureFixture[str], days: int, result: int
) -> None:
    added = TODAY - timedelta(days=1)
    review_by = (added + timedelta(days=days)).isoformat()
    repository.report("trivy-model-stub.json", image_report("model-stub"))
    repository.write_exceptions(
        [unfixed_os("model-stub", added=added.isoformat(), review_by=review_by)]
    )

    assert gate(repository, "--job", "images") == result
    rejected = "review_by is more than 90 days after added" in capsys.readouterr().out
    assert rejected is bool(result)


@pytest.mark.parametrize("image", ["web", "trivy-api.json", "*", "config"])
def test_an_os_package_exception_for_another_image_is_refused(
    repository: Repository, capsys: pytest.CaptureFixture[str], image: str
) -> None:
    repository.write_exceptions([unfixed_os(image)])

    assert gate(repository, "--job", "images") == 1
    assert ONLY_THE_ALLOWED_PAIRS in capsys.readouterr().out


def test_a_repeated_os_package_exception_is_refused(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.write_exceptions([unfixed_os("api"), unfixed_os("api")])

    assert gate(repository, "--waivers-only") == 1
    assert "repeats exception 1" in capsys.readouterr().out


def test_waivers_only_warns_14_days_before_an_os_package_review_date(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    for report in repository.audit.iterdir():
        report.unlink()
    due = (TODAY + timedelta(days=14)).isoformat()
    later = (TODAY + timedelta(days=15)).isoformat()
    repository.write_exceptions(
        [unfixed_os("api", review_by=due), unfixed_os("ingestion", review_by=later)]
    )

    assert gate(repository, "--waivers-only") == 0
    output = capsys.readouterr().out
    assert f"::warning::exception {UNFIXED_OS} on api review due {due}" in output
    assert f"- exception {UNFIXED_OS} on api review due {due}" in output
    assert f"{UNFIXED_OS} on ingestion review due" not in output


# --- The YAML subset -----------------------------------------------------------------------------


def test_the_yaml_subset_reads_block_and_flow_forms() -> None:
    text = """\
---
# the approvers
approvers:
  - sec-lead   # a comment after a value
  - 'sec #two'
waivers:
  - id: "GHSA-xxxx"
    reason: 'it''s build-time only'
    empty: []
  - id: plain
list: [a, "b, c", 'd']
flag: true
nothing: ~
"""
    assert security_gate.parse_yaml_subset(text) == {
        "approvers": ["sec-lead", "sec #two"],
        "waivers": [
            {"id": "GHSA-xxxx", "reason": "it's build-time only", "empty": []},
            {"id": "plain"},
        ],
        "list": ["a", "b, c", "d"],
        "flag": True,
        "nothing": None,
    }


@pytest.mark.parametrize(
    "text",
    [
        "a: &anchor 1\n",
        "a: {b: 1}\n",
        "a:\n\t- b\n",
        "a: |\n  text\n",
        "a: 1\na: 2\n",
        "a: [b, [c]]\n",
        "a: b: c\n",
        "a: 'unterminated\n",
        "  a: 1\nb: 2\n",
        "a:\n  - b\n  c: d\n",
    ],
)
def test_yaml_outside_the_subset_is_refused(text: str) -> None:
    with pytest.raises(security_gate.YamlSubsetError):
        security_gate.parse_yaml_subset(text)


def test_dates_stay_strings_for_the_gate_to_check() -> None:
    parsed = security_gate.parse_yaml_subset("added: 2026-10-30\n")
    assert parsed == {"added": "2026-10-30"}
    assert date.fromisoformat("2026-10-30") == date(2026, 10, 30)


def test_reports_of_sources_not_yet_built_are_not_expected(
    repository: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    (repository.root / "apps" / "web" / "package.json").unlink()
    (repository.root / "apps" / "api" / "Dockerfile").unlink()
    (repository.audit / "npm-web.json").unlink()
    (repository.audit / "trivy-api.json").unlink()
    for job in ("security", "images", "build"):
        assert gate(repository, "--job", job) == 0
    assert "missing report" not in capsys.readouterr().out
