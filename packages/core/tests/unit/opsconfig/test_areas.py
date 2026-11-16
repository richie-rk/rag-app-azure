"""The ops-config area model unit tests of the eleven API areas (T-119; `81-config.md` sections
3.4 to 3.7 and acceptance criterion 7).

Defaults, types and range ends are read from the catalogue's own tables, so the models and the
specification can't drift apart. The `ingestion` area's cases are in
`tests/unit/ingestion/test_settings.py`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Collection, Mapping
from decimal import Decimal
from typing import get_origin

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from docchat_core.opsconfig.areas import (
    ApiOps,
    ChatOps,
    MemoryOps,
    ModelRate,
    OpsReasoningEffort,
    OpsRuleError,
    PricingOps,
    SummaryOps,
    efforts_context,
)
from docchat_core.opsconfig.root import (
    API_AREAS,
    AREA_MODELS,
    CATALOGUE,
    Area,
    area_keys,
    failing_keys,
    validate_area,
)

type SpecTable = Callable[[str, str], list[list[str]]]

ALL_EFFORTS = frozenset({"none", "low", "medium", "high"})
"""Every row of the capability table today lists these (`12-chat-pipeline.md` section 10.7)."""

HEADINGS = (
    "### 3.4 Area `api` (`ApiOps`)",
    "### 3.5 Areas of the chat pipeline",
    "### 3.7 Areas of conversations and memory",
)

_RANGE_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?) to (\d[\d,]*(?:\.\d+)?)(?![\d.])")

# Values of other keys that keep a cross-field rule intact at one end of a key's range.
COMPANIONS: dict[tuple[str, str], dict[str, JsonValue]] = {
    ("chat.answer_max_completion_tokens", "high"): {"chat.context_token_budget": 200_000},
    ("quota.tokens_per_day", "high"): {"quota.tokens_per_month": 100_000_000_000},
    ("summary.trigger_tokens", "low"): {"summary.keep_recent_tokens": 0},
    ("summary.trigger_tokens", "high"): {"summary.max_input_tokens": 60_000},
    ("summary.keep_recent_tokens", "high"): {
        "summary.trigger_tokens": 20_000,
        "summary.max_input_tokens": 60_000,
    },
    ("summary.max_input_tokens", "low"): {"summary.trigger_tokens": 2000},
    ("summary.max_summary_tokens", "high"): {"summary.max_completion_tokens": 16_000},
    ("summary.max_completion_tokens", "low"): {"summary.max_summary_tokens": 200},
    ("summary.timeout_seconds", "high"): {"summary.lease_seconds": 900},
    ("summary.lease_seconds", "low"): {"summary.timeout_seconds": 10},
    ("memory.timeout_seconds", "high"): {"memory.lease_seconds": 900},
    ("memory.lease_seconds", "low"): {"memory.timeout_seconds": 10},
}


def _number(text: str) -> int | float:
    text = text.replace(",", "")
    return float(text) if "." in text else int(text)


def _default(cell: str) -> JsonValue:
    try:
        value: JsonValue = json.loads(cell.replace(",", ""))
    except json.JSONDecodeError:
        return cell
    return value


def catalogue(spec_table: SpecTable) -> dict[str, dict[str, str]]:
    """The rows of sections 3.4, 3.5 and 3.7 by key, each cell by its column name."""
    rows: dict[str, dict[str, str]] = {}
    for heading in HEADINGS:
        for cells in spec_table("81-config.md", heading):
            rows[cells[0]] = {"type": cells[1], "default": cells[2], "range": cells[3]}
    return rows


def defaults(spec_table: SpecTable, area: Area) -> dict[str, JsonValue]:
    rows = catalogue(spec_table)
    return {key: _default(rows[key]["default"]) for key in area_keys(area)}


def validate(
    area: Area, values: Mapping[str, JsonValue], efforts: Collection[str] | None = ALL_EFFORTS
) -> BaseModel:
    return validate_area(area, values, chat_small_efforts=efforts)


def refused_keys(
    area: Area, values: Mapping[str, JsonValue], efforts: Collection[str] | None = ALL_EFFORTS
) -> tuple[str, ...]:
    with pytest.raises(ValidationError) as raised:
        validate(area, values, efforts)
    return failing_keys(area, raised.value)


def with_values(base: Mapping[str, JsonValue], **changes: JsonValue) -> dict[str, JsonValue]:
    """`base` with dotted keys changed; Python names stand for dots as `__`."""
    return {**base, **{name.replace("__", "."): value for name, value in changes.items()}}


def test_t119_catalogue_covers_exactly_the_api_areas(spec_table: SpecTable) -> None:
    rows = catalogue(spec_table)
    assert set(rows) == {key for area in API_AREAS for key in area_keys(area)}
    assert len(rows) == 48


def test_t119_every_area_accepts_its_defaults(spec_table: SpecTable) -> None:
    for area in API_AREAS:
        values = defaults(spec_table, area)
        model = validate(area, values)
        dumped = model.model_dump(mode="json")
        assert dumped == values, area


def test_t119_field_types_follow_the_catalogue(spec_table: SpecTable) -> None:
    for key, row in catalogue(spec_table).items():
        area = CATALOGUE[key]
        model = AREA_MODELS[area]
        annotation = next(
            field.annotation for field in model.model_fields.values() if field.alias == key
        )
        kind = row["type"].split(",")[0]
        expected: dict[str, object] = {
            "integer": int,
            "decimal": float,
            "boolean": bool,
            "string": str,
            "enum": OpsReasoningEffort,
        }
        if kind == "object":
            assert get_origin(annotation) is Mapping, key
        else:
            assert annotation is expected[kind], key


def test_t119_each_numeric_key_accepts_its_ends_and_refuses_one_step_outside(
    spec_table: SpecTable,
) -> None:
    checked = 0
    for key, row in catalogue(spec_table).items():
        found = _RANGE_RE.match(row["range"])
        if found is None:
            continue
        area = CATALOGUE[key]
        base = defaults(spec_table, area)
        low, high = _number(found.group(1)), _number(found.group(2))
        if isinstance(low, float):
            outside_low: int | float = math.nextafter(low, -math.inf)
            outside_high: int | float = math.nextafter(float(high), math.inf)
        else:
            outside_low, outside_high = low - 1, int(high) + 1
        for end, inside, outside in (("low", low, outside_low), ("high", high, outside_high)):
            values = {**base, **COMPANIONS.get((key, end), {}), key: inside}
            validate(area, values)
            assert key in refused_keys(area, {**values, key: outside}), (key, end)
        checked += 1
    # The 41 numeric keys but the two whose other end is another key (tested below).
    assert checked == 39


def test_t119_min_confident_results_is_bounded_by_keep_count(spec_table: SpecTable) -> None:
    base = defaults(spec_table, "retrieval")
    keep = base["retrieval.keep_count"]
    assert isinstance(keep, int)
    validate("retrieval", with_values(base, retrieval__min_confident_results=1))
    validate("retrieval", with_values(base, retrieval__min_confident_results=keep))
    assert "retrieval.min_confident_results" in refused_keys(
        "retrieval", with_values(base, retrieval__min_confident_results=0)
    )
    assert refused_keys(
        "retrieval", with_values(base, retrieval__min_confident_results=keep + 1)
    ) == ("retrieval.min_confident_results", "retrieval.keep_count")


def test_t119_tokens_per_month_is_bounded_by_tokens_per_day(spec_table: SpecTable) -> None:
    base = defaults(spec_table, "quota")
    day = base["quota.tokens_per_day"]
    assert isinstance(day, int)
    validate("quota", with_values(base, quota__tokens_per_month=day))
    validate("quota", with_values(base, quota__tokens_per_month=100_000_000_000))
    assert refused_keys("quota", with_values(base, quota__tokens_per_month=day - 1)) == (
        "quota.tokens_per_month",
        "quota.tokens_per_day",
    )
    assert "quota.tokens_per_month" in refused_keys(
        "quota", with_values(base, quota__tokens_per_month=100_000_000_001)
    )


@pytest.mark.parametrize(
    ("area", "changes", "keys"),
    [
        (
            "retrieval",
            {"retrieval.candidate_count": 10, "retrieval.keep_count": 11},
            ("retrieval.keep_count", "retrieval.candidate_count"),
        ),
        (
            "chat",
            {"chat.context_token_budget": 20_000, "chat.answer_max_completion_tokens": 12_001},
            ("chat.answer_max_completion_tokens", "chat.context_token_budget"),
        ),
        (
            "quota",
            {"quota.tokens_per_day": 2_000_000, "quota.tokens_per_month": 1_999_999},
            ("quota.tokens_per_month", "quota.tokens_per_day"),
        ),
        (
            "summary",
            {"summary.keep_recent_tokens": 3000, "summary.trigger_tokens": 3000},
            ("summary.keep_recent_tokens", "summary.trigger_tokens"),
        ),
        (
            "summary",
            {"summary.trigger_tokens": 12_001, "summary.max_input_tokens": 12_000},
            ("summary.trigger_tokens", "summary.max_input_tokens"),
        ),
        (
            "summary",
            {"summary.max_summary_tokens": 800, "summary.max_completion_tokens": 999},
            ("summary.max_completion_tokens", "summary.max_summary_tokens"),
        ),
        (
            "summary",
            {"summary.timeout_seconds": 41, "summary.lease_seconds": 122},
            ("summary.lease_seconds", "summary.timeout_seconds"),
        ),
        (
            "memory",
            {"memory.timeout_seconds": 21, "memory.lease_seconds": 62},
            ("memory.lease_seconds", "memory.timeout_seconds"),
        ),
    ],
)
def test_t119_each_cross_field_rule_refuses_a_breaking_combination(
    spec_table: SpecTable, area: Area, changes: dict[str, JsonValue], keys: tuple[str, ...]
) -> None:
    base = defaults(spec_table, area)
    assert refused_keys(area, {**base, **changes}) == keys


def test_t119_rule_edges_are_accepted(spec_table: SpecTable) -> None:
    validate("chat", {**defaults(spec_table, "chat"), "chat.context_token_budget": 20_000})
    summary = defaults(spec_table, "summary")
    validate("summary", with_values(summary, summary__max_completion_tokens=1000))
    validate("summary", with_values(summary, summary__trigger_tokens=12_000))
    validate("summary", with_values(summary, summary__lease_seconds=90))
    validate("memory", with_values(defaults(spec_table, "memory"), memory__lease_seconds=60))


def test_t119_several_broken_rules_name_every_key(spec_table: SpecTable) -> None:
    values = with_values(
        defaults(spec_table, "summary"),
        summary__keep_recent_tokens=5000,
        summary__lease_seconds=60,
    )
    assert refused_keys("summary", values) == (
        "summary.keep_recent_tokens",
        "summary.trigger_tokens",
        "summary.lease_seconds",
        "summary.timeout_seconds",
    )


@pytest.mark.parametrize("area", ["summary", "memory"])
def test_t119_an_effort_the_chat_small_row_does_not_list_is_refused(
    spec_table: SpecTable, area: Area
) -> None:
    key = f"{area}.reasoning_effort"
    base = defaults(spec_table, area)
    for effort in ("none", "low", "medium"):
        model = validate(area, {**base, key: effort})
        assert model.model_dump()[key] == effort
    assert refused_keys(area, {**base, key: "medium"}, efforts={"none", "low"}) == (key,)
    # `high` is in the capability row but outside the ops-config enum.
    assert refused_keys(area, {**base, key: "high"}) == (key,)
    assert refused_keys(area, {**base, key: "minimal"}) == (key,)


@pytest.mark.parametrize("area", ["summary", "memory"])
def test_t119_none_is_refused_when_the_row_drops_none(spec_table: SpecTable, area: Area) -> None:
    key = f"{area}.reasoning_effort"
    values = {**defaults(spec_table, area), key: "none"}
    validate(area, values, efforts={"none", "low", "medium", "high"})
    assert refused_keys(area, values, efforts={"low", "medium", "high"}) == (key,)


@pytest.mark.parametrize("area", ["summary", "memory"])
def test_validation_without_the_chat_small_row_refuses_the_effort(
    spec_table: SpecTable, area: Area
) -> None:
    assert refused_keys(area, defaults(spec_table, area), efforts=None) == (
        f"{area}.reasoning_effort",
    )
    with pytest.raises(ValidationError):
        AREA_MODELS[area].model_validate(defaults(spec_table, area))
    AREA_MODELS[area].model_validate(defaults(spec_table, area), context=efforts_context({"low"}))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("chat.rewrite_history_turns", 4.0),
        ("chat.rewrite_history_turns", "4"),
        ("chat.rewrite_history_turns", True),
        ("chat.rewrite_history_turns", None),
        ("retrieval.reranker_threshold", "2.0"),
        ("retrieval.reranker_threshold", True),
        ("followups.enabled", "false"),
        ("followups.enabled", "true"),
        ("followups.enabled", 0),
        ("followups.enabled", 1),
        ("memory.enabled", "false"),
        ("feedback.comments_enabled", None),
        ("pricing.currency", 840),
        ("pricing.model_rates", []),
        ("summary.reasoning_effort", 1),
    ],
)
def test_t119_values_are_typed_data_never_coerced(
    spec_table: SpecTable, key: str, value: JsonValue
) -> None:
    area = CATALOGUE[key]
    assert key in refused_keys(area, {**defaults(spec_table, area), key: value})


def test_a_whole_number_is_a_decimal_value(spec_table: SpecTable) -> None:
    values = {**defaults(spec_table, "retrieval"), "retrieval.reranker_threshold": 3}
    model = validate("retrieval", values)
    threshold = model.model_dump()["retrieval.reranker_threshold"]
    assert threshold == 3.0
    assert type(threshold) is float


@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP"])
def test_t119_currency_accepts_three_upper_case_letters(
    spec_table: SpecTable, currency: str
) -> None:
    validate("pricing", {**defaults(spec_table, "pricing"), "pricing.currency": currency})


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "", "U5D", " USD"])
def test_t119_currency_refuses_anything_else(spec_table: SpecTable, currency: str) -> None:
    values = {**defaults(spec_table, "pricing"), "pricing.currency": currency}
    assert refused_keys("pricing", values) == ("pricing.currency",)


def _rate(**overrides: JsonValue) -> dict[str, JsonValue]:
    return {
        "inputPerMillion": "1.75",
        "cachedInputPerMillion": "0.175",
        "cacheWritePerMillion": "0",
        "outputPerMillion": "14.000000",
        **overrides,
    }


def test_t119_model_rates_are_read_as_decimals() -> None:
    model = PricingOps.model_validate(
        {
            "pricing.currency": "USD",
            "pricing.model_rates": {"gpt-5.6-sol": _rate(), "text-embedding-3-large": _rate()},
        }
    )
    rate = model.model_rates["gpt-5.6-sol"]
    assert rate == ModelRate.model_validate(_rate())
    assert rate.input_per_million == Decimal("1.75")
    assert rate.cached_input_per_million == Decimal("0.175")
    assert rate.cache_write_per_million == Decimal(0)
    assert str(rate.output_per_million) == "14.000000"
    assert model.model_dump(mode="json")["pricing.model_rates"]["gpt-5.6-sol"] == _rate()
    assert PricingOps.model_validate(model.model_dump()) == model


@pytest.mark.parametrize("rate", ["0", "1000", "1000.000000", "0.000001", "999.999999"])
def test_t119_a_rate_from_0_to_1000_with_6_places_is_accepted(rate: str) -> None:
    ModelRate.model_validate(_rate(inputPerMillion=rate))


@pytest.mark.parametrize(
    "rate",
    [
        "1000.000001",
        "1001",
        "0.0000001",
        "-1",
        "01",
        "1.",
        ".5",
        "1e3",
        " 1",
        "",
        "NaN",
        1,
        1.5,
        None,
    ],
)
def test_t119_any_other_rate_is_refused(rate: JsonValue) -> None:
    with pytest.raises(ValidationError):
        ModelRate.model_validate(_rate(inputPerMillion=rate))


@pytest.mark.parametrize(
    "rates",
    [
        {f"model-{index}": _rate() for index in range(21)},
        {"GPT": _rate()},
        {"-gpt": _rate()},
        {"g" * 65: _rate()},
        {"gpt": {k: v for k, v in _rate().items() if k != "outputPerMillion"}},
        {"gpt": _rate(currency="USD")},
        {"gpt": "1.75"},
    ],
)
def test_t119_the_price_table_refuses_a_wrong_shape(
    spec_table: SpecTable, rates: JsonValue
) -> None:
    values = {**defaults(spec_table, "pricing"), "pricing.model_rates": rates}
    assert refused_keys("pricing", values) == ("pricing.model_rates",)


def test_twenty_priced_models_are_accepted(spec_table: SpecTable) -> None:
    rates: dict[str, JsonValue] = {f"model-{index}": _rate() for index in range(20)}
    validate("pricing", {**defaults(spec_table, "pricing"), "pricing.model_rates": rates})


def test_the_price_table_of_a_snapshot_cannot_be_changed() -> None:
    model = PricingOps.model_validate(
        {"pricing.currency": "USD", "pricing.model_rates": {"gpt": _rate()}}
    )
    with pytest.raises(TypeError):
        model.model_rates["other"] = model.model_rates["gpt"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        model.model_rates["gpt"].input_per_million = Decimal(0)  # type: ignore[misc]


@pytest.mark.parametrize("area", API_AREAS)
def test_t119_a_missing_key_is_refused_by_its_name(spec_table: SpecTable, area: Area) -> None:
    base = defaults(spec_table, area)
    for key in base:
        missing = {other: value for other, value in base.items() if other != key}
        assert key in refused_keys(area, missing), key


@pytest.mark.parametrize("area", API_AREAS)
def test_every_area_model_forbids_unknown_fields(spec_table: SpecTable, area: Area) -> None:
    model = AREA_MODELS[area]
    base = defaults(spec_table, area)
    context = efforts_context(ALL_EFFORTS)
    assert model.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError) as raised:
        model.model_validate({**base, f"{area}.unknown_key": 1}, context=context)
    assert raised.value.errors()[0]["type"] == "extra_forbidden"
    # Field names aren't keys: only the dotted aliases are read.
    by_name = {name: base[field.alias or ""] for name, field in model.model_fields.items()}
    with pytest.raises(ValidationError):
        model.model_validate(by_name, context=context)


@pytest.mark.parametrize("area", API_AREAS)
def test_every_area_model_is_frozen(spec_table: SpecTable, area: Area) -> None:
    model = validate(area, defaults(spec_table, area))
    name = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, name, getattr(model, name))


def test_every_area_dumps_its_dotted_keys(spec_table: SpecTable) -> None:
    api = ApiOps.model_validate(defaults(spec_table, "api"))
    assert api.model_dump() == {"api.write_requests_per_minute": 120}
    assert api.write_requests_per_minute == 120


def test_a_rule_error_names_keys_and_never_values() -> None:
    error = OpsRuleError(("chat.answer_max_completion_tokens", "chat.context_token_budget"))
    assert error.keys == ("chat.answer_max_completion_tokens", "chat.context_token_budget")
    assert "12000" not in str(error)
    assert isinstance(error, ValueError)


def test_area_models_are_the_named_classes() -> None:
    assert AREA_MODELS["chat"] is ChatOps
    assert AREA_MODELS["summary"] is SummaryOps
    assert AREA_MODELS["memory"] is MemoryOps
