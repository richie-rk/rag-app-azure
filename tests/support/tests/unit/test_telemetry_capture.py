"""Telemetry capture and the canary audit: the serialisation and the leak search, and T-562, the
canary audit meta-test (`84-testing.md` section 2.10), which runs nested sessions in their own
processes, because the OpenTelemetry providers can be set only once per process."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Link, Status, StatusCode

from docchat_core.telemetry import TestExporters
from docchat_testing.builders.canaries import CANARY_KINDS, CanaryKind, canary
from docchat_testing.telemetry_capture import (
    CAPTURE_KEY,
    SESSION_ENVIRONMENT,
    Finding,
    TelemetryCapture,
    TelemetryItem,
    _session_environment,
    find_leaks,
    format_findings,
    session_capture,
)

pytest_plugins = ("pytester",)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CANARY = "cnry-question-0a1b2c3d"
EMAIL = "someone" + "@" + "example.org"
JWT = "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12


def _repository_ini() -> str:
    """The repository's own pytest configuration, so nested sessions run as real ones do."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options: dict[str, Any] = pyproject["tool"]["pytest"]["ini_options"]
    lines = ["[pytest]"]
    for key, value in options.items():
        if key == "testpaths":
            continue
        if key == "addopts":
            lines.append("addopts = " + " ".join(shlex.quote(option) for option in value))
        elif isinstance(value, list):
            lines.append(f"{key} =")
            lines.extend(f"    {entry}" for entry in value)
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def local_capture() -> TelemetryCapture:
    """A capture fed by its own providers, so these tests leave the process's providers alone."""
    return TelemetryCapture(TestExporters())


def _tracer(capture: TelemetryCapture) -> Any:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(capture.exporters.spans))
    return provider.get_tracer("docchat.meta")


def _logger(capture: TelemetryCapture) -> Any:
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(capture.exporters.logs))
    return provider.get_logger("docchat.meta")


# The leak search


@pytest.mark.parametrize("kind", CANARY_KINDS)
def test_every_builder_canary_is_found(kind: CanaryKind) -> None:
    item = TelemetryItem(label="span 'x'", strings=(("attributes.k", canary(kind)),))

    assert "canary" in {finding.kind for finding in find_leaks(item)}


@pytest.mark.parametrize(
    ("text", "kind", "excerpt"),
    [
        (f"why {CANARY}?", "canary", CANARY + "?"),
        (f"mail {EMAIL} now", "e-mail address", EMAIL),
        (f"Bearer {JWT}", "JWT", JWT),
    ],
)
def test_find_leaks_names_the_kind_and_the_excerpt(text: str, kind: str, excerpt: str) -> None:
    item = TelemetryItem(label="span 'x'", strings=(("attributes.k", text),))

    assert find_leaks(item) == [Finding("span 'x'", "attributes.k", kind, excerpt)]  # type: ignore[arg-type]


def test_find_leaks_passes_metadata() -> None:
    item = TelemetryItem(
        label="span 'docchat.chat.turn'",
        strings=(
            ("attributes.docchat.session_id", "0f8fad5b-d9cb-469f-a165-70867728950e"),
            ("attributes.docchat.outcome", "ok"),
            ("attributes.url.full", "https://h/v1/chat/turns"),
            ("attributes.redacted", "[redacted:email] [redacted:jwt]"),
        ),
    )

    assert find_leaks(item) == []


def test_one_string_can_leak_several_ways() -> None:
    item = TelemetryItem(label="log record #0", strings=(("body", f"{CANARY} {EMAIL}"),))

    assert [finding.kind for finding in find_leaks(item)] == ["canary", "e-mail address"]


def test_the_report_names_each_item() -> None:
    report = format_findings(
        [
            Finding("span 'a'", "attributes.k", "canary", CANARY),
            Finding("log record #3", "body", "JWT", JWT),
        ]
    )

    assert report.splitlines() == [
        "Canary audit: 2 exported telemetry string(s) hold content:",
        f"  span 'a': attributes.k holds a canary ({CANARY!r})",
        f"  log record #3: body holds a JWT ({JWT!r})",
    ]


# The serialisation of each exported item


def test_a_span_is_read_with_its_events_status_and_links(local_capture: TelemetryCapture) -> None:
    tracer = _tracer(local_capture)
    with tracer.start_as_current_span("docchat.linked") as linked:
        link_context = linked.get_span_context()
    with tracer.start_as_current_span(
        "docchat.meta.span", links=[Link(link_context, {"docchat.reason": CANARY})]
    ) as span:
        span.set_attribute("docchat.clean", "ok")
        span.add_event(
            "exception",
            {"exception.message": f"failed {EMAIL}", "exception.stacktrace": "Traceback ..."},
        )
        span.set_status(Status(StatusCode.ERROR, JWT))

    findings = local_capture.audit()

    assert {(finding.where, finding.kind) for finding in findings} == {
        ("events[0] exception.exception.message", "e-mail address"),
        ("status description", "JWT"),
        ("links[0].docchat.reason", "canary"),
    }
    assert all(finding.item.startswith("span 'docchat.meta.span' trace ") for finding in findings)


def test_a_span_name_and_attribute_are_read(local_capture: TelemetryCapture) -> None:
    tracer = _tracer(local_capture)
    with tracer.start_as_current_span(f"docchat.{CANARY}") as span:
        span.set_attribute("docchat.tags", ["fine", CANARY])

    assert [finding.where for finding in local_capture.audit()] == [
        "name",
        "attributes.docchat.tags[1]",
    ]


def test_a_log_record_body_and_attributes_are_read(local_capture: TelemetryCapture) -> None:
    from opentelemetry._logs import LogRecord  # noqa: PLC0415 - the API record type only here

    logger = _logger(local_capture)
    logger.emit(LogRecord(body="job_failed", attributes={"docchat.note": CANARY}))
    logger.emit(LogRecord(body={"nested": [EMAIL]}))

    findings = local_capture.audit()

    assert [(f.item, f.where, f.kind) for f in findings] == [
        ("log record #0 from 'docchat.meta'", "attributes.docchat.note", "canary"),
        ("log record #1 from 'docchat.meta'", "body.nested[0]", "e-mail address"),
    ]


def test_metric_data_point_attributes_are_read(local_capture: TelemetryCapture) -> None:
    meter = MeterProvider(metric_readers=[local_capture.exporters.metrics]).get_meter("m")
    counter = meter.create_counter("docchat.meta.count")
    counter.add(1, {"outcome": "ok"})
    counter.add(1, {"outcome": CANARY})

    findings = local_capture.audit()

    assert [(f.item, f.where) for f in findings] == [
        ("metric 'docchat.meta.count' data point #1", "attributes.outcome")
    ]


def test_each_record_is_serialised_once_and_a_cleared_exporter_starts_afresh(
    local_capture: TelemetryCapture,
) -> None:
    tracer = _tracer(local_capture)
    with tracer.start_as_current_span("docchat.meta.first"):
        pass
    (first,) = local_capture.items()
    with tracer.start_as_current_span(f"docchat.{CANARY}"):
        pass

    again, second = local_capture.items()

    assert again is first
    assert second.label.startswith(f"span 'docchat.{CANARY}'")
    local_capture.exporters.spans.clear()
    with tracer.start_as_current_span("docchat.meta.third"):
        pass
    (third,) = local_capture.items()
    assert third.label.startswith("span 'docchat.meta.third'")


def test_an_empty_capture_is_clean(local_capture: TelemetryCapture) -> None:
    assert local_capture.audit() == []
    assert local_capture.spans() == ()
    assert local_capture.log_records() == ()


# The session's capture


def test_the_session_environment_is_supplied_only_while_configuring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in SESSION_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCCHAT_BUILD_COMMIT", "f" * 40)

    with _session_environment():
        inside = {name: os.environ[name] for name in SESSION_ENVIRONMENT}

    assert inside == {
        "DOCCHAT_CORE_ENVIRONMENT": "local",
        "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000000",
        "DOCCHAT_BUILD_COMMIT": "f" * 40,
    }
    assert "DOCCHAT_CORE_ENVIRONMENT" not in os.environ
    assert os.environ["DOCCHAT_BUILD_COMMIT"] == "f" * 40


def test_session_capture_reuses_the_installed_capture(local_capture: TelemetryCapture) -> None:
    # A stand-in config, so the session's own capture stays where it is.
    config = SimpleNamespace(stash=pytest.Stash())
    config.stash[CAPTURE_KEY] = local_capture

    assert session_capture(cast("pytest.Config", config)) is local_capture


# T-562

_CANARY_TEST = f"""
from opentelemetry import trace


def test_exports_a_canary() -> None:
    with trace.get_tracer("docchat.meta").start_as_current_span("docchat.meta.leaky") as span:
        span.set_attribute("docchat.question", "{CANARY}")
"""

_CLEAN_TEST = """
from opentelemetry import trace


def test_exports_metadata_only(telemetry_capture) -> None:
    with trace.get_tracer("docchat.meta").start_as_current_span("docchat.meta.clean") as span:
        span.set_attribute("docchat.outcome", "ok")
    names = [span.name for span in telemetry_capture.spans()]
    assert "docchat.meta.clean" in names
"""


def _integration_session(pytester: pytest.Pytester, source: str) -> pytest.RunResult:
    pytester.makeini(_repository_ini())
    directory = pytester.path / "tests" / "integration"
    directory.mkdir(parents=True)
    (directory / "test_telemetry.py").write_text(source, encoding="utf-8")
    return pytester.runpytest_subprocess()


def test_t562_the_canary_audit_fails_the_session_and_names_the_span(
    pytester: pytest.Pytester,
) -> None:
    result = _integration_session(pytester, _CANARY_TEST)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    finding = (
        "*span 'docchat.meta.leaky' trace * attributes.docchat.question holds a canary "
        f"('{CANARY}')"
    )
    result.stdout.fnmatch_lines(
        ["*canary audit*", "Canary audit: 1 exported telemetry string(s) hold content:", finding]
    )


def test_t562_a_clean_integration_session_passes_the_audit(pytester: pytest.Pytester) -> None:
    result = _integration_session(pytester, _CLEAN_TEST)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
    assert "canary audit" not in result.stdout.str()


def test_a_unit_session_installs_no_capture(pytester: pytest.Pytester) -> None:
    pytester.makeini(_repository_ini())
    pytester.makepyfile(
        test_unit="""
from docchat_testing.telemetry_capture import CAPTURE_KEY


def test_no_capture(request) -> None:
    assert request.config.stash.get(CAPTURE_KEY, None) is None
"""
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)


def test_the_log_records_of_the_docchat_logger_are_audited(pytester: pytest.Pytester) -> None:
    source = f"""
import logging


def test_logs_a_canary() -> None:
    logging.getLogger("docchat").warning("job_failed", extra={{"note": "{CANARY}"}})
"""
    result = _integration_session(pytester, source)

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines([f"*log record #0 * holds a canary ('{CANARY}')"])
