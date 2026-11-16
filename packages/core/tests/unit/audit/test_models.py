"""Unit tests of the audit event and metadata models (`80-data-model.md` section 2.21; T-079)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from pydantic import JsonValue, ValidationError

from docchat_core.audit import models
from docchat_core.audit.catalogue import (
    ACTIONS_WITHOUT_METADATA,
    AUDIT_ACTIONS,
)
from docchat_core.audit.models import (
    METADATA_MAX_CHARACTERS,
    METADATA_MODELS,
    AuditEvent,
    AuditMetadata,
    OpsConfigResetMetadata,
    OpsConfigSetMetadata,
    value_sha256,
)
from docchat_core.search.schema import ANALYZER_ALLOWLIST

U = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
BIG = 2_147_483_647
TICKET = "T#-_" * 16
TIME = "2026-10-30T12:00:00.000Z"
CATEGORIES = ["role", "team", "expertise", "topic", "answer_style"]
DEPLOYMENT = "d" * 64
COUNTS = {
    "conversations": BIG,
    "memories": BIG,
    "usage": BIG,
    "users": BIG,
    "projectMembers": BIG,
    "userIndexOverrides": BIG,
}

MAXIMAL: dict[str, dict[str, Any]] = {
    "user.provisioned": {"defaultProjectCount": BIG},
    "project.created": {"isDefault": True},
    "project.updated": {
        "changedFields": ["name", "description", "instructions", "exampleQuestions", "isDefault"]
    },
    "project.archived": {"wasDefault": False},
    "project_member.added": {"grantSource": "admin"},
    "project_member.removed": {"grantSource": "default"},
    "project_index.mapped": {"isDefault": True},
    "project_index.default_set": {"previousDefaultIndexId": U},
    "project_index.unmapped": {"wasDefault": True, "overridesRemoved": BIG},
    "ops_config.set": {"area": "a" * 64, "previous": 1, "new": 2, "ticket": TICKET},
    "ops_config.reset": {"area": "a" * 64, "previous": 1, "ticket": TICKET},
    "user_index_override.set": {
        "searchIndexId": U,
        "expiresAt": TIME,
        "replaced": True,
        "ticket": TICKET,
    },
    "user_index_override.cleared": {"searchIndexId": U, "ticket": TICKET},
    "memory.fact_updated": {
        "category": "answer_style",
        "previousCategory": "answer_style",
        "previousSourceKind": "extracted",
    },
    "memory.fact_deleted": {"category": "answer_style", "sourceKind": "extracted"},
    "memory.cleared": {"factsRemoved": 200},
    "memory.facts_extracted": {
        "addedFactIds": [U] * 5,
        "replacedFactIds": [U] * 5,
        "categories": CATEGORIES,
        "sessionId": U,
        "turnId": U,
    },
    "conversation.read": {"turnIds": [U] * 50, "ticket": TICKET, "level": "read"},
    "user_data.exported": {
        "ticket": TICKET,
        "counts": {**COUNTS, "auditEvents": BIG, "actorReferences": BIG},
        "manifestSha256": "a" * 64,
    },
    "user_data.deleted": {"ticket": TICKET, "counts": COUNTS},
    "document.traces_redacted": {"documentId": U, "traceCount": BIG, "ticket": TICKET},
    "index.created": {
        "methodKey": "m" * 64,
        "methodVersion": BIG,
        "embeddingModel": "text-embedding-3-large" + "x" * 42,
        "embeddingDimensions": BIG,
        "languageAnalyzer": "standard.lucene" + "x" * 17,
        "physicalIndexId": U,
        "runId": U,
    },
    "index.updated": {
        "changedFields": [
            "displayName",
            "description",
            "methodKey",
            "methodVersion",
            "methodParameters",
            "embeddingModel",
            "embeddingDimensions",
            "languageAnalyzer",
        ],
        "status": "reingest_required",
    },
    "index.deleted": {"runId": U, "physicalIndexCount": BIG, "documentCount": BIG},
    "index.reingest_started": {"runId": U, "mode": "new_physical_index", "itemCount": BIG},
    "ingestion.reindex_started": {
        "runId": U,
        "mode": "new_physical_index",
        "itemCount": BIG,
        "ticket": TICKET,
    },
    "document.upload_committed": {
        "indexId": U,
        "documentId": U,
        "versionNumber": BIG,
        "sizeBytes": BIG,
        "contentType": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
    },
    "document.malware_detected": {"indexId": U, "documentId": U, "versionNumber": BIG},
    "document.source_fetched": {
        "indexId": U,
        "documentId": U,
        "versionNumber": BIG,
        "sizeBytes": BIG,
        "ticket": TICKET,
    },
    "document.deleted": {
        "indexId": U,
        "runId": U,
        "supersededVersionCount": BIG,
        "cancelledItemCount": BIG,
    },
    "document.reingest_requested": {"indexId": U, "runId": U, "itemCount": 1},
    "ingestion.run_submitted": {"indexId": U, "itemCount": BIG, "notQueuedCount": BIG},
    "ingestion.run_cancelled": {"indexId": U, "cancelledItemCount": BIG, "ticket": TICKET},
    "ingestion.item_requeued": {"runId": U, "claimCount": BIG, "ticket": TICKET},
    "ingestion.item_failed": {
        "runId": U,
        "outcomeCode": "processing_interrupted",
        "ticket": TICKET,
    },
    "ingestion.rebuild_abandoned": {
        "indexId": U,
        "generation": BIG,
        "cancelledItemCount": BIG,
        "runId": U,
        "ticket": TICKET,
    },
    "ingestion.deployments_changed": {
        "indexId": U,
        "generation": BIG,
        "previousQueryDeployment": DEPLOYMENT,
        "queryDeployment": DEPLOYMENT,
        "previousIngestionDeployment": DEPLOYMENT,
        "ingestionDeployment": DEPLOYMENT,
        "ticket": TICKET,
    },
    "ingestion.poison_drained": {"deletedCount": BIG, "ticket": TICKET},
    "test_users.rotated": {"environment": "dev", "aliasCount": 25},
    "model_deployments.checked": {
        "environment": "dev",
        "result": "failed",
        "callCount": 60,
        "failures": [
            {
                "deployment": DEPLOYMENT,
                "call": "shield_empty_user_prompt",
                "reasoningEffort": "medium",
                "status": 599,
            }
        ]
        * 10,
        "tokenizer": [{"deployment": DEPLOYMENT, "localTokens": BIG, "reportedTokens": BIG}] * 2,
        "latency": [{"call": "summary", "calls": BIG, "p95Ms": BIG, "timeoutSeconds": BIG}] * 2,
        "promptShields": [
            {
                "probe": "empty_user_prompt",
                "status": 599,
                "attackDetected": None,
                "outcome": "user_prompt_refused",
            }
        ]
        * 2,
        "ticket": TICKET,
    },
}


def model_for(action: str) -> type[AuditMetadata]:
    model = METADATA_MODELS[action]
    assert model is not None
    return model


def test_every_action_has_its_model_named_by_the_rule() -> None:
    assert tuple(METADATA_MODELS) == AUDIT_ACTIONS
    for action, model in METADATA_MODELS.items():
        if action in ACTIONS_WITHOUT_METADATA:
            assert model is None
            continue
        expected = "".join(part.capitalize() for part in re.split(r"[._]", action)) + "Metadata"
        assert model is not None
        assert model.__name__ == expected
        assert getattr(models, expected) is model


@pytest.mark.parametrize("action", sorted(MAXIMAL))
def test_t079_maximal_metadata_fits_the_column(action: str) -> None:
    metadata = model_for(action).model_validate(MAXIMAL[action])
    text = metadata.to_json()
    assert len(text) <= METADATA_MAX_CHARACTERS
    assert json.loads(text) == json.loads(json.dumps(MAXIMAL[action]))


def test_maximal_examples_cover_every_model() -> None:
    assert set(MAXIMAL) == set(AUDIT_ACTIONS) - ACTIONS_WITHOUT_METADATA


@pytest.mark.parametrize("action", sorted(MAXIMAL))
def test_t079_unknown_keys_are_refused(action: str) -> None:
    with pytest.raises(ValidationError):
        model_for(action).model_validate({**MAXIMAL[action], "note": "free text"})


@pytest.mark.parametrize(
    ("action", "field", "value"),
    [
        ("ops_config.set", "ticket", "INC 42 please fix"),
        ("project.updated", "changedFields", ["name", "the owner's email"]),
        ("project.updated", "changedFields", ["name", "name"]),
        ("project.updated", "changedFields", []),
        ("memory.cleared", "factsRemoved", 201),
        ("memory.facts_extracted", "categories", ["role", "Alice"]),
        ("conversation.read", "turnIds", [U] * 51),
        ("index.created", "embeddingModel", "Text Embedding"),
        ("document.reingest_requested", "itemCount", 2),
        ("test_users.rotated", "environment", "prod"),
        ("user_data.exported", "manifestSha256", "A" * 64),
        ("model_deployments.checked", "callCount", 61),
    ],
)
def test_t079_free_text_and_out_of_range_values_are_refused(
    action: str, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        model_for(action).model_validate({**MAXIMAL[action], field: value})


def test_every_allowlisted_analyzer_is_an_analyzer_name() -> None:
    for option in ANALYZER_ALLOWLIST:
        metadata = {**MAXIMAL["index.created"], "languageAnalyzer": option.value}
        model_for("index.created").model_validate(metadata)


@pytest.mark.parametrize("value", ["standard lucene", "en.microsoft;", ".lucene"])
def test_an_analyzer_name_is_no_free_text(value: str) -> None:
    with pytest.raises(ValidationError):
        model_for("index.created").model_validate(
            {**MAXIMAL["index.created"], "languageAnalyzer": value}
        )


def test_extracted_facts_total_one_to_ten() -> None:
    model = model_for("memory.facts_extracted")
    base = MAXIMAL["memory.facts_extracted"]
    with pytest.raises(ValidationError):
        model.model_validate({**base, "addedFactIds": [], "replacedFactIds": []})
    with pytest.raises(ValidationError):
        model.model_validate({**base, "addedFactIds": [U] * 6})
    model.model_validate({**base, "addedFactIds": [U], "replacedFactIds": []})


def test_http_status_is_zero_or_a_status() -> None:
    check = MAXIMAL["model_deployments.checked"]
    failure = check["failures"][0]
    model = model_for("model_deployments.checked")
    model.model_validate({**check, "failures": [{**failure, "status": 0}]})
    with pytest.raises(ValidationError):
        model.model_validate({**check, "failures": [{**failure, "status": 99}]})


def test_none_is_written_as_null() -> None:
    metadata = model_for("document.reingest_requested").model_validate(
        {"indexId": U, "runId": None, "itemCount": 0}
    )
    assert metadata.to_json() == f'{{"indexId":"{U}","runId":null,"itemCount":0}}'


def test_t079_large_ops_config_values_become_hashes() -> None:
    rates: JsonValue = {
        f"model-{index:03d}": {"inputPerMillion": "1.5" * 30} for index in range(100)
    }
    assert len(json.dumps(rates)) > 10_000
    changed = OpsConfigSetMetadata.build(area="pricing", previous={}, new=rates, ticket="INC-1")
    data = json.loads(changed.to_json())
    assert data == {
        "area": "pricing",
        "previousSha256": value_sha256({}),
        "newSha256": value_sha256(rates),
        "ticket": "INC-1",
    }
    small = OpsConfigSetMetadata.build(area="quota", previous=5, new=6, ticket="INC-1")
    assert json.loads(small.to_json()) == {
        "area": "quota",
        "previous": 5,
        "new": 6,
        "ticket": "INC-1",
    }
    reset = OpsConfigResetMetadata.build(area="pricing", previous=rates, ticket="INC-2")
    assert json.loads(reset.to_json()) == {
        "area": "pricing",
        "previousSha256": value_sha256(rates),
        "ticket": "INC-2",
    }
    assert value_sha256({"b": 1, "a": "é"}) == (
        __import__("hashlib").sha256('{"a":"é","b":1}'.encode()).hexdigest()
    )


def test_ops_config_needs_exactly_one_form() -> None:
    with pytest.raises(ValidationError):
        OpsConfigSetMetadata(area="quota", previous=1, ticket="T")
    with pytest.raises(ValidationError):
        OpsConfigSetMetadata(
            area="quota",
            previous=1,
            new=2,
            previous_sha256="a" * 64,
            new_sha256="b" * 64,
            ticket="T",
        )
    with pytest.raises(ValidationError):
        OpsConfigResetMetadata(area="quota", ticket="T")


def event(**overrides: Any) -> AuditEvent:
    fields: dict[str, Any] = {
        "action": "project.created",
        "actor_type": "user",
        "actor_object_id": U,
        "target_type": "project",
        "target_id": UUID(U),
        "project_id": U,
        "metadata": model_for("project.created").model_validate({"isDefault": True}),
    }
    fields.update(overrides)
    return AuditEvent.model_validate(fields)


def test_event_accepts_a_uuid_target_and_serialises_metadata() -> None:
    created = event()
    assert created.target_id == U
    assert created.metadata_json() == '{"isDefault":true}'


@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "project.restored"},
        {"metadata": None},
        {"metadata": model_for("project.archived").model_validate({"wasDefault": True})},
        {"target_id": None},
        {"target_id": U.upper()},
        {"action": "wiki.edited"},
        {"actor_type": "admin"},
        {"extra": 1},
    ],
)
def test_event_refuses_a_mismatch(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        event(**overrides)


def test_event_targets_by_type() -> None:
    restored = event(action="project.restored", metadata=None)
    assert restored.metadata_json() is None
    ops: Callable[..., AuditEvent] = lambda target: event(  # noqa: E731
        action="ops_config.reset",
        actor_type="operator",
        target_type="ops_config",
        target_id=target,
        project_id=None,
        metadata=OpsConfigResetMetadata(area="quota", previous=1, ticket="T"),
    )
    assert ops("quota.tokens_per_day").target_id == "quota.tokens_per_day"
    with pytest.raises(ValidationError):
        ops("quota tokens")
    drained = event(
        action="ingestion.poison_drained",
        actor_type="operator",
        target_type="ingestion_queue",
        target_id="ingestion-requests-poison",
        project_id=None,
        metadata=model_for("ingestion.poison_drained").model_validate(
            {"deletedCount": 3, "ticket": "T"}
        ),
    )
    assert drained.target_id == "ingestion-requests-poison"
    rotated = event(
        action="test_users.rotated",
        actor_type="operator",
        target_type="test_users",
        target_id=None,
        project_id=None,
        metadata=model_for("test_users.rotated").model_validate(
            {"environment": "dev", "aliasCount": 3}
        ),
    )
    assert rotated.target_id is None
    with pytest.raises(ValidationError):
        event(
            action="test_users.rotated",
            actor_type="operator",
            target_type="test_users",
            target_id=U,
            metadata=rotated.metadata,
        )
