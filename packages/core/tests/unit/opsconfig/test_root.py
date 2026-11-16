"""Unit tests of the root model, the catalogue of keys and the per-area validation
(`81-config.md` section 3.1; `80-data-model.md` section 2.24; ADR 0028 item 4).

The seed test reads the 73 rows the frozen first revision writes, straight from its source (the
revision imports Alembic, which `docchat-core` doesn't depend on), and proves that every area
model declares exactly those keys and reads each seeded default back as the same typed value.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.opsconfig.areas import ApiOps, ChatOps, OpsRuleError, efforts_context
from docchat_core.opsconfig.root import (
    API_AREAS,
    AREA_MODELS,
    CATALOGUE,
    EFFORT_AREAS,
    INGESTION_AREAS,
    Area,
    AreaUnavailableError,
    OpsConfig,
    area_keys,
    failing_keys,
    validate_area,
)
from docchat_core.opsconfig.values import area_of, encode_value

type SpecTable = Callable[[str, str], list[list[str]]]

ALL_EFFORTS = frozenset({"none", "low", "medium", "high"})
REVISION = Path("db", "migrations", "versions", "0001_initial_schema.py")


def _revision_source() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / REVISION
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(str(REVISION))


def seeded_rows() -> dict[str, JsonValue]:
    """The ops-config rows of `0001_initial_schema`: every `*_OPS_CONFIG` tuple `upgrade()` passes
    to `seed_ops_config`, in order."""
    tree = ast.parse(_revision_source())
    constants: dict[str, list[tuple[str, JsonValue]]] = {}
    seeded: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.endswith("_OPS_CONFIG")
            and node.value is not None
        ):
            constants[node.target.id] = list(ast.literal_eval(node.value))
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            seeded.extend(
                call.args[0].id
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "seed_ops_config"
                and isinstance(call.args[0], ast.Name)
            )
    assert seeded == list(constants), "upgrade() seeds every *_OPS_CONFIG tuple once"
    rows: dict[str, JsonValue] = {}
    for name in seeded:
        for key, value in constants[name]:
            assert key not in rows, key
            rows[key] = value
    return rows


def _data_model_rows() -> dict[str, tuple[str, str]]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return {
                key: (stored, model)
                for key, stored, model in re.findall(
                    r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `(\w+)` \|$", text, re.M
                )
            }
    raise FileNotFoundError("80-data-model.md")


def seeded_area(rows: dict[str, JsonValue], area: Area) -> dict[str, JsonValue]:
    return {key: value for key, value in rows.items() if area_of(key) == area}


def test_the_catalogue_has_73_keys_in_12_areas(spec_table: SpecTable) -> None:
    table = spec_table("81-config.md", "### 3.1 The mechanism")
    assert [row[0] for row in table] == list(AREA_MODELS)
    for (area, model), row in zip(AREA_MODELS.items(), table, strict=True):
        _, model_name, module, keys, registered_by, _ = row
        assert model.__name__ == model_name
        assert model.__module__.startswith(module)
        assert len(area_keys(area)) == int(keys), area
        expected = INGESTION_AREAS if registered_by == "ingestion app" else API_AREAS
        assert area in expected
    assert len(CATALOGUE) == 73
    assert set(API_AREAS) | set(INGESTION_AREAS) == set(AREA_MODELS)
    assert not set(API_AREAS) & set(INGESTION_AREAS)
    assert {"summary", "memory"} == EFFORT_AREAS


def test_every_key_is_its_area_and_a_dotted_name() -> None:
    for key, area in CATALOGUE.items():
        assert key.startswith(f"{area}."), key
        assert re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", key), key
        assert len(key) <= 64


def test_every_area_model_forbids_unknown_fields_and_is_frozen() -> None:
    for area, model in AREA_MODELS.items():
        assert model.model_config.get("extra") == "forbid", area
        assert model.model_config.get("frozen") is True, area
        assert model.model_config.get("strict") is True, area
        for name, field in model.model_fields.items():
            assert field.is_required(), (area, name)


def test_the_first_revision_seeds_exactly_the_catalogue() -> None:
    rows = seeded_rows()
    assert len(rows) == 73
    assert set(rows) == set(CATALOGUE)
    for area in AREA_MODELS:
        assert list(seeded_area(rows, area)) == list(area_keys(area)), area


def test_every_area_model_reads_each_seeded_default_as_the_same_typed_value() -> None:
    rows = seeded_rows()
    for area in AREA_MODELS:
        values = seeded_area(rows, area)
        model = validate_area(area, values, chat_small_efforts=ALL_EFFORTS)
        dumped = model.model_dump(mode="json")
        for key, value in values.items():
            # Equal canonical text: same value and same JSON type (2.0 stays a decimal, 3 an
            # integer, true a boolean).
            assert encode_value(dumped[key]) == encode_value(value), key
            assert type(dumped[key]) is type(value), key


def test_the_seeded_values_are_the_data_model_rows() -> None:
    documented = _data_model_rows()
    rows = seeded_rows()
    assert set(documented) == set(rows)
    for key, value in rows.items():
        stored, model_name = documented[key]
        assert encode_value(value) == stored, key
        assert AREA_MODELS[CATALOGUE[key]].__name__ == model_name, key


def test_validate_area_drops_keys_its_model_does_not_declare() -> None:
    values: dict[str, JsonValue] = {
        "api.write_requests_per_minute": 120,
        "api.added_by_a_later_release": "anything",
        "chat.context_token_budget": 1,
    }
    model = validate_area("api", values)
    assert model == ApiOps.model_validate({"api.write_requests_per_minute": 120})


def test_failing_keys_names_missing_invalid_and_rule_keys() -> None:
    rows = seeded_rows()
    chat = seeded_area(rows, "chat")
    del chat["chat.max_turn_seconds"]
    chat["chat.rewrite_timeout_seconds"] = 21
    chat["chat.answer_max_completion_tokens"] = 40_000
    with pytest.raises(ValidationError) as raised:
        validate_area("chat", chat)
    assert set(failing_keys("chat", raised.value)) == {
        "chat.max_turn_seconds",
        "chat.rewrite_timeout_seconds",
    }
    rules = {**seeded_area(rows, "chat"), "chat.answer_max_completion_tokens": 40_000}
    with pytest.raises(ValidationError) as raised:
        validate_area("chat", rules)
    assert failing_keys("chat", raised.value) == (
        "chat.answer_max_completion_tokens",
        "chat.context_token_budget",
    )


def test_an_error_without_a_key_names_the_whole_area() -> None:
    class Unkeyed(BaseModel):
        value: int

    with pytest.raises(ValidationError) as raised:
        Unkeyed.model_validate({})
    assert failing_keys("api", raised.value) == ("api.write_requests_per_minute",)


def test_a_rule_error_carries_its_keys_into_the_failing_keys() -> None:
    class Ruled(BaseModel):
        value: int

        @override
        def model_post_init(self, context: Any, /) -> None:
            raise OpsRuleError(("quota.tokens_per_month", "quota.tokens_per_day"))

    with pytest.raises(ValidationError) as raised:
        Ruled.model_validate({"value": 1})
    assert failing_keys("quota", raised.value) == (
        "quota.tokens_per_month",
        "quota.tokens_per_day",
    )


def test_the_effort_areas_take_the_chat_small_row() -> None:
    rows = seeded_rows()
    summary = seeded_area(rows, "summary")
    validate_area("summary", summary, chat_small_efforts={"low"})
    with pytest.raises(ValidationError):
        validate_area("summary", summary, chat_small_efforts={"none"})
    with pytest.raises(ValidationError):
        validate_area("summary", summary)
    assert efforts_context({"low"}) == {"chat_small_reasoning_efforts": frozenset({"low"})}


def _snapshot(*areas: Area) -> OpsConfig:
    rows = seeded_rows()
    return OpsConfig.model_validate(
        {
            area: validate_area(area, seeded_area(rows, area), chat_small_efforts=ALL_EFFORTS)
            for area in areas
        },
        context=efforts_context(ALL_EFFORTS),
    )


def test_the_root_model_holds_one_sub_model_per_area() -> None:
    snapshot = _snapshot(*AREA_MODELS)
    for area, model in AREA_MODELS.items():
        assert isinstance(getattr(snapshot, area), model), area
        assert getattr(snapshot, f"{area}_area") is getattr(snapshot, area)
    assert snapshot.chat.context_token_budget == 40_000
    assert snapshot.ingestion.max_parallel_items == 1
    assert snapshot.memory.enabled is True
    assert snapshot.feedback.comments_enabled is True
    dumped = snapshot.model_dump(mode="json")
    assert list(dumped) == list(AREA_MODELS)
    assert dumped["ingestion"]["ingestion.max_parallel_items"] == 1


def test_an_area_the_snapshot_does_not_hold_raises() -> None:
    snapshot = _snapshot("ingestion")
    assert isinstance(snapshot.ingestion, IngestionOpsConfig)
    for area in API_AREAS:
        assert getattr(snapshot, f"{area}_area") is None
        with pytest.raises(AreaUnavailableError) as raised:
            getattr(snapshot, area)
        assert raised.value.area == area
        assert isinstance(raised.value, LookupError)


def test_a_snapshot_cannot_be_mutated() -> None:
    snapshot = _snapshot("api", "chat", "pricing")
    with pytest.raises(ValidationError):
        snapshot.chat_area = None  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.chat.context_token_budget = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.pricing.model_rates["new"] = None  # type: ignore[index]


def test_the_root_model_forbids_unknown_areas() -> None:
    chat: Any = _snapshot("chat").chat
    with pytest.raises(ValidationError):
        OpsConfig.model_validate({"chat": chat, "unknown": chat})
    api = ApiOps.model_validate({"api.write_requests_per_minute": 120})
    with pytest.raises(ValidationError):
        OpsConfig.model_validate({"chat": api})
    assert isinstance(chat, ChatOps)


def test_area_keys_refuses_a_field_without_a_dotted_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class Undotted(BaseModel):
        value: int

    monkeypatch.setattr("docchat_core.opsconfig.root.AREA_MODELS", {**AREA_MODELS, "api": Undotted})
    with pytest.raises(TypeError):
        area_keys("api")
