"""Unit tests of `scripts/deploy/firewall.py` (T-476, `83-infrastructure.md` section 4.6)."""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = "203.0.113.7"
OTHER_RUNNER = "198.51.100.20"
VAULT = "kv-docchat-tst-abc123"
SERVER = "sql-docchat-dev-abc123"
REGISTRY = "crdocchatabc123"
START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


firewall = _load_script("scripts/deploy/firewall.py", "docchat_script_firewall")


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return START + timedelta(seconds=self.elapsed)


class FakeAzure:
    """Recorded answers of the Azure CLI calls `firewall.py` makes, over in-memory rule lists."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.sql_rules: list[str] = []
        self.registry_rules: list[str] = []
        self.vault_rules: list[str] = []
        self.ignored_vault_writes = 0
        self.stuck_vault_removals = False
        self.vault_list_hook: Callable[[int], None] | None = None
        self.vault_lists = 0
        self.secret_answers: list[Any] = []

    def __call__(self, args: Sequence[str]) -> Any:
        words = list(args)
        self.calls.append(words)
        joined = " ".join(words)
        routes: list[tuple[str, Callable[[list[str]], Any]]] = [
            ("az account get-access-token", lambda w: self._ok("header.payload.signature\n")),
            ("az sql server list", lambda w: self._ok(json.dumps([self._server()]))),
            ("az sql server firewall-rule", lambda w: self._sql(w[4], w)),
            ("az acr list", lambda w: self._ok(json.dumps([REGISTRY]))),
            ("az acr network-rule", lambda w: self._registry(w[3], w)),
            ("az keyvault network-rule", lambda w: self._vault(w[3], w)),
            ("az keyvault secret list", lambda w: self._secret()),
        ]
        for prefix, answer in routes:
            if joined.startswith(prefix):
                return answer(words)
        raise AssertionError(f"unexpected command {joined}")

    @staticmethod
    def _ok(stdout: str = "") -> Any:
        return firewall.CommandResult(0, stdout, "")

    @staticmethod
    def _server() -> dict[str, str]:
        return {"name": SERVER, "fqdn": f"{SERVER}.database.windows.net"}

    def _secret(self) -> Any:
        return self.secret_answers.pop(0) if self.secret_answers else self._ok()

    @staticmethod
    def _value(words: list[str], option: str) -> str:
        return words[words.index(option) + 1]

    def _sql(self, verb: str, words: list[str]) -> Any:
        result = firewall.CommandResult
        if verb == "list":
            return result(0, json.dumps(self.sql_rules), "")
        if verb == "create":
            self.sql_rules.append(self._value(words, "--name"))
        elif verb == "delete":
            self.sql_rules.remove(self._value(words, "--name"))
        return result(0, "", "")

    def _registry(self, verb: str, words: list[str]) -> Any:
        result = firewall.CommandResult
        if verb == "list":
            rules = [{"action": "Allow", "ipAddressOrRange": v} for v in self.registry_rules]
            return result(0, json.dumps({"ipRules": rules, "virtualNetworkRules": []}), "")
        if verb == "add":
            self.registry_rules.append(self._value(words, "--ip-address"))
        elif verb == "remove":
            self.registry_rules.remove(self._value(words, "--ip-address"))
        return result(0, "", "")

    def _vault(self, verb: str, words: list[str]) -> Any:
        result = firewall.CommandResult
        if verb == "list":
            self.vault_lists += 1
            if self.vault_list_hook is not None:
                self.vault_list_hook(self.vault_lists)
            rules = [{"value": value} for value in self.vault_rules]
            return result(0, json.dumps({"ipRules": rules, "virtualNetworkRules": []}), "")
        if verb == "add":
            # A concurrent job that read the list before this write and wrote after it undoes it.
            if self.ignored_vault_writes > 0:
                self.ignored_vault_writes -= 1
            else:
                self.vault_rules.append(self._value(words, "--ip-address"))
        elif verb == "remove" and not self.stuck_vault_removals:
            self.vault_rules.remove(self._value(words, "--ip-address"))
        return result(0, "", "")

    def count(self, prefix: str) -> int:
        return sum(1 for call in self.calls if " ".join(call).startswith(prefix))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(firewall, "sleep", fake.sleep)
    monkeypatch.setattr(firewall, "monotonic", fake.monotonic)
    monkeypatch.setattr(firewall, "now", fake.now)
    monkeypatch.setattr(firewall, "jitter", lambda low, high: (low + high) / 2)
    return fake


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> FakeAzure:
    fake = FakeAzure()
    monkeypatch.setattr(firewall, "run_command", fake)
    return fake


@pytest.fixture
def job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock) -> Path:
    """A GitHub Actions job on a runner whose public address is RUNNER."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "100")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("DOCCHAT_TEST_VAULT_URI", f"https://{VAULT}.vault.azure.net/")
    monkeypatch.delenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", raising=False)
    monkeypatch.delenv("DOCCHAT_OPERATOR_IP_ALLOWLIST", raising=False)
    monkeypatch.setattr(firewall, "fetch_text", lambda url, timeout: f"{RUNNER}\n")
    return tmp_path


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    parsed: dict[str, str] = json.loads(lines[0])
    return parsed


# --- arguments and the runner's address -----------------------------------------------------


@pytest.mark.parametrize("target", ["cosmos", "storage", "keyvault", ""])
def test_t476_only_three_targets_are_accepted(target: str) -> None:
    with pytest.raises(SystemExit) as raised:
        firewall.main(["open", "--target", target, "--env", "dev"])
    assert raised.value.code == 2


def test_t476_vault_target_refuses_prod() -> None:
    with pytest.raises(SystemExit) as raised:
        firewall.main(["open", "--target", "vault", "--env", "prod"])
    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ((RUNNER, OTHER_RUNNER), "runner_ip_mismatch"),
        (("10.1.2.3", "10.1.2.3"), "runner_ip_unresolved"),
        (("172.16.4.5", "172.16.4.5"), "runner_ip_unresolved"),
        (("192.168.1.1", "192.168.1.1"), "runner_ip_unresolved"),
        (("100.64.0.9", "100.64.0.9"), "runner_ip_unresolved"),
        (("127.0.0.1", "127.0.0.1"), "runner_ip_unresolved"),
        (("169.254.169.254", "169.254.169.254"), "runner_ip_unresolved"),
        (("<html>", "<html>"), "runner_ip_unresolved"),
    ],
)
@pytest.mark.parametrize("target", ["sql", "registry", "vault"])
def test_t476_disagreeing_or_non_public_addresses_exit_2_and_add_nothing(
    *,
    monkeypatch: pytest.MonkeyPatch,
    job: Path,
    azure: FakeAzure,
    capsys: pytest.CaptureFixture[str],
    answers: tuple[str, str],
    code: str,
    target: str,
) -> None:
    replies = iter(answers)
    monkeypatch.setattr(firewall, "fetch_text", lambda url, timeout: next(replies))

    assert firewall.main(["open", "--target", target, "--env", "dev"]) == 2

    assert _failure(capsys)["code"] == code
    assert azure.calls == []


def test_t476_a_failed_echo_call_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    job: Path,
    azure: FakeAzure,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(url: str, timeout: float) -> str:
        assert timeout == 5.0
        raise TimeoutError

    monkeypatch.setattr(firewall, "fetch_text", fail)

    assert firewall.main(["open", "--target", "sql", "--env", "dev"]) == 2

    assert _failure(capsys)["code"] == "runner_ip_unresolved"
    assert azure.calls == []


def test_failure_is_one_json_line_on_stderr_and_in_the_job_summary(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter((RUNNER, OTHER_RUNNER))
    monkeypatch.setattr(firewall, "fetch_text", lambda url, timeout: next(replies))

    firewall.main(["open", "--target", "registry", "--env", "dev"])

    failure = _failure(capsys)
    assert set(failure) == {"code", "message", "detail"}
    assert failure["message"] == firewall.MESSAGES["runner_ip_mismatch"]
    assert json.loads((job / "summary.md").read_text(encoding="utf-8")) == failure


# --- outside GitHub Actions -----------------------------------------------------------------


def test_open_outside_actions_adds_nothing_for_an_allowlisted_address(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.setenv("DOCCHAT_OPERATOR_IP_ALLOWLIST", f"198.51.100.1,{RUNNER}")

    assert firewall.main(["open", "--target", "sql", "--env", "dev"]) == 0
    assert azure.calls == []


def test_open_outside_actions_refuses_an_address_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", "198.51.100.1")

    assert firewall.main(["open", "--target", "registry", "--env", "dev"]) == 2

    assert _failure(capsys)["code"] == "not_in_allowlist"
    assert azure.calls == []


# --- SQL ------------------------------------------------------------------------------------


def test_t476_sql_sweep_removes_only_ci_runner_rules(job: Path, azure: FakeAzure) -> None:
    azure.sql_rules = ["operator-1", "ci-runner-90-1", "operator-2", "ci-runner-91-2"]

    assert firewall.main(["sweep", "--target", "sql", "--env", "dev"]) == 0

    assert azure.sql_rules == ["operator-1", "operator-2"]


def test_t476_sql_open_adds_the_job_rule_and_probes_until_the_login_gets_through(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    answers = iter([40615, 40615, 18456])
    logins: list[tuple[str, str, str]] = []

    def login(server: str, database: str, token: str) -> int | None:
        logins.append((server, database, token))
        return next(answers)

    monkeypatch.setattr(firewall, "tds_login", login)

    assert firewall.main(["open", "--target", "sql", "--env", "dev"]) == 0

    create = next(call for call in azure.calls if "create" in call)
    assert create[create.index("--name") + 1] == "ci-runner-100-1"
    assert create[create.index("--start-ip-address") + 1] == RUNNER
    assert create[create.index("--end-ip-address") + 1] == RUNNER
    assert azure.sql_rules == ["ci-runner-100-1"]
    assert logins[0] == (f"{SERVER}.database.windows.net", "docchat", "header.payload.signature")
    assert clock.sleeps == [15.0, 15.0]


def test_t476_sql_open_gives_up_after_300_seconds_and_removes_the_rule(
    monkeypatch: pytest.MonkeyPatch,
    job: Path,
    azure: FakeAzure,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probes: list[float] = []

    def refused(server: str, database: str, token: str) -> int:
        probes.append(clock.elapsed)
        return 40615

    monkeypatch.setattr(firewall, "tds_login", refused)

    assert firewall.main(["open", "--target", "sql", "--env", "dev"]) == 3

    assert _failure(capsys)["code"] == "rule_not_effective"
    assert probes == [15.0 * n for n in range(21)]
    assert azure.sql_rules == []


def test_sql_probe_treats_a_connection_failure_as_not_yet(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    outcomes: list[Any] = [ConnectionResetError(), None]

    def login(server: str, database: str, token: str) -> int | None:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return None

    monkeypatch.setattr(firewall, "tds_login", login)

    assert firewall.main(["open", "--target", "sql", "--env", "dev"]) == 0
    assert clock.sleeps == [15.0]


def test_t476_sql_close_removes_only_the_rule_open_added(job: Path, azure: FakeAzure) -> None:
    azure.sql_rules = ["operator-1", "ci-runner-99-1", "ci-runner-100-1"]

    assert firewall.main(["close", "--target", "sql", "--env", "dev"]) == 0

    assert azure.sql_rules == ["operator-1", "ci-runner-99-1"]


def test_sql_close_without_a_rule_or_outside_actions_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure
) -> None:
    azure.sql_rules = ["operator-1"]
    assert firewall.main(["close", "--target", "sql", "--env", "prod"]) == 0
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert firewall.main(["close", "--target", "sql", "--env", "prod"]) == 0
    assert azure.sql_rules == ["operator-1"]
    assert azure.count("az sql server firewall-rule delete") == 0


def test_sql_close_failure_exits_4(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.sql_rules = ["ci-runner-100-1"]

    def refuse_delete(args: Sequence[str]) -> Any:
        if "delete" in args:
            return firewall.CommandResult(1, "", "ERROR: (AuthorizationFailed) denied")
        return azure(args)

    monkeypatch.setattr(firewall, "run_command", refuse_delete)

    assert firewall.main(["close", "--target", "sql", "--env", "dev"]) == 4
    assert _failure(capsys)["code"] == "rule_close_failed"


# --- registry -------------------------------------------------------------------------------


def test_t476_registry_sweep_removes_only_addresses_outside_the_shared_allowlist(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure
) -> None:
    monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", "198.51.100.1, 198.51.100.2")
    azure.registry_rules = ["198.51.100.1", OTHER_RUNNER, "198.51.100.2/32"]

    assert firewall.main(["sweep", "--target", "registry", "--env", "dev"]) == 0

    assert azure.registry_rules == ["198.51.100.1", "198.51.100.2/32"]


def test_t476_registry_open_probes_v2_until_401_and_close_removes_only_its_rule(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    statuses = iter([403, 403, 401])
    urls: list[str] = []

    def status(url: str, timeout: float) -> int:
        urls.append(url)
        return next(statuses)

    monkeypatch.setattr(firewall, "http_status", status)
    azure.registry_rules = ["198.51.100.1"]

    assert firewall.main(["open", "--target", "registry", "--env", "dev"]) == 0
    assert azure.registry_rules == ["198.51.100.1", RUNNER]
    assert urls == [f"https://{REGISTRY}.azurecr.io/v2/"] * 3
    assert clock.sleeps == [15.0, 15.0]

    azure.registry_rules.append(OTHER_RUNNER)
    assert firewall.main(["close", "--target", "registry", "--env", "dev"]) == 0
    assert azure.registry_rules == ["198.51.100.1", OTHER_RUNNER]


def test_registry_open_that_never_works_removes_its_rule_and_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    job: Path,
    azure: FakeAzure,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(firewall, "http_status", lambda url, timeout: 403)

    assert firewall.main(["open", "--target", "registry", "--env", "dev"]) == 3

    assert _failure(capsys)["code"] == "rule_not_effective"
    assert azure.registry_rules == []
    assert clock.elapsed == 300.0


# --- vault ----------------------------------------------------------------------------------


def _vault_state(job: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(
        (job / "docchat-firewall-vault.json").read_text(encoding="utf-8")
    )
    return state


def test_t476_vault_open_adds_a_32_rule_and_records_its_state(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", "198.51.100.1")
    azure.vault_rules = ["198.51.100.1/32", f"{OTHER_RUNNER}/32"]

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0

    add = next(
        call for call in azure.calls if call[:4] == ["az", "keyvault", "network-rule", "add"]
    )
    assert add[add.index("--ip-address") + 1] == f"{RUNNER}/32"
    assert add[add.index("--name") + 1] == VAULT
    # No job sweeps the vault: another job's live window stays.
    assert azure.vault_rules == ["198.51.100.1/32", f"{OTHER_RUNNER}/32", f"{RUNNER}/32"]
    assert azure.count("az keyvault network-rule remove") == 0
    assert _vault_state(job) == {"address": RUNNER, "openedAt": "2026-10-30T12:00:00Z"}
    probe = next(call for call in azure.calls if call[:4] == ["az", "keyvault", "secret", "list"])
    assert probe[probe.index("--maxresults") + 1] == "1"


def test_t476_vault_open_writes_again_when_a_read_back_lacks_its_address(
    job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    azure.ignored_vault_writes = 2

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0

    assert azure.count("az keyvault network-rule add") == 3
    assert azure.vault_rules == [f"{RUNNER}/32"]
    assert clock.sleeps == [3.5, 3.5]


def test_t476_vault_open_gives_up_after_5_writes(
    job: Path, azure: FakeAzure, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.ignored_vault_writes = 5

    assert firewall.main(["open", "--target", "vault", "--env", "load"]) == 3

    assert _failure(capsys)["code"] == "rule_not_effective"
    assert azure.count("az keyvault network-rule add") == 5
    assert clock.sleeps == [3.5] * 4
    assert azure.count("az keyvault secret list") == 0


def test_t476_vault_probe_rewrites_a_rule_a_concurrent_write_removed(
    job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    def concurrent_overwrite(list_number: int) -> None:
        # List 1 is open's first read, list 2 the read-back after its write; before the probe's
        # read another job rewrites the list without this address.
        if list_number == 3:
            azure.vault_rules = [f"{OTHER_RUNNER}/32"]

    azure.vault_list_hook = concurrent_overwrite

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0

    assert azure.count("az keyvault network-rule add") == 2
    assert azure.vault_rules == [f"{OTHER_RUNNER}/32", f"{RUNNER}/32"]


def test_t476_vault_probe_treats_forbidden_by_firewall_as_not_yet_and_exits_3_after_300_s(
    job: Path, azure: FakeAzure, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = firewall.CommandResult(
        1, "", "ERROR: (Forbidden) Client address is not authorized\nCode: ForbiddenByFirewall"
    )
    azure.secret_answers = [refusal] * 30

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 3

    failure = _failure(capsys)
    assert failure["code"] == "rule_not_effective"
    assert "ForbiddenByFirewall" in failure["detail"]
    assert azure.count("az keyvault secret list") == 21
    assert clock.elapsed == 300.0
    assert azure.vault_rules == []


def test_t476_vault_probe_succeeds_after_forbidden_by_firewall(
    job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    refusal = firewall.CommandResult(1, "", "Code: ForbiddenByFirewall")
    azure.secret_answers = [refusal, refusal]

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0

    assert clock.sleeps == [15.0, 15.0]
    assert azure.vault_rules == [f"{RUNNER}/32"]


def test_t476_vault_each_window_records_only_its_address_and_opening_time(
    job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    azure.vault_rules = [f"{OTHER_RUNNER}/32"]
    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0
    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 0
    clock.elapsed += 3600.0
    azure.vault_rules.append("192.0.2.44/32")

    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0

    assert _vault_state(job) == {"address": RUNNER, "openedAt": "2026-10-30T13:00:00Z"}
    assert azure.vault_rules == [f"{OTHER_RUNNER}/32", "192.0.2.44/32", f"{RUNNER}/32"]


def _write_vault_state(job: Path, opened_minutes_ago: float) -> None:
    opened_at = START - timedelta(minutes=opened_minutes_ago)
    state = {"address": RUNNER, "openedAt": opened_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    (job / "docchat-firewall-vault.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.mark.parametrize("opened_minutes_ago", [1, 29.9, 30, 600])
@pytest.mark.parametrize("shared_allowlist", [None, "198.51.100.1", ""])
def test_t476_vault_close_removes_only_its_own_address_however_long_its_job_has_run(
    *,
    monkeypatch: pytest.MonkeyPatch,
    job: Path,
    azure: FakeAzure,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
    opened_minutes_ago: float,
    shared_allowlist: str | None,
) -> None:
    # Even an operator's shell that holds the shared allowlist doesn't widen close.
    if shared_allowlist is not None:
        monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", shared_allowlist)
    _write_vault_state(job, opened_minutes_ago)
    azure.vault_rules = ["198.51.100.1/32", f"{OTHER_RUNNER}/32", f"{RUNNER}/32", "192.0.2.44/32"]

    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 0

    assert azure.vault_rules == ["198.51.100.1/32", f"{OTHER_RUNNER}/32", "192.0.2.44/32"]
    removals = [
        call[call.index("--ip-address") + 1]
        for call in azure.calls
        if call[:4] == ["az", "keyvault", "network-rule", "remove"]
    ]
    assert removals == [f"{RUNNER}/32"]
    assert "::warning::" not in capsys.readouterr().out


def test_t476_vault_always_run_close_after_the_window_closed_changes_nothing(
    job: Path, azure: FakeAzure, clock: FakeClock
) -> None:
    azure.vault_rules = [f"{OTHER_RUNNER}/32"]
    assert firewall.main(["open", "--target", "vault", "--env", "dev"]) == 0
    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 0
    clock.elapsed += 7200.0

    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 0

    assert azure.vault_rules == [f"{OTHER_RUNNER}/32"]
    assert azure.count("az keyvault network-rule remove") == 1


def test_t476_vault_close_without_state_changes_nothing(job: Path, azure: FakeAzure) -> None:
    azure.vault_rules = [f"{OTHER_RUNNER}/32"]

    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 0

    assert azure.calls == []


def test_t476_vault_close_exits_4_when_its_address_stays_listed(
    job: Path, azure: FakeAzure, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_vault_state(job, 1)
    azure.vault_rules = [f"{RUNNER}/32"]
    azure.stuck_vault_removals = True

    assert firewall.main(["close", "--target", "vault", "--env", "dev"]) == 4

    failure = _failure(capsys)
    assert failure["code"] == "rule_close_failed"
    assert failure["message"] == (
        "The temporary rule couldn't be removed; a later sweep removes it."
    )
    assert azure.count("az keyvault network-rule remove") == 5


def test_t476_vault_sweep_removes_every_address_outside_the_shared_allowlist(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure
) -> None:
    monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", "198.51.100.1")
    azure.vault_rules = ["198.51.100.1/32", f"{OTHER_RUNNER}/32", f"{RUNNER}/32"]

    assert firewall.main(["sweep", "--target", "vault", "--env", "dev"]) == 0

    assert azure.vault_rules == ["198.51.100.1/32"]


# --- the TDS 8.0 login probe ----------------------------------------------------------------


class FakeStream:
    def __init__(self, answer: bytes) -> None:
        self.answer = bytearray(answer)
        self.sent = bytearray()
        self.closed = False

    def sendall(self, data: bytes, /) -> None:
        self.sent += data

    def recv(self, size: int, /) -> bytes:
        chunk = bytes(self.answer[: min(size, 7)])
        del self.answer[: len(chunk)]
        return chunk

    def close(self) -> None:
        self.closed = True


def _prelogin_answer(*, fedauth_required: bool, nonce: bytes = b"") -> bytes:
    options = [(0x00, bytes(6)), (0x01, b"\x00")]
    if fedauth_required:
        options.append((0x06, b"\x01"))
    if nonce:
        options.append((0x07, nonce))
    start = len(options) * 5 + 1
    header, data = bytearray(), bytearray()
    for token, value in options:
        header += struct.pack(">BHH", token, start + len(data), len(value))
        data += value
    payload: bytes = bytes(header) + b"\xff" + bytes(data)
    framed: bytes = firewall.tds_packets(0x04, payload)
    return framed


def _error(number: int) -> bytes:
    text = "Client is not allowed".encode("utf-16-le")
    body = struct.pack("<iBBH", number, 1, 14, len(text) // 2) + text + b"\x00\x00" + b"\x00" * 4
    return b"\xaa" + struct.pack("<H", len(body)) + body


def _loginack() -> bytes:
    body = b"\x01" + struct.pack(">I", 0x74000004) + b"\x00" + b"\x00" * 4
    return b"\xad" + struct.pack("<H", len(body)) + body


def _routing(host: str, port: int) -> bytes:
    name = host.encode("utf-16-le")
    value = struct.pack("<BHH", 0, port, len(host)) + name
    body = struct.pack("<BH", 20, len(value)) + value + b"\x00\x00"
    return b"\xe3" + struct.pack("<H", len(body)) + body


def _done() -> bytes:
    return b"\xfd" + bytes(12)


def _reply(*tokens: bytes) -> bytes:
    framed: bytes = firewall.tds_packets(0x04, b"".join(tokens))
    return framed


def _sent_login7(stream: FakeStream) -> bytes:
    data = bytes(stream.sent)
    position = 0
    payload = bytearray()
    while position < len(data):
        packet_type, _status, length = struct.unpack_from(">BBH", data, position)
        if packet_type == 0x10:
            payload += data[position + 8 : position + length]
        position += length
    return bytes(payload)


def test_tds_login_returns_the_firewall_error_number(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream(_prelogin_answer(fedauth_required=True) + _reply(_error(40615), _done()))
    monkeypatch.setattr(firewall, "open_tds_stream", lambda host, port: stream)

    assert firewall.tds_login("sql-x.database.windows.net", "docchat", "tok") == 40615
    assert stream.closed


def test_tds_login_sends_the_token_with_fedauth_echo_and_tds_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "eyJ0eXAiOiJKV1Qi." + "a" * 3000
    nonce = bytes(range(32))
    stream = FakeStream(
        _prelogin_answer(fedauth_required=True, nonce=nonce) + _reply(_loginack(), _done())
    )
    monkeypatch.setattr(firewall, "open_tds_stream", lambda host, port: stream)

    assert firewall.tds_login("sql-x.database.windows.net", "docchat", token) is None

    login = _sent_login7(stream)
    length, version = struct.unpack_from("<II", login)
    assert length == len(login)
    assert version == 0x08000000
    assert login[27] & 0x10  # fExtension
    server_at, server_chars = struct.unpack_from("<HH", login, 52)
    assert login[server_at : server_at + 2 * server_chars].decode("utf-16-le") == (
        "sql-x.database.windows.net"
    )
    (feature_at,) = struct.unpack_from("<I", login, server_at + 2 * server_chars)
    assert login[feature_at] == 0x02
    options = login[feature_at + 5]
    assert options == (0x01 << 1) | 1
    (token_length,) = struct.unpack_from("<I", login, feature_at + 6)
    token_start = feature_at + 10
    assert login[token_start : token_start + token_length].decode("utf-16-le") == token
    assert login[token_start + token_length : token_start + token_length + 32] == nonce
    assert login[-1] == 0xFF
    # A long token spans several packets of at most 4,096 bytes.
    assert len(stream.sent) > 4096


def test_tds_login_follows_one_gateway_route(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeStream(
        _prelogin_answer(fedauth_required=False)
        + _reply(_loginack(), _routing("node.eastus2.worker.database.windows.net", 11003))
    )
    node = FakeStream(_prelogin_answer(fedauth_required=False) + _reply(_error(40613), _done()))
    opened: list[tuple[str, int]] = []
    streams = iter([gateway, node])

    def connect(host: str, port: int) -> FakeStream:
        opened.append((host, port))
        return next(streams)

    monkeypatch.setattr(firewall, "open_tds_stream", connect)

    assert firewall.tds_login("sql-x.database.windows.net", "docchat", "tok") == 40613
    assert opened == [
        ("sql-x.database.windows.net", 1433),
        ("node.eastus2.worker.database.windows.net", 11003),
    ]
    # Neither server asked for FEDAUTHREQUIRED, so the echo bit stays clear.
    login = _sent_login7(gateway)
    server_at, server_chars = struct.unpack_from("<HH", login, 52)
    (feature_at,) = struct.unpack_from("<I", login, server_at + 2 * server_chars)
    assert login[feature_at + 5] == 0x01 << 1


def test_tds_login_raises_on_a_truncated_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream(_prelogin_answer(fedauth_required=True)[:-3])
    monkeypatch.setattr(firewall, "open_tds_stream", lambda host, port: stream)

    with pytest.raises(OSError, match="closed the connection"):
        firewall.tds_login("sql-x.database.windows.net", "docchat", "tok")
    assert stream.closed


def test_tds_login_raises_when_neither_accepted_nor_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(_prelogin_answer(fedauth_required=True) + _reply(_done()))
    monkeypatch.setattr(firewall, "open_tds_stream", lambda host, port: stream)

    with pytest.raises(OSError, match="neither accepted nor refused"):
        firewall.tds_login("sql-x.database.windows.net", "docchat", "tok")


# --- registry and vault sweeps: runbook R26 only ---------------------------------------------


@pytest.mark.parametrize("target", ["registry", "vault"])
def test_t476_sweep_removes_nothing_warns_and_exits_0_when_the_shared_allowlist_is_unset(
    job: Path, azure: FakeAzure, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    azure.registry_rules = ["198.51.100.1", OTHER_RUNNER]
    azure.vault_rules = ["198.51.100.1/32", f"{OTHER_RUNNER}/32"]

    assert firewall.main(["sweep", "--target", target, "--env", "dev"]) == 0

    assert azure.registry_rules == ["198.51.100.1", OTHER_RUNNER]
    assert azure.vault_rules == ["198.51.100.1/32", f"{OTHER_RUNNER}/32"]
    # Nothing is even listed, so an Azure failure can't turn the no-op into a failed step.
    assert azure.calls == []
    warning = capsys.readouterr().out
    assert warning.startswith("::warning::DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST is not set")
    assert "Remove a stray runner rule" in warning


def test_t476_vault_sweep_without_the_shared_allowlist_needs_no_vault_uri(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure
) -> None:
    monkeypatch.delenv("DOCCHAT_TEST_VAULT_URI")

    assert firewall.main(["sweep", "--target", "vault", "--env", "dev"]) == 0
    assert azure.calls == []


@pytest.mark.parametrize("target", ["registry", "vault"])
def test_sweep_with_an_empty_shared_allowlist_removes_every_address(
    monkeypatch: pytest.MonkeyPatch, job: Path, azure: FakeAzure, target: str
) -> None:
    # Set but empty is the operator saying the list holds nothing, unlike unset.
    monkeypatch.setenv("DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST", "")
    azure.registry_rules = [OTHER_RUNNER]
    azure.vault_rules = [f"{OTHER_RUNNER}/32"]

    assert firewall.main(["sweep", "--target", target, "--env", "dev"]) == 0

    remaining = azure.registry_rules if target == "registry" else azure.vault_rules
    assert remaining == []
