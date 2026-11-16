"""The attribute registry against the catalogue of `85-observability.md` section 4 (T-483 for the
event declarations, T-484 for the dimension set, T-485 for the catalogue)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from opentelemetry.trace import SpanKind

from docchat_core.errors import CODES, ITEM_CODES
from docchat_core.telemetry.attributes import (
    DIMENSIONS,
    EVENTS,
    KEYS,
    LOG_EVENTS,
    MEMORY_REJECTION_REASONS,
    METRICS,
    SERVER_SPAN_ATTRIBUTES,
    SPANS,
    SQL_SPAN_FAMILY,
    InstrumentKind,
    ValueKind,
    ValueSpec,
    is_camel_case,
    is_event_name,
    span_spec,
    value_spec,
)

type SpecTable = Callable[[str, str], list[list[str]]]

_SPEC = "85-observability.md"
_TOKEN = re.compile(r"`([^`]+)`")
_PARENTHESISED = re.compile(r"\([^()]*\)")
_LEVELS = {"error": 40, "warning": 30, "information": 20}
_KINDS = {"INTERNAL": SpanKind.INTERNAL, "CLIENT": SpanKind.CLIENT, "CONSUMER": SpanKind.CONSUMER}
_IDENTIFIER_DIMENSIONS = {
    "user_id",
    "oid",
    "project_id",
    "session_id",
    "turn_id",
    "document_id",
    "version_id",
    "run_id",
    "item_id",
    "index_id",
    "physical_index_id",
    "chunk_set_id",
}
_SPAN_TABLES = (
    "### 4.2 Instrumentation and shared spans",
    "### 4.3 Identity and access spans",
    "### 4.4 Chat pipeline spans",
    "### 4.5 Conversations and memory spans",
    "### 4.6 Ingestion spans",
)
_METRIC_TABLES = (
    "### 4.7 Runtime, SQL and telemetry metrics",
    "### 4.8 Identity and access metrics",
    "### 4.9 Chat pipeline metrics",
    "### 4.10 Conversations and memory metrics",
    "### 4.11 Ingestion metrics",
)


def _tokens(cell: str) -> list[str]:
    # `spec_table` unwraps a cell that is one code span, so a bare name is one token too.
    if "`" not in cell and re.fullmatch(r"[A-Za-z_][\w.<>-]*", cell) and cell != "none":
        return [cell]
    return _TOKEN.findall(cell)


def _without_values(cell: str) -> str:
    """A cell with its parenthesised value lists and any `: values` tail removed, leaving the
    names it declares."""
    previous = None
    while previous != cell:
        previous, cell = cell, _PARENTHESISED.sub("", cell)
    outside_code = re.sub(r"`[^`]*`", lambda match: "x" * len(match.group()), cell)
    colon = outside_code.find(":")
    return cell if colon < 0 else cell[:colon]


def _attribute_keys(cell: str) -> set[str]:
    keys: set[str] = set()
    for token in _tokens(cell):
        if token == "docchat.memory.rejected.<reason>":
            keys.update(f"docchat.memory.rejected.{reason}" for reason in MEMORY_REJECTION_REASONS)
        elif token.startswith("docchat.") or token in KEYS:
            keys.add(token)
    return keys


def _outcome_values(cell: str) -> set[str]:
    match = re.search(r"`docchat\.outcome` \(([^)]*)\)", cell)
    return set() if match is None else set(_tokens(match.group(1)))


def _spec_spans(spec_table: SpecTable) -> dict[str, tuple[str, str]]:
    spans: dict[str, tuple[str, str]] = {}
    for heading in _SPAN_TABLES:
        for row in spec_table(_SPEC, heading):
            attributes = row[3] if heading.startswith("### 4.6") else row[2]
            names = [name for name in _tokens(row[0]) if name.startswith("docchat.")]
            if not names and row[0].startswith("docchat."):
                names = [row[0]]
            if names and "<" in names[0]:
                names = [SQL_SPAN_FAMILY]
            for name in names:
                spans[name] = (row[1], attributes)
    return spans


# T-485: the registry equals the catalogue.


def test_t485_every_span_of_the_catalogue_is_registered_and_no_other(spec_table: SpecTable) -> None:
    assert set(_spec_spans(spec_table)) == set(SPANS)


def test_t485_each_span_has_its_kind_and_exactly_its_attributes(spec_table: SpecTable) -> None:
    for name, (kind, attributes) in _spec_spans(spec_table).items():
        spec = SPANS[name]
        assert spec.kind is _KINDS[kind.split(",")[0]], name
        assert spec.root == kind.endswith("root"), name
        assert spec.attributes == _attribute_keys(attributes), name


def test_t485_each_span_allows_the_outcomes_the_catalogue_lists(spec_table: SpecTable) -> None:
    for name, (_, attributes) in _spec_spans(spec_table).items():
        listed = _outcome_values(attributes)
        allowed = SPANS[name].values.get("docchat.outcome", frozenset())
        assert listed <= allowed, name
        if listed and "or the" not in attributes:
            assert allowed == listed, name


def test_t485_every_log_record_of_the_catalogue_is_registered(spec_table: SpecTable) -> None:
    records: dict[str, tuple[str, str]] = {}
    for row in spec_table(_SPEC, "### 4.13 Log records"):
        for name in _tokens(row[0]):
            records[name] = (row[1], row[2])

    assert set(records) == set(LOG_EVENTS)
    for name, (level, keys) in records.items():
        assert LOG_EVENTS[name].level == _LEVELS[level], name
        assert LOG_EVENTS[name].keys == _attribute_keys(keys), name


def test_t483_every_custom_event_is_declared_with_exactly_its_properties(
    spec_table: SpecTable,
) -> None:
    events = {
        _tokens(row[0])[0]: set(_tokens(_without_values(row[1])))
        for row in spec_table(_SPEC, "### 4.12 Custom events")
    }

    assert set(events) == set(EVENTS)
    for name, properties in events.items():
        assert set(EVENTS[name].properties) == properties, name


def test_t485_every_metric_has_its_kind_unit_and_dimensions(spec_table: SpecTable) -> None:
    metrics: dict[str, tuple[str, str, set[str]]] = {}
    for heading in _METRIC_TABLES:
        for row in spec_table(_SPEC, heading):
            kind, unit = row[1].split(", ")
            dimensions = set(_tokens(_without_values(row[2])))
            metrics[row[0]] = (kind, unit.strip("`"), dimensions)

    assert set(metrics) == set(METRICS)
    for name, (kind, unit, dimensions) in metrics.items():
        assert METRICS[name].kind == InstrumentKind(kind), name
        assert METRICS[name].unit == unit, name
        assert METRICS[name].dimensions == dimensions, name


def test_t484_the_dimension_set_is_the_closed_set() -> None:
    spec = next(
        parent / "docs" / "spec" / _SPEC
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "spec").is_dir()
    )
    lines = spec.read_text(encoding="utf-8").splitlines()
    paragraph = lines[lines.index("### 3.5 Metric dimensions") + 2]
    listed = paragraph.split(": ", 1)[1].split(". Each instrument")[0]

    assert set(_tokens(listed)) == DIMENSIONS


# T-484 at the registry: every declared dimension is in the closed set and none is an identifier.


def test_t484_every_metric_declares_only_closed_set_dimensions() -> None:
    for name, spec in METRICS.items():
        assert spec.dimensions <= DIMENSIONS, name
        assert not spec.dimensions & _IDENTIFIER_DIMENSIONS, name
    assert not DIMENSIONS & _IDENTIFIER_DIMENSIONS


def test_metric_names_follow_the_naming_rule() -> None:
    for name in METRICS:
        assert re.fullmatch(r"docchat\.[a-z_]+(\.[a-z_]+)+", name), name
        assert not name.endswith("_total"), name


# The registry's own rules.


def test_every_span_and_record_key_is_registered() -> None:
    used = {key for spec in SPANS.values() for key in spec.attributes}
    used |= {key for spec in LOG_EVENTS.values() for key in spec.keys}
    used |= SERVER_SPAN_ATTRIBUTES

    assert used <= set(KEYS)


def test_narrowed_values_belong_to_their_key() -> None:
    narrowed = [spec.values for spec in SPANS.values()]
    narrowed += [spec.values for spec in LOG_EVENTS.values()]
    for values_by_key in narrowed:
        for key, values in values_by_key.items():
            for value in values:
                assert KEYS[key].accepts(value), (key, value)


def test_keys_follow_the_naming_rule() -> None:
    for key in KEYS:
        if key.startswith("docchat."):
            segments = key.split(".")[1:]
            assert 1 <= len(segments) <= 3, key
            assert all(re.fullmatch(r"[a-z][a-z0-9_]*", part) for part in segments), key


def test_string_keys_are_only_the_allowed_ones() -> None:
    strings = {key for key, spec in KEYS.items() if spec.kind is ValueKind.STRING}

    assert strings == {
        "docchat.model",
        "error.type",
        "http.route",
        "exception.type",
        "exception.message",
        "exception.stacktrace",
    }


def test_event_properties_are_lower_camel_case() -> None:
    for name, spec in EVENTS.items():
        assert is_event_name(name), name
        for prop in spec.properties:
            assert is_camel_case(prop), (name, prop)


def test_the_error_codes_are_both_catalogues() -> None:
    error_code = KEYS["docchat.error_code"]

    assert all(error_code.accepts(code) for code in CODES)
    assert all(error_code.accepts(code) for code in ITEM_CODES)
    for internal in ("model_unavailable", "content_filtered", "invalid_output", "store_conflict"):
        assert error_code.accepts(internal)
    assert not error_code.accepts("no_such_code")


def test_the_outcome_key_allows_every_span_outcome() -> None:
    outcome = KEYS["docchat.outcome"]

    for value in ("ok", "accepted", "replay", "deferred_screening", "nothing_to_fold"):
        assert outcome.accepts(value)
    assert not outcome.accepts("cnry-question-0a1b2c3d")


def test_span_spec_resolves_sql_spans() -> None:
    assert span_spec("docchat.sql.ensure_user") is SPANS[SQL_SPAN_FAMILY]
    assert span_spec("docchat.chat.turn") is SPANS["docchat.chat.turn"]
    assert span_spec("docchat.sql.Ensure-User") is None
    assert span_spec("docchat.sql.") is None
    assert span_spec("docchat.unknown") is None


def test_value_spec_narrows_by_span() -> None:
    spec = value_spec("docchat.outcome", SPANS["docchat.chat.turn"].values)

    assert spec is not None
    assert spec.accepts("completed")
    assert not spec.accepts("ok")
    assert value_spec("docchat.unregistered") is None


# Value types.

_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("docchat.project_id", _UUID, _UUID.upper()),
        ("docchat.project_id", _UUID, "not-a-uuid"),
        ("docchat.attempts", 1, 0),
        ("docchat.attempts", 100, 101),
        ("docchat.attempts", 3, True),
        ("docchat.attempts", 3, 3.0),
        ("docchat.attempts", 3, "3"),
        ("docchat.dimensions", 1024, 512),
        ("docchat.cosmos.request_charge", 2.5, -0.1),
        ("docchat.cosmos.request_charge", 3, float("nan")),
        ("docchat.cosmos.request_charge", 0.0, False),
        ("docchat.broadened", True, 1),
        ("docchat.finish_reason", "stop", "STOP"),
        ("docchat.deployment", "chat-small", "chat small"),
        ("docchat.deployment", "embed-ingest-te3-large-v1", "x"),
        ("docchat.audit.action", "project_member.added", "project_member"),
        ("docchat.audit.action", "ops_config.set", "a.b.c"),
        ("docchat.opsconfig.key", "ingestion.max_parallel", "ingestion."),
        ("docchat.opsconfig.area", "ingestion", "Ingestion"),
        ("docchat.model", "gpt-5.6-sol", "GPT 5"),
        ("error.type", "builtins.ValueError", "ValueError: cnry-question-0a1b2c3d"),
        ("error.type", "tests.test_x.f.<locals>.Boom", "a b"),
        ("http.response.status_code", 404, 99),
        ("http.route", "/v1/admin/indexes/{indexId}/uploads", "/v1/x?y=1"),
        ("docchat.ingestion.parse_uid", 10002, 10018),
    ],
)
def test_value_types(key: str, good: object, bad: object) -> None:
    spec = KEYS[key]

    assert spec.accepts(good)
    assert not spec.accepts(bad)


def test_timestamps_and_floats_in_events() -> None:
    expires_at = EVENTS["index_override_used"].properties["expiresAt"]
    duration = EVENTS["ingestion_run_finished"].properties["durationSeconds"]

    assert expires_at.accepts("2026-10-30T12:00:00.000Z")
    assert not expires_at.accepts("2026-10-30 12:00")
    assert duration.accepts(12)
    assert duration.accepts(12.5)
    assert not duration.accepts(-1)


def test_an_enum_needs_its_values_or_shape() -> None:
    with pytest.raises(ValueError, match="enum"):
        ValueSpec(kind=ValueKind.ENUM)


def test_the_registries_are_frozen() -> None:
    with pytest.raises(TypeError):
        KEYS["docchat.new_key"] = ValueSpec(kind=ValueKind.BOOLEAN)  # type: ignore[index]
