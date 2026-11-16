"""`chat capacity-check` (T-123's command half, T-542) and `chat model-check` (the command behind
T-250, T-251, T-331 and T-332, which run in `dev` only) (`86-operations.md` section 2.8.7;
`12-chat-pipeline.md` sections 3.7 and 10.7).

The seeded values are read from the frozen first revision, as the capacity unit test reads them.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion
from pydantic import JsonValue

from docchat_core.audit.models import ModelDeploymentsCheckedMetadata
from docchat_core.chat.models import MODEL_CAPABILITIES
from docchat_core.sql.engine import SqlUnavailable
from docchat_ops.commands import chat
from docchat_ops.framework.exitcodes import ExitCode, UnexpectedError

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "INC-1042")
REASON = ("--reason", "acceptance of the chat-pipeline stage")
AREAS = ("chat", "ratelimit", "followups", "summary", "memory")


def _seeded_rows() -> dict[str, JsonValue]:
    for parent in Path(__file__).resolve().parents:
        source = parent / "db" / "migrations" / "versions" / "0001_initial_schema.py"
        if source.is_file():
            break
    else:
        raise FileNotFoundError("0001_initial_schema.py")
    rows: dict[str, JsonValue] = {}
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.endswith("_OPS_CONFIG")
            and node.value is not None
        ):
            rows.update(dict(ast.literal_eval(node.value)))
    return rows


SEEDED = _seeded_rows()


@pytest.fixture
def seeded(ops: OpsHarness) -> OpsHarness:
    for key, value in SEEDED.items():
        ops.database.seed(key, value)
    return ops


def _capacity(ops: OpsHarness, environment: str, *extra: str) -> Outcome:
    return ops.run("--env", environment, "--json", "chat", "capacity-check", *extra)


# --- capacity-check ----------------------------------------------------------------------------


def test_t123_the_seeded_values_pass_in_prod(seeded: OpsHarness) -> None:
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["result"] == "passed"
    assert (report["chatShare"], report["smallShare"]) == (0.24, 0.2479)
    assert report["shareLimit"] == 0.25
    assert (report["chatCapacity"], report["smallCapacity"]) == (500, 530)
    assert report["smallChargeTokens"] == 43_790
    assert report["smallCallCharges"] == {
        "rewrite": 8_340,
        "search_passage": 5_100,
        "followups": 8_300,
        "memory": 7_700,
        "summary": 14_350,
    }
    assert report["enforced"] is True
    assert [unit for unit, _ in seeded.database.units] == ["chat_ops_config_read"]
    assert seeded.database.audit_rows == []


def test_t123_four_requests_a_minute_exit_2_in_prod(seeded: OpsHarness) -> None:
    outcome = _capacity(seeded, "prod", "--set", "ratelimit.chat_requests_per_minute=4")
    assert outcome.code == ExitCode.USAGE
    report = outcome.json()
    assert report["result"] == "exceeded"
    assert report["exceeded"] == ["chat", "small"]
    assert report["proposed"] == ["ratelimit.chat_requests_per_minute"]
    assert "more than 25%" in outcome.failure
    # The proposal is evaluated in memory only.
    stored = seeded.database.ops_rows["ratelimit.chat_requests_per_minute"]["value_json"]
    assert json.loads(stored) == {"value": 3}
    assert seeded.database.sql("chat_ops_config_read")[0].startswith("SELECT")
    assert all(text.startswith("SELECT") for _, text in seeded.database.statements)


@pytest.mark.parametrize("environment", ["dev", "load"])
def test_t123_dev_and_load_exit_0_whatever_the_shares(seeded: OpsHarness, environment: str) -> None:
    outcome = _capacity(seeded, environment, "--set", "ratelimit.chat_requests_per_minute=40")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["result"] == "exceeded"
    assert outcome.json()["enforced"] is False


@pytest.mark.parametrize(("environment", "code"), [("dev", 0), ("prod", 4), ("load", 0)])
def test_t123_an_empty_small_capacity_is_reported_absent(
    seeded: OpsHarness, environment: str, code: int
) -> None:
    seeded.write_azd(environment, DOCCHAT_OPENAI_SMALL_CAPACITY="")
    outcome = _capacity(seeded, environment)
    assert outcome.code == code, outcome.stderr
    report = outcome.json()
    assert report["result"] == "capacity_absent"
    assert report["absent"] == ["DOCCHAT_OPENAI_SMALL_CAPACITY"]
    assert report["smallShare"] is None
    assert report["chatShare"] == 0.24
    if code:
        assert "DOCCHAT_OPENAI_SMALL_CAPACITY absent" in outcome.failure


def test_an_output_missing_from_the_file_counts_as_absent(seeded: OpsHarness) -> None:
    path = seeded.root / ".azure" / "prod" / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(line for line in lines if not line.startswith("DOCCHAT_OPENAI_CHAT_CAPACITY")),
        encoding="utf-8",
    )
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.NOT_FOUND
    assert outcome.json()["absent"] == ["DOCCHAT_OPENAI_CHAT_CAPACITY"]


def test_an_exceeded_share_wins_over_an_absent_capacity(seeded: OpsHarness) -> None:
    seeded.write_azd("prod", DOCCHAT_OPENAI_SMALL_CAPACITY="")
    outcome = _capacity(seeded, "prod", "--set", "ratelimit.chat_requests_per_minute=4")
    assert outcome.code == ExitCode.USAGE
    assert outcome.json()["result"] == "exceeded"


def test_t123_an_unreachable_store_exits_7(seeded: OpsHarness) -> None:
    seeded.database.unit_errors["chat_ops_config_read"] = SqlUnavailable("connection")
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.UNREACHABLE
    assert seeded.database.disposed


def test_t542_capacity_check_accepts_a_service_principal(seeded: OpsHarness) -> None:
    seeded.az.on("account", "show", stdout="servicePrincipal\n")
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert seeded.az.calls == []


def test_the_text_report_prints_both_shares(seeded: OpsHarness) -> None:
    outcome = seeded.run("--env", "dev", "chat", "capacity-check")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    lines = outcome.stdout.splitlines()
    assert lines[0] == "result: passed"
    assert "chatShare: 0.24" in lines
    assert "smallShare: 0.2479" in lines


@pytest.mark.parametrize(
    "value",
    [
        "ratelimit.chat_requests_per_minute=0",
        "chat.answer_max_completion_tokens=35000",
        'ratelimit.chat_requests_per_minute="4"',
        "quota.tokens_per_month=10000",
    ],
)
@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_a_set_value_its_model_refuses_exits_2(
    seeded: OpsHarness, value: str, environment: str
) -> None:
    outcome = _capacity(seeded, environment, "--set", value)
    assert outcome.code == ExitCode.USAGE
    assert "refuses the --set value" in outcome.failure
    assert outcome.stdout == ""


@pytest.mark.parametrize(
    "value",
    [
        "chat.unknown_key=4",
        "ratelimit.chat_requests_per_minute",
        "ratelimit.x=4",
        "chat.max_turn_seconds=NaN",
        "=4",
    ],
)
def test_a_malformed_set_exits_2_before_any_read(ops: OpsHarness, value: str) -> None:
    outcome = _capacity(ops, "prod", "--set", value)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0


def test_a_set_value_that_isnt_json_exits_2(ops: OpsHarness) -> None:
    outcome = _capacity(ops, "prod", "--set", "ratelimit.chat_requests_per_minute=four")
    assert outcome.code == ExitCode.USAGE
    assert "JSON value" in outcome.failure


def test_a_key_set_twice_exits_2(seeded: OpsHarness) -> None:
    outcome = _capacity(
        seeded,
        "prod",
        "--set",
        "ratelimit.chat_requests_per_minute=2",
        "--set",
        "ratelimit.chat_requests_per_minute=3",
    )
    assert outcome.code == ExitCode.USAGE
    assert seeded.credentials_made == 0


def test_several_proposals_and_another_area(seeded: OpsHarness) -> None:
    outcome = _capacity(
        seeded,
        "prod",
        "--set",
        "summary.max_input_tokens=20000",
        "--set",
        "api.write_requests_per_minute=120",
        "--set",
        "ratelimit.chat_requests_per_minute=2",
    )
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["smallChargeTokens"] == 51_790
    assert report["requestsPerMinute"] == 2
    assert report["proposed"] == [
        "api.write_requests_per_minute",
        "ratelimit.chat_requests_per_minute",
        "summary.max_input_tokens",
    ]


def test_a_proposed_effort_is_checked_against_the_chat_small_row(
    seeded: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = dict(MODEL_CAPABILITIES)
    row = rows["gpt-5.6-terra"]
    rows["gpt-5.6-terra"] = replace(row, reasoning_efforts=row.reasoning_efforts - {"none"})
    monkeypatch.setattr(chat, "MODEL_CAPABILITIES", rows)
    assert _capacity(seeded, "prod", "--set", 'memory.reasoning_effort="low"').code == 0
    outcome = _capacity(seeded, "prod", "--set", 'memory.reasoning_effort="none"')
    assert outcome.code == ExitCode.USAGE
    assert "refuses the --set value of memory.reasoning_effort" in outcome.failure


def test_stored_values_that_dont_validate_exit_2(seeded: OpsHarness) -> None:
    seeded.database.seed("chat.context_token_budget", 10)
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.USAGE
    assert "stored values of the chat area" in outcome.failure


def test_a_stored_value_that_isnt_json_is_named(seeded: OpsHarness) -> None:
    seeded.database.ops_rows["followups.source_count"]["value_json"] = "not json"
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.USAGE
    assert "followups.source_count" in outcome.failure


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5"])
def test_a_capacity_that_isnt_a_unit_count_exits_2(seeded: OpsHarness, value: str) -> None:
    seeded.write_azd("dev", DOCCHAT_OPENAI_CHAT_CAPACITY=value)
    outcome = _capacity(seeded, "dev")
    assert outcome.code == ExitCode.USAGE
    assert "DOCCHAT_OPENAI_CHAT_CAPACITY" in outcome.failure
    assert seeded.credentials_made == 0


def test_a_chat_small_model_without_a_row_exits_2(seeded: OpsHarness) -> None:
    deployments = {"chatSmall": {"name": "chat-small", "model": "gpt-4o"}}
    seeded.write_checkout_json("infra/env/prod.json", {"openAi": {"deployments": deployments}})
    outcome = _capacity(seeded, "prod")
    assert outcome.code == ExitCode.USAGE
    assert "no row in the capability table" in outcome.failure


@pytest.mark.parametrize("document", [{}, {"openAi": {"deployments": {"chatSmall": {}}}}])
def test_an_environment_file_without_the_chat_small_model_exits_2(
    seeded: OpsHarness, document: dict[str, Any]
) -> None:
    seeded.write_checkout_json("infra/env/prod.json", document)
    assert _capacity(seeded, "prod").code == ExitCode.USAGE


def test_the_chat_small_efforts(ops: OpsHarness) -> None:
    assert chat.chat_small_efforts(ops.root, "dev") == {"none", "low", "medium", "high"}


# --- model-check -------------------------------------------------------------------------------

SMALL = "chat-small"


def _completion(
    content: str | None,
    *,
    finish: str = "stop",
    prompt_tokens: int = 10,
    refusal: str | None = None,
    usage: bool = True,
) -> ChatCompletion:
    document: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5.6",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }
    if usage:
        document["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 1,
            "total_tokens": prompt_tokens + 1,
        }
    return ChatCompletion.model_validate(document)


STRUCTURED = {
    "standalone_query": '{"query": "hotel limit for long head-office trips"}',
    "search_passage": '{"passage": "Hotels for head-office trips are limited."}',
    "followup_questions": '{"questions": ["Who books serviced apartments?"]}',
    "memory_operations": (
        '{"operations": [{"action": "add", "targetFactId": null, "category": "role", '
        '"text": "Works as a site safety coordinator", "sensitivity": "none", '
        '"addressesAssistant": false}]}'
    ),
}


class _Words:
    """One token per whitespace-separated word, as the harness's encodings count."""

    def encode(self, text: str, *, disallowed_special: Any) -> list[int]:
        del disallowed_special
        return list(range(len(text.split())))


def _encoder() -> chat.Encoder:
    return _Words()


def _kind(params: dict[str, Any]) -> str:
    response_format = params.get("response_format")
    if response_format is not None:
        return str(response_format["json_schema"]["name"])
    if params["max_completion_tokens"] == chat.TOKENIZER_PROBE_MAX_COMPLETION_TOKENS:
        return "tokenizer"
    if params["messages"][0]["content"] == chat.EFFORT_PROBE_SYSTEM:
        return "effort"
    return "summary"


def answer(params: dict[str, Any]) -> ChatCompletion:
    """A deployment that accepts every call and answers it as the check expects."""
    kind = _kind(params)
    if kind in STRUCTURED:
        return _completion(STRUCTURED[kind])
    if kind == "tokenizer":
        return _completion(
            "counted", prompt_tokens=chat.count_messages(_encoder(), params["messages"])
        )
    return _completion("ready" if kind == "effort" else "The user asked about hotel limits.")


def _shield(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["userPrompt"]:
        return httpx.Response(200, json={"userPromptAnalysis": {"attackDetected": False}})
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": True}],
        },
    )


SHIELD_URL = "https://cs-docchat-dev-abc123.cognitiveservices.azure.com/contentsafety/"


@pytest.fixture
def checking(seeded: OpsHarness) -> OpsHarness:
    seeded.model.responder = answer
    seeded.http.route("POST", SHIELD_URL, _shield)
    return seeded


def _check(ops: OpsHarness, *extra: str) -> Outcome:
    return ops.run("--env", "dev", "--json", "chat", "model-check", *TICKET, *REASON, *extra)


def _metadata(ops: OpsHarness) -> dict[str, Any]:
    (row,) = ops.database.audit_rows
    assert row["action"] == "model_deployments.checked"
    assert row["actor_type"] == "operator"
    assert str(row["actor_object_id"]) == ops.operator_oid
    assert row["target_type"] == "model_deployments"
    assert row["target_id"] is None
    metadata: dict[str, Any] = json.loads(row["metadata_json"])
    ModelDeploymentsCheckedMetadata.model_validate(metadata)
    return metadata


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    response: Any = SimpleNamespace(status_code=status, headers={}, request=None)
    return cls("refused", response=response, body=None)


def test_t250_a_passing_check_writes_its_row(checking: OpsHarness) -> None:
    outcome = _check(checking)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["result"] == "passed"
    # 1 effort on chat, 2 on chat small (none; low for summary and memory), 3 schemas, 10 memory
    # questions, 2 tokenizer probes, 20 summary-shaped and 20 memory-shaped calls.
    assert report["callCount"] == 58
    kinds = [call["kind"] for call in report["calls"]]
    assert kinds[:3] == ["effort"] * 3
    assert kinds[3:6] == ["standalone_query", "search_passage", "followup_questions"]
    assert kinds[6:16] == ["memory_extraction"] * 10
    assert kinds[16:18] == ["tokenizer"] * 2
    assert kinds[18:38] == ["summary_latency"] * 20
    assert kinds[38:] == ["memory_latency"] * 20
    assert [(call["deployment"], call["effort"]) for call in report["calls"][:3]] == [
        ("chat", "low"),
        (SMALL, "none"),
        (SMALL, "low"),
    ]
    metadata = _metadata(checking)
    assert metadata["environment"] == "dev"
    assert metadata["result"] == "passed"
    assert metadata["callCount"] == 58
    assert metadata["failures"] == []
    assert [probe["deployment"] for probe in metadata["tokenizer"]] == ["chat", SMALL]
    assert [probe["call"] for probe in metadata["latency"]] == ["summary", "memory"]
    assert [probe["calls"] for probe in metadata["latency"]] == [20, 20]
    # T-331: the row's latency member holds both p95 values beside their timeouts.
    assert [probe["timeoutSeconds"] for probe in metadata["latency"]] == [30, 20]
    assert all(probe["p95Ms"] <= probe["timeoutSeconds"] * 1000 for probe in metadata["latency"])
    assert [(probe["probe"], probe["outcome"]) for probe in metadata["promptShields"]] == [
        ("request", "passed"),
        ("empty_user_prompt", "passed"),
    ]
    assert metadata["ticket"] == "INC-1042"
    assert checking.model.closed
    assert checking.model.endpoints == [checking.openai_endpoint]
    assert checking.database.disposed


def test_t250_every_call_sends_store_false_and_its_settings(checking: OpsHarness) -> None:
    assert _check(checking).code == ExitCode.SUCCESS
    expected_identifier = chat.safety_identifier(checking.tenant_id, checking.operator_oid)
    assert len(expected_identifier) == 32
    for params in checking.model.requests:
        assert params["store"] is False
        assert params["safety_identifier"] == expected_identifier
        assert params["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}
        assert params["reasoning_effort"] in {"none", "low"}
        assert {"temperature", "top_p", "tools", "max_tokens", "user"}.isdisjoint(params)
        assert {message["role"] for message in params["messages"]} <= {"system", "user"}
    formats = [params["response_format"] for params in checking.model.requests[3:16]]
    assert all(item["type"] == "json_schema" for item in formats)
    assert all(item["json_schema"]["strict"] is True for item in formats)
    assert formats[0]["json_schema"]["schema"] == chat.STANDALONE_QUERY_SCHEMA
    assert formats[3]["json_schema"]["schema"] == chat.MEMORY_OPERATIONS_SCHEMA


def test_the_calls_take_the_ops_config_values(checking: OpsHarness) -> None:
    assert _check(checking).code == ExitCode.SUCCESS
    requests = checking.model.requests
    assert [params["max_completion_tokens"] for params in requests[3:6]] == [800] * 3
    assert {params["max_completion_tokens"] for params in requests[6:16]} == {1200}
    summary = requests[18]
    assert summary["max_completion_tokens"] == 2000
    assert summary["reasoning_effort"] == "low"
    assert "Use at most 360 words." in summary["messages"][0]["content"]
    assert "Return at most 3 operations." in requests[6]["messages"][0]["content"]
    assert "Propose at most 3 questions" in requests[5]["messages"][0]["content"]
    # The fold set fills summary.max_input_tokens in the chat-small encoding.
    data = summary["messages"][1]["content"]
    assert 11_800 < len(data.split()) <= 12_000 + 8
    assert checking.encodings == ["o200k_base"]


def test_t250_a_400_fails_the_check_and_the_row_records_it(checking: OpsHarness) -> None:
    def refuse_none(params: dict[str, Any]) -> ChatCompletion | BaseException:
        if params["reasoning_effort"] == "none":
            return _status_error(openai.BadRequestError, 400)
        return answer(params)

    checking.model.responder = refuse_none
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert "the audit row records it as failed" in outcome.failure
    assert outcome.json()["result"] == "failed"
    metadata = _metadata(checking)
    assert metadata["result"] == "failed"
    assert metadata["failures"][0] == {
        "deployment": SMALL,
        "call": "effort",
        "reasoningEffort": "none",
        "status": 400,
    }
    # The effort probe, the three small-call schemas and the tokenizer probe use `none`.
    assert [failure["call"] for failure in metadata["failures"]] == [
        "effort",
        "standalone_query",
        "search_passage",
        "followup_questions",
        "tokenizer",
    ]
    assert outcome.json()["failureCount"] == 5


def test_failures_beyond_ten_are_counted_not_recorded(checking: OpsHarness) -> None:
    checking.model.responder = lambda params: _status_error(openai.InternalServerError, 503)
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert len(metadata["failures"]) == 10
    assert outcome.json()["failureCount"] == 58
    assert metadata["tokenizer"] == []
    assert metadata["latency"] == []


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        ("standalone_query", '{"query": 4}'),
        ("search_passage", '{"passage": "x", "extra": 1}'),
        ("followup_questions", "not json"),
        ("memory_operations", '{"operations": [{"action": "delete"}]}'),
    ],
)
def test_t250_a_structured_output_outside_its_schema_fails(
    checking: OpsHarness, schema: str, content: str
) -> None:
    def malformed(params: dict[str, Any]) -> ChatCompletion:
        if _kind(params) == schema:
            return _completion(content)
        return answer(params)

    checking.model.responder = malformed
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    failure = _metadata(checking)["failures"][0]
    assert failure["status"] == 0
    assert failure["deployment"] == SMALL


def test_a_refusal_is_no_structured_output(checking: OpsHarness) -> None:
    def refusing(params: dict[str, Any]) -> ChatCompletion:
        if _kind(params) == "standalone_query":
            return _completion(None, refusal="I can't help with that.")
        return answer(params)

    checking.model.responder = refusing
    assert _check(checking).code == ExitCode.USAGE
    assert _metadata(checking)["failures"][0]["call"] == "standalone_query"


@pytest.mark.parametrize(("offset", "code"), [(0, 0), (1, 0), (-1, 0), (60, 2)])
def test_t251_the_tokenizer_count_within_1_percent(
    checking: OpsHarness, offset: int, code: int
) -> None:
    def counted(params: dict[str, Any]) -> ChatCompletion:
        if _kind(params) == "tokenizer":
            local = chat.count_messages(_encoder(), params["messages"])
            return _completion("counted", prompt_tokens=local + offset)
        return answer(params)

    checking.model.responder = counted
    outcome = _check(checking)
    assert outcome.code == code, outcome.stderr
    tokenizer = _metadata(checking)["tokenizer"]
    assert [probe["reportedTokens"] - probe["localTokens"] for probe in tokenizer] == [offset] * 2
    assert all(probe["localTokens"] > 1_200 for probe in tokenizer)


def test_a_tokenizer_probe_without_usage_fails(checking: OpsHarness) -> None:
    def no_usage(params: dict[str, Any]) -> ChatCompletion:
        if _kind(params) == "tokenizer":
            return _completion("counted", usage=False)
        return answer(params)

    checking.model.responder = no_usage
    assert _check(checking).code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert metadata["tokenizer"] == []
    assert [failure["call"] for failure in metadata["failures"]] == ["tokenizer"] * 2


def _timed(ops: OpsHarness, seconds: list[float], kind: str) -> None:
    pending = list(seconds)

    def slow(params: dict[str, Any]) -> ChatCompletion:
        is_summary = _kind(params) == "summary" and params["max_completion_tokens"] == 2000
        is_memory = _kind(params) == "memory_operations"
        if (kind == "summary" and is_summary) or (kind == "memory" and is_memory and pending):
            ops.clock.current += timedelta(seconds=pending.pop(0))
        return answer(params)

    ops.model.responder = slow


@pytest.mark.parametrize(
    ("slow_calls", "code", "p95_ms"),
    [(1, 0, 10_000), (2, 2, 100_000)],
)
def test_t331_the_p95_is_the_19th_of_20_durations(
    checking: OpsHarness, slow_calls: int, code: int, p95_ms: int
) -> None:
    _timed(checking, [10.0] * (20 - slow_calls) + [100.0] * slow_calls, "summary")
    outcome = _check(checking)
    assert outcome.code == code, outcome.stderr
    (summary, _) = _metadata(checking)["latency"]
    assert summary == {"call": "summary", "calls": 20, "p95Ms": p95_ms, "timeoutSeconds": 30}


def test_t331_a_memory_latency_above_its_timeout_fails(checking: OpsHarness) -> None:
    # The 10 memory-extraction calls come first; then the 20 memory-shaped latency calls.
    _timed(checking, [0.0] * 10 + [25.0] * 20, "memory")
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert metadata["latency"][1]["p95Ms"] == 25_000
    assert metadata["failures"] == [
        {"deployment": SMALL, "call": "memory_latency", "reasoningEffort": "low", "status": 0}
    ]


def test_t331_a_summary_answer_that_isnt_accepted_fails(checking: OpsHarness) -> None:
    def cut(params: dict[str, Any]) -> ChatCompletion:
        if _kind(params) == "summary" and params["max_completion_tokens"] == 2000:
            return _completion("The user", finish="length")
        return answer(params)

    checking.model.responder = cut
    assert _check(checking).code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert len(metadata["failures"]) == 10
    assert metadata["failures"][0]["call"] == "summary_latency"
    # Every call answered, so the set's p95 is still recorded.
    assert [probe["call"] for probe in metadata["latency"]] == ["summary", "memory"]


def test_t331_a_failed_latency_call_records_no_p95(checking: OpsHarness) -> None:
    calls = {"summary": 0}

    def one_fails(params: dict[str, Any]) -> ChatCompletion | BaseException:
        if _kind(params) == "summary" and params["max_completion_tokens"] == 2000:
            calls["summary"] += 1
            if calls["summary"] == 5:
                return _status_error(openai.InternalServerError, 500)
        return answer(params)

    checking.model.responder = one_fails
    assert _check(checking).code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert [probe["call"] for probe in metadata["latency"]] == ["memory"]
    assert metadata["failures"][0]["status"] == 500


def test_t250_distinct_efforts_each_get_a_call(checking: OpsHarness) -> None:
    checking.database.seed("summary.reasoning_effort", "medium")
    outcome = _check(checking)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["callCount"] == 59
    assert [call["effort"] for call in report["calls"][:4]] == ["low", "none", "medium", "low"]


@pytest.mark.parametrize(
    ("answer", "probe", "outcome_name", "code"),
    [
        ((400, {"error": {"code": "InvalidRequestBody"}}), "request", "documents_refused", 0),
        ((400, {"error": {"code": "InvalidRequestBody"}}), "empty", "user_prompt_refused", 0),
        ((200, {"userPromptAnalysis": {"attackDetected": True}}), "request", "failed", 2),
        ((200, {"userPromptAnalysis": {}}), "request", "failed", 2),
        ((200, {"documentsAnalysis": [{"attackDetected": False}]}), "empty", "failed", 2),
        ((200, {"documentsAnalysis": []}), "empty", "failed", 2),
        ((429, {}), "request", "failed", 2),
        ((500, {}), "empty", "failed", 2),
    ],
)
def test_t332_the_prompt_shields_probes(
    checking: OpsHarness,
    answer: tuple[int, dict[str, Any]],
    probe: str,
    outcome_name: str,
    code: int,
) -> None:
    status, body = answer

    def respond(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        is_request = sent["userPrompt"] != ""
        if is_request == (probe == "request"):
            return httpx.Response(status, json=body)
        return _shield(request)

    checking.http.route("POST", SHIELD_URL, respond)
    outcome = _check(checking)
    assert outcome.code == code, outcome.stderr
    probes = _metadata(checking)["promptShields"]
    index = 0 if probe == "request" else 1
    assert probes[index]["status"] == status
    assert probes[index]["outcome"] == outcome_name
    assert probes[1 - index]["outcome"] == "passed"


def test_the_probes_send_the_specified_bodies(checking: OpsHarness) -> None:
    assert _check(checking).code == ExitCode.SUCCESS
    requests = checking.http.sent("POST", SHIELD_URL)
    assert [json.loads(request.content) for request in requests] == [
        {"userPrompt": "Works on supplier contract renewals", "documents": []},
        {"userPrompt": "", "documents": [chat.SHIELD_PROBE_DOCUMENT]},
    ]
    for request in requests:
        assert request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.url.params["api-version"] == "2024-09-01"
        assert request.headers["Authorization"] == (
            "Bearer token-for-https://cognitiveservices.azure.com/.default"
        )
    assert chat.SHIELD_PROBE_DOCUMENT.startswith("Supplier onboarding checklist, revision 4.")


def test_a_probe_timeout_fails_with_status_0(checking: OpsHarness) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    checking.http.route("POST", SHIELD_URL, slow)
    assert _check(checking).code == ExitCode.USAGE
    metadata = _metadata(checking)
    assert [probe["status"] for probe in metadata["promptShields"]] == [0, 0]
    assert metadata["failures"][0] == {
        "deployment": "contentsafety",
        "call": "shield_request",
        "reasoningEffort": None,
        "status": 0,
    }


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_content_safety_call_exits_3_without_a_row(
    checking: OpsHarness, status: int
) -> None:
    checking.http.answer("POST", SHIELD_URL, status, {})
    outcome = _check(checking)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert checking.database.audit_rows == []


def test_an_unreachable_content_safety_account_exits_7_without_a_row(
    checking: OpsHarness,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    checking.http.route("POST", SHIELD_URL, refuse)
    outcome = _check(checking)
    assert outcome.code == ExitCode.UNREACHABLE
    assert checking.database.audit_rows == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (_status_error(openai.AuthenticationError, 401), ExitCode.ACCESS_DENIED),
        (_status_error(openai.PermissionDeniedError, 403), ExitCode.ACCESS_DENIED),
        (openai.APIConnectionError(request=SimpleNamespace()), ExitCode.UNREACHABLE),  # type: ignore[arg-type]
    ],
)
def test_a_refused_or_unreachable_openai_account_writes_no_row(
    checking: OpsHarness, error: BaseException, code: int
) -> None:
    checking.model.responder = lambda params: error
    outcome = _check(checking)
    assert outcome.code == code
    assert checking.database.audit_rows == []
    assert len(checking.model.requests) == 1
    assert checking.model.closed


class ConnectTimeout(Exception):  # noqa: N818 - named as the transport's exception
    pass


class ReadTimeout(Exception):  # noqa: N818 - named as the transport's exception
    pass


def _timeout(cause: Exception) -> openai.APITimeoutError:
    # As `raise ... from cause` leaves it.
    error = openai.APITimeoutError(request=SimpleNamespace())  # type: ignore[arg-type]
    error.__cause__ = cause
    return error


def test_a_connect_timeout_is_an_unreachable_account(checking: OpsHarness) -> None:
    error = _timeout(ConnectTimeout())
    checking.model.responder = lambda params: error
    assert _check(checking).code == ExitCode.UNREACHABLE
    assert checking.database.audit_rows == []


def test_a_read_timeout_is_a_failed_call(checking: OpsHarness) -> None:
    error = _timeout(ReadTimeout())

    def slow_chat(params: dict[str, Any]) -> ChatCompletion | BaseException:
        return error if params["model"] == "chat" else answer(params)

    checking.model.responder = slow_chat
    assert _check(checking).code == ExitCode.USAGE
    failures = _metadata(checking)["failures"]
    assert failures[0] == {
        "deployment": "chat",
        "call": "effort",
        "reasoningEffort": "low",
        "status": 0,
    }
    assert [failure["call"] for failure in failures] == ["effort", "tokenizer"]


def test_model_check_refuses_a_service_principal(checking: OpsHarness) -> None:
    checking.az.on("account", "show", stdout="servicePrincipal\n")
    outcome = _check(checking)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert checking.credentials_made == 0
    assert checking.model.requests == []
    assert checking.database.audit_rows == []


@pytest.mark.parametrize("environment", ["prod", "load"])
def test_model_check_runs_in_dev_only(checking: OpsHarness, environment: str) -> None:
    outcome = checking.run("--env", environment, "chat", "model-check", *TICKET, *REASON)
    assert outcome.code == ExitCode.USAGE
    assert "use --env dev" in outcome.failure
    assert checking.az.calls == []


@pytest.mark.parametrize(
    "name", ["DOCCHAT_OPENAI_ENDPOINT", "DOCCHAT_CONTENTSAFETY_ENDPOINT", "AZURE_TENANT_ID"]
)
def test_a_missing_endpoint_exits_2(checking: OpsHarness, name: str) -> None:
    checking.write_azd("dev", **{name: ""})
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert f"{name} is missing" in outcome.failure
    assert checking.az.calls == []


def test_an_endpoint_that_isnt_https_exits_2(checking: OpsHarness) -> None:
    checking.write_azd("dev", DOCCHAT_OPENAI_ENDPOINT="http://oai.example")
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert "isn't an https endpoint" in outcome.failure


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"DOCCHAT_OPENAI_SMALL_REASONING_EFFORT": "xhigh"}, "isn't one gpt-5.6-terra accepts"),
        ({"DOCCHAT_OPENAI_CHAT_PROMPT_CACHE": "sometimes"}, "isn't one gpt-5.6-sol accepts"),
        ({"DOCCHAT_OPENAI_CHAT_REASONING_EFFORT": ""}, "isn't a name"),
    ],
)
def test_app_settings_outside_the_capability_table_exit_2(
    checking: OpsHarness, settings: dict[str, str], message: str
) -> None:
    checking.write_app_settings({**_api_settings(), **settings})
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert checking.model.requests == []


def _api_settings() -> dict[str, str]:
    return {
        "DOCCHAT_OPENAI_CHAT_REASONING_EFFORT": "low",
        "DOCCHAT_OPENAI_SMALL_REASONING_EFFORT": "none",
        "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE": "disabled",
        "DOCCHAT_OPENAI_SMALL_PROMPT_CACHE": "disabled",
    }


def _deployments(**chat_changes: object) -> dict[str, Any]:
    return {
        "openAi": {
            "deployments": {
                "chat": {
                    "name": "chat",
                    "model": "gpt-5.6-sol",
                    "encoding": "o200k_base",
                    **chat_changes,
                },
                "chatSmall": {"name": SMALL, "model": "gpt-5.6-terra", "encoding": "cl100k_base"},
            }
        }
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model": "gpt-4o"}, "has no row in the capability table"),
        ({"encoding": "p50k_base"}, "isn't one in use"),
        ({"name": "c"}, "isn't a deployment name"),
        ({"name": "chat deployment"}, "isn't a deployment name"),
    ],
)
def test_deployments_outside_the_table_exit_2(
    checking: OpsHarness, changes: dict[str, object], message: str
) -> None:
    checking.write_checkout_json("infra/env/dev.json", _deployments(**changes))
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure


def test_a_disabled_cache_needs_prompt_cache_options(
    checking: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = dict(MODEL_CAPABILITIES)
    rows["gpt-5.6-sol"] = replace(rows["gpt-5.6-sol"], prompt_cache_options=False)
    monkeypatch.setattr(chat, "MODEL_CAPABILITIES", rows)
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert "takes no prompt_cache_options" in outcome.failure
    checking.write_app_settings(
        {**_api_settings(), "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE": "automatic"}
    )
    outcome = _check(checking)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "extra_body" not in checking.model.requests[0]
    assert checking.model.requests[1]["extra_body"] == {
        "prompt_cache_options": {"mode": "explicit"}
    }


def test_each_deployment_is_counted_in_its_own_encoding(checking: OpsHarness) -> None:
    checking.write_checkout_json("infra/env/dev.json", _deployments())
    assert _check(checking).code == ExitCode.SUCCESS
    assert sorted(checking.encodings) == ["cl100k_base", "o200k_base"]


def test_the_stored_efforts_are_checked_against_the_chat_small_row(checking: OpsHarness) -> None:
    checking.database.seed("memory.reasoning_effort", "high")
    outcome = _check(checking)
    assert outcome.code == ExitCode.USAGE
    assert "memory.reasoning_effort" in outcome.failure
    assert checking.model.requests == []


def test_no_openai_token_exits_3_before_any_call(checking: OpsHarness) -> None:
    checking.credential.refused.add("https://ai.azure.com/.default")
    assert _check(checking).code == ExitCode.ACCESS_DENIED
    assert checking.model.requests == []


# --- the pieces --------------------------------------------------------------------------------


def test_the_counting_rule() -> None:
    messages = [{"role": "system", "content": "a b c"}, {"role": "user", "content": "d e"}]
    assert chat.count_messages(_encoder(), messages) == 3 + (3 + 3) + (3 + 2)


def test_the_safety_identifier() -> None:

    tenant, oid = "44444444-4444-4444-8444-444444444444", "11111111-1111-4111-8111-111111111111"
    expected = hashlib.sha256(f"docchat:{tenant}:{oid}".encode()).hexdigest()[:32]
    assert chat.safety_identifier(tenant, oid) == expected


def test_a_marker_that_collides_is_drawn_again() -> None:
    draws = iter(["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])
    marker = chat.draw_marker(["text AAAAAAAAAAAAAAAA"], lambda size: next(draws))
    assert marker == "bbbbbbbbbbbbbbbb"


def test_a_fourth_collision_stops_the_check() -> None:
    with pytest.raises(UnexpectedError):
        chat.draw_marker(["cccccccccccccccc"], lambda size: "c" * size * 2)


def test_markers_are_16_hexadecimal_characters() -> None:
    marker = chat.draw_marker(["text"])
    assert len(marker) == 16
    assert set(marker) <= set("0123456789abcdef")


def test_the_blocks_and_messages() -> None:
    assert chat.block("SOURCE S1", "m", "x") == "[[BEGIN SOURCE S1 m]]\nx\n[[END SOURCE S1 m]]"
    rewrite = chat.rewrite_messages()
    assert rewrite[0]["content"].endswith(
        "Answer with a JSON object that matches the response schema."
    )
    assert "<marker>" not in rewrite[0]["content"]
    assert "[[BEGIN <KIND> <MARKER>]]" in rewrite[0]["content"]
    assert rewrite[1]["content"].startswith("[[BEGIN EARLIER_EXCHANGE ")
    passage = chat.search_passage_messages()
    assert passage[1]["content"].startswith("[[BEGIN SEARCH_QUERY ")
    followups = chat.followups_messages(2)
    assert "Propose at most 2 questions" in followups[0]["content"]
    assert "Section: Accommodation > Limits" in followups[1]["content"]
    memory = chat.memory_messages("I lead the payroll team.", 3)
    assert memory[1]["content"].splitlines()[1] == chat.MEMORY_STORED_FACTS[0]
    assert "\nI lead the payroll team.\nUSER MESSAGE " in memory[1]["content"]
    assert len(chat.MEMORY_QUESTIONS) == 10


def test_the_fold_set_stops_at_the_limit() -> None:
    fold_set = chat.summary_fold_set(_encoder(), 1000)
    assert fold_set.startswith("Current summary:\nnone\n\nExchanges, oldest first:\n\nExchange 1\n")
    assert 950 < len(fold_set.split()) <= 1000
    assert (
        chat.summary_fold_set(_encoder(), 10)
        == "Current summary:\nnone\n\nExchanges, oldest first:"
    )


def test_the_summary_message_shape() -> None:
    system, user = chat.summary_messages("Current summary:\nnone", 800)
    assert "Use at most 360 words." in system["content"]
    lines = user["content"].splitlines()
    assert lines[0].startswith("CONVERSATION DATA ")
    assert lines[0].endswith(" BEGIN")
    assert lines[-1].endswith(" END")


def test_the_memory_schema_categories_are_the_closed_set() -> None:
    schema: Any = chat.MEMORY_OPERATIONS_SCHEMA
    item = schema["properties"]["operations"]["items"]
    assert item["properties"]["category"]["enum"] == [
        "role",
        "team",
        "expertise",
        "topic",
        "answer_style",
    ]
    assert item["required"] == list(item["properties"])


async def test_the_openai_adapter() -> None:
    sent: list[dict[str, Any]] = []
    closed: list[bool] = []

    async def create(**params: Any) -> ChatCompletion:
        sent.append(params)
        return _completion("ready")

    async def close() -> None:
        closed.append(True)

    client: Any = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close
    )
    model = chat.OpenAiChatModel(client)
    assert (await model.complete({"model": "chat"})).choices[0].message.content == "ready"
    await model.close()
    assert sent == [{"model": "chat"}]
    assert closed == [True]


async def test_the_openai_adapter_refuses_a_stream() -> None:
    async def create(**params: Any) -> object:
        return object()

    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(UnexpectedError):
        await chat.OpenAiChatModel(client).complete({})


async def test_the_production_model_is_the_api_profile(
    fake_credential: Any,
) -> None:
    model = chat.openai_chat_model(
        "https://oai-docchat-dev-abc123.openai.azure.com", fake_credential
    )
    assert isinstance(model, chat.OpenAiChatModel)
    client = model._client
    try:
        assert str(client.base_url) == "https://oai-docchat-dev-abc123.openai.azure.com/openai/v1/"
        assert client.max_retries == 0
        assert (client.timeout.connect, client.timeout.read) == (10.0, 60.0)  # type: ignore[union-attr]
    finally:
        await model.close()


def test_tiktoken_is_loaded_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=loaded.append))
    chat.tiktoken_encoding("o200k_base")
    assert loaded == ["o200k_base"]


def test_the_tokenizer_probe_is_about_2000_tokens() -> None:
    words = len(chat.TOKENIZER_PROBE_TEXT.split())
    assert 1_200 < words < 1_800
