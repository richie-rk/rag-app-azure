"""Check the compiled templates for preview APIs and AVM telemetry (83-infrastructure.md 8.6).

Reads the compiled `main.json` and `shared.json` (ARM JSON written by `az bicep build`) and fails
when a resource, nested module resources included, uses an API version ending in `-preview` other
than `Microsoft.Insights/diagnosticSettings@2021-05-01-preview`, the one entry of the preview
register (section 8.7), or when an Azure Verified Module call doesn't pass `enableTelemetry: false`.

Bicep inlines every module as a `Microsoft.Resources/deployments` resource with its template under
`properties.template`. An AVM module is recognised by its `enableTelemetry` parameter, which every
AVM module declares with the default `true`; the call must pass the literal `false`, or the module
deploys its telemetry deployment. API versions inside template expressions (`reference(...)`,
`list*(...)`) are checked too, because an `existing` resource can compile to one.

Usage: uv run --frozen python scripts/ci/check_bicep_modules.py [infra/main.json infra/shared.json]
Exit codes: 0 both rules hold; 1 a rule is broken or a template can't be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATES = ("infra/main.json", "infra/shared.json")
DEPLOYMENTS = "microsoft.resources/deployments"
ALLOWED_PREVIEW = ("microsoft.insights/diagnosticsettings", "2021-05-01-preview")
EXPRESSION_PREVIEW = re.compile(r"'(\d{4}-\d{2}-\d{2}(?:-[A-Za-z0-9]+)*-preview)'", re.IGNORECASE)


def _resources(template: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (label, resource) for a template's resources, symbolic or array form."""
    resources = template.get("resources", [])
    if isinstance(resources, dict):
        for symbolic_name, resource in resources.items():
            if isinstance(resource, dict):
                yield str(symbolic_name), resource
    elif isinstance(resources, list):
        for index, resource in enumerate(resources):
            if isinstance(resource, dict):
                yield str(resource.get("name", f"resources[{index}]")), resource


def _is_allowed_preview(resource_type: str, api_version: str) -> bool:
    return (resource_type.lower(), api_version.lower()) == ALLOWED_PREVIEW


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _without_nested_template(resource: dict[str, Any]) -> dict[str, Any]:
    """Return the resource without `properties.template`, which is checked on its own."""
    own = {key: value for key, value in resource.items() if key != "properties"}
    properties = resource.get("properties")
    if isinstance(properties, dict):
        own["properties"] = {key: value for key, value in properties.items() if key != "template"}
    return own


def _expression_problems(value: Any, where: str) -> list[str]:
    """Find preview API versions used inside the template expressions of `value`."""
    problems: list[str] = []
    for text in _strings(value):
        if not text.startswith("["):
            continue
        for version in EXPRESSION_PREVIEW.findall(text):
            if version.lower() == ALLOWED_PREVIEW[1] and ALLOWED_PREVIEW[0] in text.lower():
                continue
            problems.append(f"{where}: an expression uses the preview API version {version}")
    return problems


def _telemetry_problem(resource: dict[str, Any], where: str) -> str | None:
    properties = resource.get("properties", {})
    template = properties.get("template") if isinstance(properties, dict) else None
    if not isinstance(template, dict):
        return None
    declared = template.get("parameters", {})
    if not isinstance(declared, dict) or "enableTelemetry" not in declared:
        return None
    passed = properties.get("parameters", {})
    argument = passed.get("enableTelemetry") if isinstance(passed, dict) else None
    if isinstance(argument, dict) and argument.get("value") is False:
        return None
    return f"{where}: Azure Verified Module call without enableTelemetry: false"


def template_problems(template: dict[str, Any], where: str) -> list[str]:
    """Check one template and every module template inlined below it."""
    problems: list[str] = []
    for label, resource in _resources(template):
        location = f"{where}/{label}"
        resource_type = str(resource.get("type", ""))
        api_version = str(resource.get("apiVersion", ""))
        if api_version.lower().endswith("-preview") and not _is_allowed_preview(
            resource_type, api_version
        ):
            problems.append(f"{location}: {resource_type}@{api_version} is a preview API version")
        problems.extend(_expression_problems(_without_nested_template(resource), location))
        if resource_type.lower() != DEPLOYMENTS:
            continue
        telemetry = _telemetry_problem(resource, location)
        if telemetry:
            problems.append(telemetry)
        properties = resource.get("properties", {})
        nested = properties.get("template") if isinstance(properties, dict) else None
        if isinstance(nested, dict):
            problems.extend(template_problems(nested, location))
    rest = {key: value for key, value in template.items() if key != "resources"}
    problems.extend(_expression_problems(rest, where))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check compiled templates for preview APIs.")
    parser.add_argument(
        "templates",
        nargs="*",
        type=Path,
        default=[REPOSITORY_ROOT / path for path in DEFAULT_TEMPLATES],
    )
    args = parser.parse_args(argv)
    problems: list[str] = []
    for path in args.templates:
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{path}: can't read the compiled template: {error}")
            continue
        if not isinstance(template, dict):
            problems.append(f"{path}: not an ARM template")
            continue
        problems.extend(template_problems(template, path.name))
    for problem in problems:
        sys.stdout.write(problem + "\n")
    if problems:
        return 1
    sys.stdout.write(
        "no preview API version outside the register, and every AVM call sets "
        "enableTelemetry: false\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
