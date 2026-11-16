"""Write the pull-request what-if change list to the job summary (83-infrastructure.md 10.5).

Step 4 of `preflight-whatif` passes the JSON results of step 3 (`az deployment group what-if
... --no-pretty-print > <file>`, one file per template) and this script writes, per file, the
count of each change type and one row per resource that changes: created, modified (with the
changed property paths), deleted, deployed or unsupported. Microsoft Graph resources come back as
`Unsupported`, because what-if can't evaluate them; their review is the code-owner rule of
section 10.4, so the table names them with the reason what-if gives. Resources with no change are
only counted.

Usage: uv run --frozen python scripts/ci/whatif_summary.py <what-if JSON> [<what-if JSON> ...]
The Markdown goes to `$GITHUB_STEP_SUMMARY` (appended) and to standard output.
Exit codes: 0 summary written; 1 a file can't be read or holds a failed what-if.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ORDER = ("Create", "Modify", "Delete", "Deploy", "Unsupported", "NoChange", "Ignore")
LISTED = frozenset({"Create", "Modify", "Delete", "Deploy", "Unsupported"})
MAX_PATHS = 12
# A provider part holds at least a namespace, a type and a name.
MIN_PROVIDER_SEGMENTS = 3


class WhatIfFormatError(ValueError):
    """The file isn't a what-if result."""


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def resource_label(resource_id: str) -> tuple[str, str]:
    """Split a resource ID into (type, name), e.g. ('Microsoft.Web/sites', 'app-x')."""
    _, separator, provider_part = resource_id.rpartition("/providers/")
    if not separator:
        return "", resource_id
    segments = [segment for segment in provider_part.split("/") if segment]
    if len(segments) < MIN_PROVIDER_SEGMENTS:
        return "", resource_id
    namespace, rest = segments[0], segments[1:]
    types = rest[0::2]
    names = rest[1::2]
    return f"{namespace}/{'/'.join(types)}", "/".join(names)


def _changed_paths(delta: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if not isinstance(delta, list):
        return paths
    for item in delta:
        if not isinstance(item, dict) or item.get("propertyChangeType") == "NoEffect":
            continue
        path = f"{prefix}{item.get('path', '')}"
        children = _changed_paths(item.get("children"), f"{path}.")
        paths.extend(children or [path])
    return paths


def _detail(change: dict[str, Any]) -> str:
    if change.get("changeType") == "Unsupported":
        return str(change.get("unsupportedReason") or "what-if can't evaluate this resource")
    paths = _changed_paths(change.get("delta"))
    if not paths:
        return ""
    shown = ", ".join(paths[:MAX_PATHS])
    more = len(paths) - MAX_PATHS
    return f"{shown} and {more} more" if more > 0 else shown


def load_result(path: Path) -> dict[str, Any]:
    """Read a what-if result, accepting the operation result alone or under `properties`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("properties"), dict):
        data = data["properties"]
    if not isinstance(data, dict) or not isinstance(data.get("changes", []), list):
        raise WhatIfFormatError(f"{path}: not a what-if result with a changes list")
    return data


def summarise(label: str, result: dict[str, Any]) -> tuple[str, bool]:
    """Return the Markdown for one what-if result and whether it reports a failure."""
    lines = [f"### What-if: {label}", ""]
    failed = bool(result.get("error")) or str(result.get("status", "")).lower() == "failed"
    if failed:
        error = result.get("error") or {}
        code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
        lines += [f"What-if failed: `{code}`.", ""]
    changes = [change for change in result.get("changes", []) if isinstance(change, dict)]
    counts = dict.fromkeys(ORDER, 0)
    for change in changes:
        change_type = str(change.get("changeType", ""))
        counts[change_type] = counts.get(change_type, 0) + 1
    totals = ", ".join(f"{name}: {count}" for name, count in counts.items() if count)
    if totals:
        lines += [totals, ""]
    rows = []
    for change in changes:
        change_type = str(change.get("changeType", ""))
        if change_type not in LISTED:
            continue
        resource_type, name = resource_label(str(change.get("resourceId", "")))
        rank = ORDER.index(change_type)
        rows.append((rank, resource_type, name, change_type, _detail(change)))
    if rows:
        lines += ["| Change | Resource type | Name | Detail |", "|---|---|---|---|"]
        for _, resource_type, name, change_type, detail in sorted(rows):
            lines.append(
                f"| {change_type} | {_cell(resource_type)} | {_cell(name)} | {_cell(detail)} |"
            )
    else:
        lines.append("No resource changes.")
    if counts.get("Unsupported"):
        lines += [
            "",
            (
                "What-if can't evaluate the unsupported resources (Microsoft Graph among them); "
                "their changes rely on code-owner review."
            ),
        ]
    lines.append("")
    return "\n".join(lines), failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the what-if change list to the summary.")
    parser.add_argument("results", nargs="+", type=Path)
    args = parser.parse_args(argv)
    sections = ["## Infrastructure what-if", ""]
    failed = False
    for path in args.results:
        try:
            section, result_failed = summarise(path.stem, load_result(path))
        except (OSError, json.JSONDecodeError, WhatIfFormatError) as error:
            section, result_failed = f"### What-if: {path.stem}\n\nUnreadable: {error}\n", True
        sections.append(section)
        failed = failed or result_failed
    markdown = "\n".join(sections)
    sys.stdout.write(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
