"""`load_dispatch_settings` (`14-ingestion.md` section 5.2; part of T-154): the area's snapshot as
its dotted keys, or `valid: false` with the invalid keys reported once per wake."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import azure.durable_functions as df

from docchat_core.sql.engine import SqlUnavailable
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import load_dispatch_settings as activity


def _provider(process: Any) -> Any:
    async def provide() -> Any:
        return process

    return provide


def _events(exporters: TestExporters, name: str) -> list[dict[str, Any]]:
    return [
        dict(item.log_record.attributes or {})
        for item in exporters.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name") == name
    ]


async def test_a_valid_area_is_returned_as_its_dotted_keys(fakes: SimpleNamespace) -> None:
    process = fakes.process(ops=fakes.FakeOps(fakes.settings(max_parallel_items=4)))

    result = await activity.run({}, _provider(process))

    assert result["ok"] is True
    assert result["valid"] is True
    assert result["settings"]["ingestion.max_parallel_items"] == 4
    assert len(result["settings"]) == 25


async def test_t154_an_invalid_area_reports_each_key_and_returns_no_settings(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    ops = fakes.FakeOps(invalid=("ingestion.max_parallel_items",))
    process = fakes.process(ops=ops)

    result = await activity.run({}, _provider(process))

    assert (result["ok"], result["valid"], result["settings"]) == (True, False, None)
    assert _events(telemetry, "ops_config_invalid") == [
        {
            "microsoft.custom_event.name": "ops_config_invalid",
            "area": "ingestion",
            "key": "ingestion.max_parallel_items",
        }
    ]
    data: Any = telemetry.metrics.get_metrics_data()
    points = [
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    assert "docchat.config.invalid" in points


async def test_an_area_that_never_validated_is_not_valid(fakes: SimpleNamespace) -> None:
    ops = fakes.FakeOps()
    ops.empty = True

    result = await activity.run({}, _provider(fakes.process(ops=ops)))

    assert (result["valid"], result["settings"]) == (False, None)


async def test_an_unreachable_database_is_a_failed_result(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    ops = fakes.FakeOps()
    ops.failure = SqlUnavailable("connection")

    result = await activity.run(None, _provider(fakes.process(ops=ops)))

    assert result["ok"] is False
    assert result["errorCode"] == "internal_error"


async def test_an_input_other_than_an_empty_object_is_refused(fakes: SimpleNamespace) -> None:
    result = await activity.run({"unexpected": 1}, _provider(fakes.process()))

    assert result["errorCode"] == "internal_error"


def test_the_function_is_registered_on_its_blueprint() -> None:
    app = df.DFApp()
    app.register_functions(activity.bp)
    assert {function.get_function_name() for function in app.get_functions()} == {
        "load_dispatch_settings"
    }
