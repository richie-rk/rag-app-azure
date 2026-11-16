"""The query check T-499 (`84-testing.md` sections 1.6 and 5.10; `85-observability.md` section 5.11;
`90-build-order.md` section 1.3.1 step 3): every workbook tile query and every alert query runs
without a KQL error against `log-docchat-dev` through the Log Analytics query API.

Live level: run by the stage's performer at each stage acceptance with
`uv run pytest -m live --live`, signed in with `az login` as an operator whose Log Analytics
Reader on `log-docchat-dev` comes from `sg-docchat-operators`, with `.azure/dev/.env` refreshed.
The queries are the ones `scripts/ci/check_workbook_queries.py` collects: every KQL tile of every
`*.workbook.json` with the deployed environment's values in its placeholders, and the KQL of every
log search alert deployed in `rg-docchat-dev`, read back through Azure Resource Manager. Each runs
through `docchat_testing.logquery.query_logs` with timespan `P1D`; a query with no rows passes,
because a quiet day is valid.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import pytest

from docchat_testing.logquery import LogQueryError, query_logs

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
SCRIPTS_CI: Final = str(REPOSITORY_ROOT / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_workbook_queries  # noqa: E402

pytestmark = pytest.mark.live

ENVIRONMENT: Final = "dev"
TIMESPAN: Final = "P1D"


def dev_queries() -> tuple[str, list[check_workbook_queries.Query]]:
    """The workspace ID and every tile and alert query, as the script collects them."""
    settings = check_workbook_queries.read_azd_env(
        REPOSITORY_ROOT / ".azure" / ENVIRONMENT / ".env"
    )
    values = check_workbook_queries.placeholders(settings, ENVIRONMENT)
    queries = check_workbook_queries.workbook_queries(
        REPOSITORY_ROOT / check_workbook_queries.WORKBOOKS, values
    )
    queries += check_workbook_queries.alert_queries(
        check_workbook_queries.list_alert_rules(
            settings["AZURE_SUBSCRIPTION_ID"], settings["AZURE_RESOURCE_GROUP"]
        )
    )
    return settings["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"], queries


@pytest.fixture(scope="module")
def collected() -> tuple[str, list[check_workbook_queries.Query]]:
    # The alert rules are read with `az rest` in a child process, outside any event loop.
    return dev_queries()


async def test_t499_every_tile_and_alert_query_runs_against_log_docchat_dev(
    collected: tuple[str, list[check_workbook_queries.Query]],
) -> None:
    workspace, queries = collected
    assert any(query.source == "alert" for query in queries), "no alert rule was read back"
    failures: list[str] = []
    for query in queries:
        place = (
            f"alert {query.name}" if query.source == "alert" else f"{query.source} / {query.name}"
        )
        try:
            await query_logs(workspace, query.text, TIMESPAN)
        except LogQueryError as error:
            failures.append(f"{place}: {error}")

    assert failures == [], "\n".join(failures)
