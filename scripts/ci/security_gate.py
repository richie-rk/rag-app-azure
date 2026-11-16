"""The one security gate of CI (84-testing.md section 9, 82-security.md SEC-110, ADR 0136).

Reads the scanner reports in `build/audit/`, `.github/security/waivers.yaml`,
`.github/security/approvers.yaml` and `.github/security/exceptions.yaml`, and exits 1 when a
report is missing or isn't a valid report of its scanner, when any never-waivable finding exists
that the two named exceptions don't cover, when a waivable finding has no valid waiver, or when
any waiver or exception is invalid. Every high or critical finding is never-waivable: a waiver
naming one is listed as "not applicable: high and critical findings can't be waived" and changes
nothing. The findings, the waivers, the exceptions, and the waivers expiring and the exceptions
falling due for review within 14 days go to the job summary.

- pip-audit (`pip-audit.json`): rated `HIGH` or `CRITICAL`, or unrated, never waivable; `LOW` and
  `MODERATE` waivable.
- npm audit (`npm-<dir>.json`): `high` and `critical` never waivable; `info`, `low` and `moderate`
  waivable.
- zizmor (`zizmor.json`): `high` never waivable; `medium` and below waivable.
- deptry (`deptry-<member>.json`): every finding waivable.
- Trivy (`trivy-<image>.json`, `trivy-config.json`): every HIGH or CRITICAL vulnerability or
  misconfiguration and every secret never waivable; nothing waivable. The named exceptions below
  pass the one HIGH misconfiguration DS002 on the ingestion image, and an image's HIGH or
  CRITICAL OS-package vulnerabilities that have no fixed version.
- gitleaks (`gitleaks*.json`, read when present): every finding never waivable.

pip-audit reports identifiers without severities, so each finding is rated from its OSV record
(`GET https://api.osv.dev/v1/vulns/<id>`, 15-second timeout, 3 attempts 15 s apart) or, when that
record carries no `database_specific.severity`, from the records of the `GHSA-` identifiers in its
`aliases`; the highest rating wins, and a finding no record rates fails like `HIGH`.

Report sets (`--job`): `security` (the default; `pip-audit.json`, `npm-web.json`, `npm-e2e.json`,
`npm-swa-cli.json`, `zizmor.json` and `deptry-<member>.json` for every workspace member, named by
the last component of its path), `images` (`trivy-api.json`, `trivy-ingestion.json`,
`trivy-model-stub.json`, `trivy-config.json`) and `build` (the three image reports, for the `build`
job of `deploy.yaml`). `--waivers-only` validates the three YAML files alone and warns about
waivers expiring and exceptions falling due for review within 14 days (the nightly run).

`waivers.yaml` holds `waivers:` followed by a list of mappings with exactly the keys `id`,
`scanner`, `scope`, `reason`, `ticket`, `added`, `expires` and `approved_by`; `approvers.yaml`
holds `approvers: [<GitHub handle>, ...]`. The three files are read with a parser for that YAML
subset (comments, block mappings and sequences, one-line flow sequences, plain and quoted
scalars), since the gate uses the Python standard library only; anything outside the subset fails
the gate.

The exceptions at high severity (84-testing.md section 9.3): `exceptions.yaml` holds
`exceptions:` followed by a list of mappings with exactly the keys `id`, `image`, `reason`,
`decision`, `added` and `review_by`. The only pairs an entry may name are in `EXCEPTION_KINDS`, and
any other entry is invalid and fails the gate:

- `DS002` on `ingestion` (ADR 0153): Trivy's Dockerfile check, raised because the ingestion image's
  Functions host keeps UID 0 so that the parsing child can switch to its parse user. A valid entry
  passes a HIGH DS002 misconfiguration reported in `trivy-ingestion.json`, or in
  `trivy-config.json` on `apps/ingestion/Dockerfile`, and nothing else. `review_by` is at most 184
  days (six calendar months) after `added`.
- `unfixed-os-packages` on `api`, `ingestion` or `model-stub` (ADR 0154), one entry per image: a
  valid entry passes a vulnerability in that image's own report `trivy-<image>.json` only when its
  result's class is `os-pkgs` (a package of the base image's distribution), it is rated HIGH or
  CRITICAL, and the report gives it no fixed version (no `FixedVersion` and no status `fixed`). A
  language-package finding, a finding with a fix, a secret, a misconfiguration or another image's
  finding still fails. `review_by` is at most 90 days after `added`.

For both, `added` isn't after today, and once `review_by` has passed the entry is invalid, failing
the gate, until a reviewed change renews it with new dates.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WAIVABLE_SCANNERS = ("pip-audit", "npm-audit", "zizmor", "deptry")
WAIVER_KEYS = ("id", "scanner", "scope", "reason", "ticket", "added", "expires", "approved_by")
MIN_REASON_LENGTH = 20
MAX_WAIVER_DAYS = 90
EXPIRY_WARNING_DAYS = 14
# Report name -> the package or Dockerfile it audits. The build adds each in its stage (the API
# image at stage 2, the web packages at stage 5), so a report is expected once its source exists.
NPM_DIRECTORIES = {"web": "apps/web", "e2e": "tests/e2e", "swa-cli": "scripts/deploy/swa-cli"}
TRIVY_IMAGES = {
    "api": "apps/api/Dockerfile",
    "ingestion": "apps/ingestion/Dockerfile",
    "model-stub": "tests/model-stub/Dockerfile",
}
NOT_APPLICABLE = "not applicable: high and critical findings can't be waived"

EXCEPTION_KEYS = ("id", "image", "reason", "decision", "added", "review_by")


@dataclass(frozen=True)
class ExceptionKind:
    """What an exception's `id` may cover: the images it may name and its longest review period."""

    images: frozenset[str]
    max_days: int


DS002 = "DS002"
UNFIXED_OS_PACKAGES = "unfixed-os-packages"
# The kinds an exception may be, with the images each may name. The list lives here rather than in
# the YAML file, so another exception needs a reviewed change to the gate and the spec, never only
# an entry in the file. Six calendar months never exceed 184 days, so a review date stays finite
# and near; an unfixed OS-package finding is looked at again within 90 days (ADR 0154).
EXCEPTION_KINDS = {
    DS002: ExceptionKind(images=frozenset({"ingestion"}), max_days=184),
    UNFIXED_OS_PACKAGES: ExceptionKind(images=frozenset(TRIVY_IMAGES), max_days=90),
}
ALLOWED_EXCEPTIONS = frozenset(
    (check, image) for check, kind in EXCEPTION_KINDS.items() for image in kind.images
)
DS002_SEVERITY = "HIGH"
# The Trivy result class of the base image distribution's packages; `lang-pkgs`, `secret` and
# `config` results are never covered.
OS_PACKAGES_CLASS = "os-pkgs"
DECISION = re.compile(r"^ADR \d{4}$")

OSV_URL = "https://api.osv.dev/v1/vulns/"
OSV_TIMEOUT_SECONDS = 15
OSV_ATTEMPTS = 3
OSV_RETRY_SECONDS = 15.0
OSV_SEVERITY_RANK = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}
OSV_WAIVABLE = frozenset({"LOW", "MODERATE"})
OSV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HTTP_NOT_FOUND = 404  # OSV's answer for an identifier it has no record of
NPM_REPORT_VERSION = 2

NPM_NEVER = frozenset({"high", "critical"})
NPM_WAIVABLE = frozenset({"info", "low", "moderate"})
ZIZMOR_NEVER = frozenset({"high"})
ZIZMOR_WAIVABLE = frozenset({"medium", "low", "informational"})
TRIVY_NEVER = frozenset({"HIGH", "CRITICAL"})
GHSA_URL = re.compile(r"(GHSA(?:-[0-9a-z]{4}){3})", re.IGNORECASE)
HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- The YAML subset of the two security files ---------------------------------------------------


class YamlSubsetError(ValueError):
    """The text isn't in the YAML subset the security files are written in."""


@dataclass(frozen=True)
class _Line:
    number: int
    indent: int
    text: str


PLAIN_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")
MIN_QUOTED_LENGTH = 2


def _strip_comment(raw: str) -> str:
    quote: str | None = None
    for index, character in enumerate(raw):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
    return raw


def _lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        content = _strip_comment(raw).rstrip()
        if not content.strip():
            continue
        if content.strip() == "---" and not lines:
            continue
        indentation = content[: len(content) - len(content.lstrip())]
        if "\t" in indentation:
            raise YamlSubsetError(f"line {number}: tabs can't indent YAML")
        lines.append(_Line(number, len(indentation), content.strip()))
    return lines


def _double_quoted(text: str, number: int) -> str:
    escapes = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t"}
    result: list[str] = []
    index = 1
    while index < len(text) - 1:
        character = text[index]
        if character == "\\":
            index += 1
            if index >= len(text) - 1 or text[index] not in escapes:
                raise YamlSubsetError(f"line {number}: unsupported escape in a quoted scalar")
            result.append(escapes[text[index]])
        elif character == '"':
            raise YamlSubsetError(f"line {number}: unescaped quote in a quoted scalar")
        else:
            result.append(character)
        index += 1
    return "".join(result)


def _scalar(text: str, number: int) -> object:
    text = text.strip()
    if not text:
        raise YamlSubsetError(f"line {number}: empty value")
    if text[0] == "'":
        if len(text) < MIN_QUOTED_LENGTH or text[-1] != "'" or "'" in text[1:-1].replace("''", ""):
            raise YamlSubsetError(f"line {number}: unterminated single-quoted scalar")
        return text[1:-1].replace("''", "'")
    if text[0] == '"':
        if len(text) < MIN_QUOTED_LENGTH or text[-1] != '"':
            raise YamlSubsetError(f"line {number}: unterminated double-quoted scalar")
        return _double_quoted(text, number)
    if text[0] in "{&*!|>%@`" or text.startswith("- "):
        raise YamlSubsetError(f"line {number}: {text[0]!r} starts YAML outside the subset")
    if ": " in text or text.endswith(":"):
        raise YamlSubsetError(f"line {number}: quote a value that holds ': '")
    keywords: dict[str, object] = {"true": True, "false": False, "null": None, "~": None}
    return keywords.get(text, text)


def _flow_sequence(text: str, number: int) -> list[object]:
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for character in inner:
        if quote:
            current.append(character)
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
            current.append(character)
        elif character in "[]{}":
            raise YamlSubsetError(f"line {number}: nested flow collections are outside the subset")
        elif character == ",":
            items.append("".join(current))
            current = []
        else:
            current.append(character)
    if quote:
        raise YamlSubsetError(f"line {number}: unterminated quoted scalar")
    items.append("".join(current))
    return [_scalar(item, number) for item in items]


def _value(text: str, number: int) -> object:
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlSubsetError(f"line {number}: a flow sequence must end on its line")
        return _flow_sequence(text, number)
    return _scalar(text, number)


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.position = 0

    def _peek(self) -> _Line | None:
        return self.lines[self.position] if self.position < len(self.lines) else None

    def block(self, indent: int) -> object:
        line = self._peek()
        if line is None or line.indent != indent:
            raise YamlSubsetError("unexpected indentation")
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def _child(self, parent_indent: int) -> object:
        line = self._peek()
        if line is None or line.indent <= parent_indent:
            return None
        return self.block(line.indent)

    def mapping(self, indent: int) -> dict[str, object]:
        result: dict[str, object] = {}
        while (line := self._peek()) is not None and line.indent == indent:
            if line.text == "-" or line.text.startswith("- "):
                raise YamlSubsetError(f"line {line.number}: a sequence item inside a mapping")
            match = PLAIN_KEY.match(line.text)
            if not match:
                raise YamlSubsetError(f"line {line.number}: expected 'key: value'")
            key, rest = match.group(1), match.group(2)
            if key in result:
                raise YamlSubsetError(f"line {line.number}: duplicate key {key}")
            self.position += 1
            result[key] = _value(rest, line.number) if rest else self._child(indent)
        line = self._peek()
        if line is not None and line.indent > indent:
            raise YamlSubsetError(f"line {line.number}: unexpected indentation")
        return result

    def sequence(self, indent: int) -> list[object]:
        result: list[object] = []
        while (line := self._peek()) is not None and line.indent == indent:
            if not (line.text == "-" or line.text.startswith("- ")):
                raise YamlSubsetError(f"line {line.number}: expected a sequence item")
            rest = line.text[1:].lstrip()
            if not rest:
                self.position += 1
                result.append(self._child(indent))
                continue
            if PLAIN_KEY.match(rest):
                # A mapping item: its keys line up with the first key, after the dash.
                item_indent = indent + len(line.text) - len(rest)
                self.lines[self.position] = _Line(line.number, item_indent, rest)
                result.append(self.mapping(item_indent))
                continue
            self.position += 1
            result.append(_value(rest, line.number))
        line = self._peek()
        if line is not None and line.indent > indent:
            raise YamlSubsetError(f"line {line.number}: unexpected indentation")
        return result


def parse_yaml_subset(text: str) -> object:
    """Parse the YAML subset of the security files; raise YamlSubsetError outside it."""
    lines = _lines(text)
    if not lines:
        return None
    parser = _Parser(lines)
    document = parser.block(lines[0].indent)
    if parser.position != len(lines):
        raise YamlSubsetError(f"line {lines[parser.position].number}: unexpected content")
    return document


# --- Approvers and waivers -----------------------------------------------------------------------


@dataclass
class Waiver:
    index: int
    values: dict[str, str]
    problems: list[str] = field(default_factory=list)
    expires: date | None = None
    matched: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.values.get("id") or f"waiver {self.index}"

    @property
    def valid(self) -> bool:
        return not self.problems


def load_approvers(path: Path) -> list[str]:
    """Read `approvers.yaml`: `approvers:` and a list of GitHub handles."""
    document = parse_yaml_subset(path.read_text(encoding="utf-8"))
    approvers = document.get("approvers") if isinstance(document, dict) else None
    if not isinstance(approvers, list) or not approvers:
        raise YamlSubsetError(f"{path.name} must hold 'approvers:' with at least one handle")
    handles: list[str] = []
    for handle in approvers:
        if not isinstance(handle, str) or not HANDLE.match(handle):
            raise YamlSubsetError(f"{path.name}: {handle!r} isn't a GitHub handle")
        handles.append(handle)
    return handles


def _parse_date(text: str) -> date | None:
    if not DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _waiver_problems(values: dict[str, str], approvers: Iterable[str], today: date) -> list[str]:
    problems: list[str] = []
    if values["scanner"] not in WAIVABLE_SCANNERS:
        problems.append(f"scanner must be one of {', '.join(WAIVABLE_SCANNERS)}")
    if len(values["reason"].strip()) < MIN_REASON_LENGTH:
        problems.append(f"reason is shorter than {MIN_REASON_LENGTH} characters")
    if not re.match(r"^https://[^\s/]+\.[^\s/]+(/\S*)?$", values["ticket"]):
        problems.append("ticket must be an https URL")
    if values["approved_by"] not in approvers:
        problems.append(f"approved_by {values['approved_by']} isn't listed in approvers.yaml")
    added = _parse_date(values["added"])
    expires = _parse_date(values["expires"])
    if added is None or expires is None:
        problems.append("added and expires must be dates written YYYY-MM-DD")
        return problems
    if expires < added:
        problems.append("expires is before added")
    if expires > added + timedelta(days=MAX_WAIVER_DAYS):
        problems.append(f"expires is more than {MAX_WAIVER_DAYS} days after added")
    if expires < today:
        problems.append(f"expired on {expires.isoformat()}")
    return problems


def load_waivers(path: Path, approvers: Iterable[str], today: date) -> list[Waiver]:
    """Read and validate `waivers.yaml`; every waiver carries its own problems."""
    document = parse_yaml_subset(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"waivers"}:
        raise YamlSubsetError(f"{path.name} must hold exactly one key, 'waivers:'")
    entries = document["waivers"]
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise YamlSubsetError(f"{path.name}: 'waivers' must be a list")
    approver_set = set(approvers)
    waivers: list[Waiver] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            waivers.append(Waiver(index, {}, ["a waiver must be a mapping"]))
            continue
        values = {key: value for key, value in entry.items() if isinstance(value, str)}
        problems = [
            f"{key} must be a non-empty string"
            for key in WAIVER_KEYS
            if not values.get(key, "").strip()
        ]
        problems += [f"unknown key {key}" for key in entry if key not in WAIVER_KEYS]
        waiver = Waiver(index, values, problems)
        if not problems:
            waiver.problems = _waiver_problems(values, approver_set, today)
            waiver.expires = _parse_date(values["expires"])
        waivers.append(waiver)
    return waivers


# --- The named exceptions at high severity -------------------------------------------------------


@dataclass
class NamedException:
    index: int
    values: dict[str, str]
    problems: list[str] = field(default_factory=list)
    review_by: date | None = None
    matched: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        check, image = self.values.get("id"), self.values.get("image")
        return f"{check} on {image}" if check and image else f"exception {self.index}"

    @property
    def valid(self) -> bool:
        return not self.problems


def _exception_problems(values: dict[str, str], today: date) -> list[str]:
    problems: list[str] = []
    kind = EXCEPTION_KINDS.get(values["id"])
    if (values["id"], values["image"]) not in ALLOWED_EXCEPTIONS:
        problems.append(
            "only Trivy's DS002 on the ingestion image, or unfixed-os-packages on the api, "
            "ingestion or model-stub image, can be excepted"
        )
    if len(values["reason"].strip()) < MIN_REASON_LENGTH:
        problems.append(f"reason is shorter than {MIN_REASON_LENGTH} characters")
    if not DECISION.match(values["decision"]):
        problems.append("decision must name the ADR that records it, written 'ADR NNNN'")
    added = _parse_date(values["added"])
    review_by = _parse_date(values["review_by"])
    if added is None or review_by is None:
        problems.append("added and review_by must be dates written YYYY-MM-DD")
        return problems
    if added > today:
        problems.append("added is in the future")
    if review_by < added:
        problems.append("review_by is before added")
    if kind is not None and review_by > added + timedelta(days=kind.max_days):
        problems.append(f"review_by is more than {kind.max_days} days after added")
    if review_by < today:
        problems.append(f"review date {review_by.isoformat()} has passed; renew it with new dates")
    return problems


def load_exceptions(path: Path, today: date) -> list[NamedException]:
    """Read and validate `exceptions.yaml`; every exception carries its own problems."""
    document = parse_yaml_subset(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"exceptions"}:
        raise YamlSubsetError(f"{path.name} must hold exactly one key, 'exceptions:'")
    entries = document["exceptions"]
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise YamlSubsetError(f"{path.name}: 'exceptions' must be a list")
    exceptions: list[NamedException] = []
    seen: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            exceptions.append(NamedException(index, {}, ["an exception must be a mapping"]))
            continue
        values = {key: value for key, value in entry.items() if isinstance(value, str)}
        problems = [
            f"{key} must be a non-empty string"
            for key in EXCEPTION_KEYS
            if not values.get(key, "").strip()
        ]
        problems += [f"unknown key {key}" for key in entry if key not in EXCEPTION_KEYS]
        exception = NamedException(index, values, problems)
        if not problems:
            exception.problems = _exception_problems(values, today)
            exception.review_by = _parse_date(values["review_by"])
            pair = (values["id"], values["image"])
            if pair in seen:
                exception.problems.append(f"repeats exception {seen[pair]}")
            seen.setdefault(pair, index)
        exceptions.append(exception)
    return exceptions


# --- Findings and reports ------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    scanner: str
    report: str
    id: str
    subject: str
    severity: str
    waivable: bool
    # Other identifiers of the same finding (a PYSEC record's GHSA alias, an npm advisory's GHSA).
    aliases: tuple[str, ...] = ()
    # Further names or paths a waiver's scope glob may match, beside `subject`.
    scopes: tuple[str, ...] = ()
    # Trivy's kind of finding (vulnerability, secret or misconfiguration), which the exceptions
    # check so that a vulnerability or secret can never pass under a check's ID.
    kind: str = ""
    # For a Trivy vulnerability: its result's class (`os-pkgs` for the distribution's packages,
    # `lang-pkgs` for a language's), and the fixed version and status the report gives it.
    result_class: str = ""
    fixed_version: str = ""
    status: str = ""


class ReportError(ValueError):
    """A report is missing or isn't a valid report of its scanner."""


class OsvUnavailableError(RuntimeError):
    """An OSV record couldn't be fetched after every attempt."""


def fetch_osv_record(vulnerability_id: str) -> dict[str, Any] | None:
    """Return the OSV record of an identifier, or None when OSV has no record of it."""
    if not OSV_ID.match(vulnerability_id):
        return None
    last_error: Exception | None = None
    for attempt in range(OSV_ATTEMPTS):
        if attempt:
            time.sleep(OSV_RETRY_SECONDS)
        try:
            # The URL is the fixed OSV base and an identifier checked against OSV_ID above.
            with urllib.request.urlopen(  # noqa: S310
                OSV_URL + vulnerability_id, timeout=OSV_TIMEOUT_SECONDS
            ) as response:
                record = json.load(response)
            return record if isinstance(record, dict) else None
        except urllib.error.HTTPError as error:
            if error.code == HTTP_NOT_FOUND:
                return None
            last_error = error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
    raise OsvUnavailableError(f"{vulnerability_id}: {last_error}")


def _osv_severity(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    specific = record.get("database_specific")
    severity = specific.get("severity") if isinstance(specific, dict) else None
    if isinstance(severity, str) and severity.upper() in OSV_SEVERITY_RANK:
        return severity.upper()
    return None


def rate_osv(
    vulnerability_id: str, fetch: Callable[[str], dict[str, Any] | None]
) -> tuple[str | None, tuple[str, ...]]:
    """Return the OSV rating of an identifier (None when unrated) and the record's aliases."""
    try:
        record = fetch(vulnerability_id)
    except OsvUnavailableError:
        return None, ()
    if record is None:
        return None, ()
    aliases = tuple(alias for alias in record.get("aliases", []) if isinstance(alias, str))
    own = _osv_severity(record)
    if own:
        return own, aliases
    best: str | None = None
    for alias in aliases:
        if not alias.upper().startswith("GHSA-"):
            continue
        try:
            rating = _osv_severity(fetch(alias))
        except OsvUnavailableError:
            continue
        if rating and (best is None or OSV_SEVERITY_RANK[rating] > OSV_SEVERITY_RANK[best]):
            best = rating
    return best, aliases


def _normalise_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pip_audit_findings(
    report: Any, name: str, fetch: Callable[[str], dict[str, Any] | None]
) -> list[Finding]:
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise ReportError(f"{name}: a pip-audit report needs a 'dependencies' array")
    findings: list[Finding] = []
    for dependency in report["dependencies"]:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            raise ReportError(f"{name}: every dependency needs a name")
        vulnerabilities = dependency.get("vulns", [])
        if not isinstance(vulnerabilities, list):
            raise ReportError(f"{name}: {dependency['name']}: 'vulns' must be an array")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("id"), str):
                raise ReportError(f"{name}: {dependency['name']}: every finding needs an id")
            rating, record_aliases = rate_osv(vulnerability["id"], fetch)
            reported = [a for a in vulnerability.get("aliases", []) if isinstance(a, str)]
            findings.append(
                Finding(
                    scanner="pip-audit",
                    report=name,
                    id=vulnerability["id"],
                    subject=_normalise_package(dependency["name"]),
                    severity=rating or "unrated",
                    waivable=rating in OSV_WAIVABLE,
                    aliases=tuple(dict.fromkeys([*record_aliases, *reported])),
                )
            )
    return findings


def npm_findings(report: Any, name: str) -> list[Finding]:
    if not isinstance(report, dict) or "error" in report:
        raise ReportError(f"{name}: npm audit reported an error instead of a result")
    if report.get("auditReportVersion") != NPM_REPORT_VERSION or not isinstance(
        report.get("vulnerabilities"), dict
    ):
        raise ReportError(f"{name}: needs auditReportVersion 2 and a 'vulnerabilities' object")
    advisories: dict[tuple[str, str], Finding] = {}
    for entry in report["vulnerabilities"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("via", []), list):
            raise ReportError(f"{name}: every vulnerability needs a 'via' array")
        for via in entry.get("via", []):
            if not isinstance(via, dict):
                continue  # a package name: the advisory itself sits under that package
            source = str(via.get("source", ""))
            package = str(via.get("name", entry.get("name", "")))
            severity = str(via.get("severity", "")).lower()
            match = GHSA_URL.search(str(via.get("url", "")))
            aliases = (match.group(1),) if match else ()
            advisories[(source, package)] = Finding(
                scanner="npm-audit",
                report=name,
                id=source or (aliases[0] if aliases else "unknown"),
                subject=package,
                severity=severity or "unrated",
                waivable=severity in NPM_WAIVABLE,
                aliases=aliases,
            )
    return list(advisories.values())


def _zizmor_path(finding: dict[str, Any]) -> str:
    for location in finding.get("locations", []):
        try:
            path = location["symbolic"]["key"]["Local"]["given_path"]
        except (KeyError, TypeError):
            continue
        if isinstance(path, str):
            return path
    return ""


def zizmor_findings(report: Any, name: str) -> list[Finding]:
    if not isinstance(report, list):
        raise ReportError(f"{name}: a zizmor report must be a JSON array")
    findings: list[Finding] = []
    for item in report:
        if not isinstance(item, dict) or not isinstance(item.get("ident"), str):
            raise ReportError(f"{name}: every finding needs an 'ident'")
        determinations = item.get("determinations", {})
        severity = ""
        if isinstance(determinations, dict):
            severity = str(determinations.get("severity", "")).lower()
        findings.append(
            Finding(
                scanner="zizmor",
                report=name,
                id=item["ident"],
                subject=_zizmor_path(item),
                severity=severity or "unrated",
                waivable=severity in ZIZMOR_WAIVABLE,
            )
        )
    return findings


def deptry_findings(report: Any, name: str) -> list[Finding]:
    if not isinstance(report, list):
        raise ReportError(f"{name}: a deptry report must be a JSON array")
    findings: list[Finding] = []
    for item in report:
        error = item.get("error") if isinstance(item, dict) else None
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            raise ReportError(f"{name}: every finding needs an error code")
        location = item.get("location")
        path = location.get("file") if isinstance(location, dict) else None
        findings.append(
            Finding(
                scanner="deptry",
                report=name,
                id=error["code"],
                subject=str(item.get("module", "")),
                severity="finding",
                waivable=True,
                scopes=(str(path),) if path else (),
            )
        )
    return findings


def trivy_findings(report: Any, name: str) -> list[Finding]:
    if not isinstance(report, dict) or "SchemaVersion" not in report:
        raise ReportError(f"{name}: a Trivy report needs 'SchemaVersion' and 'Results'")
    results = report.get("Results")
    if not isinstance(results, list):
        raise ReportError(f"{name}: a Trivy report needs 'SchemaVersion' and 'Results'")
    findings: list[Finding] = []
    for result in results:
        if not isinstance(result, dict):
            raise ReportError(f"{name}: every result must be an object")
        target = str(result.get("Target", ""))
        result_class = str(result.get("Class") or "")
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity", "")).upper()
            if severity in TRIVY_NEVER:
                package = str(vulnerability.get("PkgName", target))
                vulnerability_id = str(vulnerability.get("VulnerabilityID", "unknown"))
                findings.append(
                    Finding(
                        "trivy", name, vulnerability_id, package, severity, False,
                        kind="vulnerability",
                        result_class=result_class,
                        fixed_version=str(vulnerability.get("FixedVersion") or ""),
                        status=str(vulnerability.get("Status") or ""),
                    )
                )  # fmt: skip
        for secret in result.get("Secrets") or []:
            severity = str(secret.get("Severity", "")).upper() or "secret"
            rule = str(secret.get("RuleID", "secret"))
            findings.append(Finding("trivy", name, rule, target, severity, False, kind="secret"))
        for misconfiguration in result.get("Misconfigurations") or []:
            severity = str(misconfiguration.get("Severity", "")).upper()
            if severity in TRIVY_NEVER and misconfiguration.get("Status", "FAIL") != "PASS":
                rule = trivy_check_id(str(misconfiguration.get("ID", "unknown")))
                findings.append(
                    Finding("trivy", name, rule, target, severity, False, kind="misconfiguration")
                )
    return findings


_TRIVY_CHECK_ID = re.compile(r"(?:AVD-)?([A-Z]+)-0*(\d+)")


def trivy_check_id(raw: str) -> str:
    """A Dockerfile check's ID in the form the spec and `exceptions.yaml` use (`DS002`). Trivy
    0.76 reports the same check as `DS-0002` (older releases `DS002`, with `AVD-DS-0002` as its
    AVD ID); any other ID is kept as reported."""
    match = _TRIVY_CHECK_ID.fullmatch(raw)
    if match is None:
        return raw
    return f"{match.group(1)}{int(match.group(2)):03d}"


def gitleaks_findings(report: Any, name: str) -> list[Finding]:
    if not isinstance(report, list):
        raise ReportError(f"{name}: a gitleaks report must be a JSON array")
    return [
        Finding(
            "gitleaks",
            name,
            str(item.get("RuleID", "secret")) if isinstance(item, dict) else "secret",
            str(item.get("File", "")) if isinstance(item, dict) else "",
            "secret",
            False,
        )
        for item in report
    ]


def workspace_member_names(root: Path) -> list[str]:
    """Name each workspace member by the last component of its path, as deptry reports are."""
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    members = config.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    return [member.rstrip("/").rsplit("/", 1)[-1] for member in members if isinstance(member, str)]


def expected_reports(job: str, root: Path) -> list[str]:
    images = [name for name, dockerfile in TRIVY_IMAGES.items() if (root / dockerfile).is_file()]
    if job == "images":
        return [*(f"trivy-{image}.json" for image in images), "trivy-config.json"]
    if job == "build":
        return [f"trivy-{image}.json" for image in images]
    packages = [
        name
        for name, directory in NPM_DIRECTORIES.items()
        if (root / directory / "package.json").is_file()
    ]
    return [
        "pip-audit.json",
        *(f"npm-{directory}.json" for directory in packages),
        "zizmor.json",
        *(f"deptry-{member}.json" for member in workspace_member_names(root)),
    ]


def read_report(path: Path, fetch: Callable[[str], dict[str, Any] | None]) -> list[Finding]:
    """Parse one report by its file name and return its findings."""
    name = path.name
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReportError(f"{name}: missing report") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ReportError(f"{name}: not a JSON report: {error}") from error
    if name == "pip-audit.json":
        return pip_audit_findings(report, name, fetch)
    if name.startswith("npm-"):
        return npm_findings(report, name)
    if name == "zizmor.json":
        return zizmor_findings(report, name)
    if name.startswith("deptry-"):
        return deptry_findings(report, name)
    if name.startswith("trivy-"):
        return trivy_findings(report, name)
    if name.startswith("gitleaks"):
        return gitleaks_findings(report, name)
    raise ReportError(f"{name}: no scanner writes a report of this name")


# --- The decision --------------------------------------------------------------------------------


def _waiver_matches(waiver: Waiver, finding: Finding) -> bool:
    values = waiver.values
    if values.get("scanner") != finding.scanner:
        return False
    if values.get("id") not in (finding.id, *finding.aliases):
        return False
    scope = values.get("scope", "")
    if finding.scanner == "pip-audit":
        return fnmatch.fnmatchcase(finding.subject, _normalise_package(scope))
    subjects = (finding.subject, *finding.scopes)
    return any(fnmatch.fnmatchcase(subject, scope) for subject in subjects if subject)


def _unfixed_os_package(finding: Finding, image: str) -> bool:
    """A HIGH or CRITICAL vulnerability of the distribution's packages in the image's own report,
    for which the report names no fix."""
    return (
        finding.scanner == "trivy"
        and finding.kind == "vulnerability"
        and finding.report == f"trivy-{image}.json"
        and finding.result_class == OS_PACKAGES_CLASS
        and finding.severity in TRIVY_NEVER
        and not finding.fixed_version
        and finding.status.lower() != "fixed"
    )


def _exception_matches(exception: NamedException, finding: Finding) -> bool:
    values = exception.values
    check, image = values.get("id", ""), values.get("image", "")
    if (check, image) not in ALLOWED_EXCEPTIONS:
        return False
    if check == UNFIXED_OS_PACKAGES:
        return _unfixed_os_package(finding, image)
    if (finding.scanner, finding.kind, finding.id, finding.severity) != (
        "trivy",
        "misconfiguration",
        check,
        DS002_SEVERITY,
    ):
        return False
    # The image's own report, or the Dockerfile scan's result for that image's Dockerfile only.
    if finding.report == f"trivy-{image}.json":
        return True
    return finding.report == "trivy-config.json" and finding.subject == TRIVY_IMAGES[image]


@dataclass
class GateResult:
    findings: list[tuple[Finding, str, bool]] = field(default_factory=list)
    report_errors: list[str] = field(default_factory=list)
    waivers: list[Waiver] = field(default_factory=list)
    exceptions: list[NamedException] = field(default_factory=list)
    file_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return (
            bool(self.report_errors or self.file_errors)
            or any(not waiver.valid for waiver in self.waivers)
            or any(not exception.valid for exception in self.exceptions)
            or any(failing for _, _, failing in self.findings)
        )


def _never_waivable(
    finding: Finding, exceptions: Sequence[NamedException]
) -> tuple[Finding, str, bool]:
    covering = [exception for exception in exceptions if _exception_matches(exception, finding)]
    valid = [exception for exception in covering if exception.valid]
    if valid:
        exception = valid[0]
        exception.matched.append(f"{finding.report}: {finding.subject}")
        review_by = exception.values["review_by"]
        return finding, f"passes: exception {exception.label}, review by {review_by}", False
    if covering:
        return finding, f"fails: exception {covering[0].label} is invalid", True
    return finding, "fails: high and critical findings can't be waived", True


def decide(
    findings: Iterable[Finding],
    waivers: list[Waiver],
    exceptions: Sequence[NamedException] = (),
) -> list[tuple[Finding, str, bool]]:
    """Return (finding, status, fails) for every finding, marking the waivers and the exception
    it meets."""
    decisions: list[tuple[Finding, str, bool]] = []
    for finding in findings:
        matching = [waiver for waiver in waivers if _waiver_matches(waiver, finding)]
        if not finding.waivable:
            for waiver in matching:
                waiver.not_applicable.append(finding.id)
            decisions.append(_never_waivable(finding, exceptions))
            continue
        valid = [waiver for waiver in matching if waiver.valid]
        if valid:
            for waiver in valid:
                waiver.matched.append(finding.id)
            decisions.append((finding, f"waived by {valid[0].label}", False))
        elif matching:
            decisions.append((finding, f"fails: waiver {matching[0].label} is invalid", True))
        else:
            decisions.append((finding, "fails: no waiver", True))
    return decisions


def load_security_files(
    waivers_path: Path, approvers_path: Path, today: date
) -> tuple[list[Waiver], list[str]]:
    try:
        approvers = load_approvers(approvers_path)
    except (OSError, YamlSubsetError) as error:
        return [], [f"approvers.yaml: {error}"]
    try:
        return load_waivers(waivers_path, approvers, today), []
    except (OSError, YamlSubsetError) as error:
        return [], [f"waivers.yaml: {error}"]


def load_exceptions_file(path: Path, today: date) -> tuple[list[NamedException], list[str]]:
    try:
        return load_exceptions(path, today), []
    except (OSError, YamlSubsetError) as error:
        return [], [f"exceptions.yaml: {error}"]


def run_gate(
    audit_dir: Path,
    report_names: Iterable[str],
    *,
    waivers_path: Path,
    approvers_path: Path,
    exceptions_path: Path,
    today: date,
    fetch: Callable[[str], dict[str, Any] | None],
) -> GateResult:
    result = GateResult()
    result.waivers, result.file_errors = load_security_files(waivers_path, approvers_path, today)
    result.exceptions, exception_errors = load_exceptions_file(exceptions_path, today)
    result.file_errors += exception_errors
    names = list(dict.fromkeys(report_names))
    # gitleaks reports are optional: their step already fails on a finding, and the gate only
    # makes sure no finding in one is ever passed.
    names += sorted(
        path.name for path in audit_dir.glob("gitleaks*.json") if path.name not in names
    )
    findings: list[Finding] = []
    for name in names:
        try:
            findings.extend(read_report(audit_dir / name, fetch))
        except ReportError as error:
            result.report_errors.append(str(error))
    result.findings = decide(findings, result.waivers, result.exceptions)
    return result


# --- Output --------------------------------------------------------------------------------------


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _waiver_status(waiver: Waiver) -> str:
    if not waiver.valid:
        return "invalid: " + "; ".join(waiver.problems)
    parts: list[str] = []
    if waiver.matched:
        parts.append(f"applies to {', '.join(dict.fromkeys(waiver.matched))}")
    if waiver.not_applicable:
        parts.append(f"{NOT_APPLICABLE} ({', '.join(dict.fromkeys(waiver.not_applicable))})")
    return "; ".join(parts) or "valid, matches no finding"


def _exception_status(exception: NamedException) -> str:
    if not exception.valid:
        return "invalid: " + "; ".join(exception.problems)
    if exception.matched and exception.values.get("id") == UNFIXED_OS_PACKAGES:
        count = len(exception.matched)
        report = f"trivy-{exception.values['image']}.json"
        noun = "finding" if count == 1 else "findings"
        return f"applies to {count} unfixed OS-package {noun} in {report}"
    if exception.matched:
        return f"applies to {', '.join(dict.fromkeys(exception.matched))}"
    return "valid, matches no finding"


def expiring(waivers: Iterable[Waiver], today: date) -> list[Waiver]:
    horizon = today + timedelta(days=EXPIRY_WARNING_DAYS)
    return [w for w in waivers if w.valid and w.expires is not None and w.expires <= horizon]


def due_for_review(exceptions: Iterable[NamedException], today: date) -> list[NamedException]:
    horizon = today + timedelta(days=EXPIRY_WARNING_DAYS)
    return [e for e in exceptions if e.valid and e.review_by is not None and e.review_by <= horizon]


def render(result: GateResult, today: date, *, waivers_only: bool = False) -> str:
    lines = ["## Security gate", ""]
    errors = [*result.file_errors, *result.report_errors]
    if errors:
        lines += ["### Errors", "", *(f"- {_cell(error)}" for error in errors), ""]
    if not waivers_only:
        lines += ["### Findings", ""]
        if result.findings:
            lines += [
                "| Scanner | ID | Package or path | Severity | Status |",
                "|---|---|---|---|---|",
            ]
            lines += [
                f"| {f.scanner} | {_cell(f.id)} | {_cell(f.subject)} | {f.severity} | {status} |"
                for f, status, _ in result.findings
            ]
        else:
            lines.append("No findings.")
        lines.append("")
    lines += ["### Waivers", ""]
    if result.waivers:
        lines += [
            "| ID | Scanner | Scope | Expires | Approved by | Status |",
            "|---|---|---|---|---|---|",
        ]
        for waiver in result.waivers:
            values = waiver.values
            cells = [values.get(key, "") for key in ("scanner", "scope", "expires", "approved_by")]
            lines.append(
                f"| {_cell(waiver.label)} | {' | '.join(_cell(cell) for cell in cells)} "
                f"| {_cell(_waiver_status(waiver))} |"
            )
    else:
        lines.append("No waivers.")
    lines += ["", "### Exceptions at high severity", ""]
    if result.exceptions:
        lines += ["| Exception | Decision | Review by | Status |", "|---|---|---|---|"]
        for exception in result.exceptions:
            cells = [exception.values.get(key, "") for key in ("decision", "review_by")]
            lines.append(
                f"| {_cell(exception.label)} | {' | '.join(_cell(cell) for cell in cells)} "
                f"| {_cell(_exception_status(exception))} |"
            )
    else:
        lines.append("None.")
    soon = expiring(result.waivers, today)
    due = due_for_review(result.exceptions, today)
    lines += [
        "",
        f"### Waivers expiring and exceptions due for review within {EXPIRY_WARNING_DAYS} days",
        "",
    ]
    lines += [
        f"- {_cell(w.label)} ({w.values['scanner']}) expires {w.values['expires']}" for w in soon
    ]
    lines += [f"- exception {_cell(e.label)} review due {e.values['review_by']}" for e in due]
    if not soon and not due:
        lines.append("None.")
    lines += ["", "**Result: fail**" if result.failed else "**Result: pass**", ""]
    return "\n".join(lines)


def workflow_commands(result: GateResult, today: date) -> list[str]:
    """GitHub workflow commands for the log: one error per failure, one warning per expiry."""
    lines = [f"::error::{error}" for error in [*result.file_errors, *result.report_errors]]
    lines += [
        f"::error::{f.scanner} {f.id} ({f.subject}, {f.severity}): {status}"
        for f, status, failing in result.findings
        if failing
    ]
    lines += [
        f"::error::waiver {waiver.label}: {'; '.join(waiver.problems)}"
        for waiver in result.waivers
        if not waiver.valid
    ]
    lines += [
        f"::error::exception {exception.label}: {'; '.join(exception.problems)}"
        for exception in result.exceptions
        if not exception.valid
    ]
    lines += [
        f"::warning::waiver {w.label} ({w.values['scanner']}) expires {w.values['expires']}"
        for w in expiring(result.waivers, today)
    ]
    lines += [
        f"::warning::exception {e.label} review due {e.values['review_by']}"
        for e in due_for_review(result.exceptions, today)
    ]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate the scanner reports and the waivers.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--job", choices=("security", "images", "build"), default="security")
    parser.add_argument("--waivers-only", action="store_true")
    args = parser.parse_args(argv)
    root: Path = args.root
    audit_dir: Path = args.audit_dir or root / "build" / "audit"
    security = root / ".github" / "security"
    today = datetime.now(tz=UTC).date()
    if args.waivers_only:
        result = GateResult()
        result.waivers, result.file_errors = load_security_files(
            security / "waivers.yaml", security / "approvers.yaml", today
        )
        result.exceptions, exception_errors = load_exceptions_file(
            security / "exceptions.yaml", today
        )
        result.file_errors += exception_errors
    else:
        try:
            names = expected_reports(args.job, root)
        except (OSError, tomllib.TOMLDecodeError) as error:
            sys.stdout.write(f"::error::can't read the workspace members: {error}\n")
            return 1
        result = run_gate(
            audit_dir,
            names,
            waivers_path=security / "waivers.yaml",
            approvers_path=security / "approvers.yaml",
            exceptions_path=security / "exceptions.yaml",
            today=today,
            fetch=fetch_osv_record,
        )
    markdown = render(result, today, waivers_only=args.waivers_only)
    for line in workflow_commands(result, today):
        sys.stdout.write(line + "\n")
    sys.stdout.write(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
