# ADR 0129: Coding rules as tool gates

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 84-testing.md, 82-security.md
- **Related ADRs:** 0006, 0011, 0013, 0014, 0025, 0026, 0128, 0130, 0132, 0136, 0150

## Context

The code base follows a defined set of coding rules: async end to end, with nothing synchronous on an event loop and clients created once per process; SQL only through SQLAlchemy with bound parameters, never built from strings; typed boundaries, every request, response, queue message, Cosmos document and setting a Pydantic model that rejects unknown fields; test-first work; deep modules with shared logic in `packages/core`, no copy-paste between apps, no speculative abstraction and no dead code; exceptions never swallowed and each failure logged once at the boundary; comments that explain why. A rule that only reviewers enforce erodes with every pull request, so each rule here becomes a tool that fails the build, or a named check; review judges only what a tool can't.

Facts (versions confirmed on PyPI, 2026-10-15): Ruff 0.17.0 provides the flake8-async rules (`ASYNC`), the Bandit rules including `S608` (SQL built from strings) and `TID251` (banned APIs, each with a message naming the replacement); import-linter 2.15 checks import contracts over packages, external packages included when `include_external_packages` is on; deptry 0.25.1 reports unused, missing and transitive-only dependencies of each package; pytest-cov 7.1.0 with coverage 7.16.2 measures line and branch coverage.

## Decision

1. **Ruff 0.17.0** (root `pyproject.toml`): `target-version = "py313"`, `line-length = 100`; `select = ["E","W","F","I","N","UP","B","A","C4","DTZ","T10","T20","ISC","G","LOG","PIE","PT","RET","SIM","TID","PTH","ERA","PL","TRY","BLE","S","ASYNC","RUF","FAST","PERF"]`; `ignore = ["TRY003","TRY400","PLR0913"]` (long exception messages are allowed; `logging.exception` isn't forced, because exception text can carry content and the boundary logs type and code; argument counts are a review matter). `S608` and every `ASYNC` rule are errors with no per-file ignore anywhere. Formatting is `ruff format` with `docstring-code-format = true`; isort's `known-first-party` lists the six first-party packages.
2. **Banned APIs** (`[tool.ruff.lint.flake8-tidy-imports.banned-api]`, each with a message naming the replacement): `requests`, `urllib.request`, `httpx.Client` (use `httpx.AsyncClient`), `httpx2` (ADR 0130), `openai.OpenAI`, `openai.AzureOpenAI`, `openai.AsyncAzureOpenAI` (use `openai.AsyncOpenAI` against `/openai/v1/`), `azure.identity.DefaultAzureCredential`, `azure.identity.ManagedIdentityCredential`, `azure.identity.AzureCliCredential`, `azure.storage.blob.BlobServiceClient`, `azure.storage.queue.QueueClient`, `azure.cosmos.CosmosClient`, `azure.search.documents.SearchClient`, `azure.search.documents.indexes.SearchIndexClient`, `azure.ai.documentintelligence.DocumentIntelligenceClient`, `azure.keyvault.secrets.SecretClient`, `sqlalchemy.text`, `pyodbc`. The per-file ignores are exactly: `"**/tests/**" = ["S101","PLR2004","S105","S106"]`; `TID251` for `packages/core/src/docchat_core/sql/statements.py` and `packages/core/src/docchat_core/sql/engine.py` (pyodbc), `db/migrations/**` (Alembic runs synchronously outside any event loop with `AzureCliCredential`, `sqlalchemy.text` and `pyodbc`), `packages/core/src/docchat_core/clients/openai.py` (`httpx2` timeouts for `AsyncOpenAI`), `tests/model-stub/**`, `scripts/ci/**` and `tests/load/aci/run.py` (standard-library tools that call HTTPS with `urllib.request.urlopen`, always with an explicit `timeout`). In `statements.py` every `text()` construct is a module-level constant with bound parameters only, and CODEOWNERS routes the file to `docchat-security`.
3. **Import contracts** (import-linter 2.15, root `[tool.importlinter]`, `root_packages` = the six first-party packages, `include_external_packages = true`), run by `lint-imports` in the `lint` job, except contract 6: import-linter refuses a forbidden contract on `azure.identity`, a subpackage of an external package, so `scripts/ci/check_outbound_clients.py` checks contract 6 in the same job:
   1. `docchat_core` imports none of `docchat_api`, `docchat_ingestion`, `docchat_ops`, `docchat_testing`, `docchat_model_stub`;
   2. `docchat_api` and `docchat_ingestion` are independent;
   3. no production package (`docchat_core`, `docchat_api`, `docchat_ingestion`) imports `docchat_testing` or `docchat_model_stub`, so no test switch exists in production code;
   4. inside `docchat_core`, `telemetry` and `settings` import no other `docchat_core` subpackage except `errors` and `models`;
   5. `pyodbc` is imported only by `docchat_core.sql.engine`;
   6. `azure.identity.aio` is imported only by `docchat_core.clients`, `docchat_core.sql.engine`, `docchat_ops` and `docchat_testing`;
   7. `httpx2` is imported only by `docchat_core.clients.openai` (ADR 0130).
4. **Dependencies.** deptry 0.25.1 runs per workspace member in the `security` job (`uv run deptry <member path> --json-output build/audit/deptry-<member>.json`); any unused, missing or transitive-only dependency fails the gate unless an expiring waiver of at most 90 days covers it (ADR 0136); no `[tool.deptry]` ignore is configured.
5. **Coverage and layout.** pytest-cov measures line and branch coverage, and CI fails below these totals per package: `docchat_core` 90%, `docchat_api` 85%, `docchat_ingestion` 85%, `docchat_ops` 80%, `docchat_model_stub` 80%; the web client (Vitest, v8, over `src/**` without `src/api/generated/**` and `src/main.tsx`) fails below 85% lines, statements and functions and 80% branches, and below 95% branches in `src/chat/stream/`, `src/chat/render/`, `src/api/http.ts`, `src/platform/traceparent.ts` and `src/bootstrap/`. `scripts/ci/check_test_layout.py` fails CI when a module `src/<pkg>/<path>/<name>.py` (other than `__init__.py`, `__main__.py`, `py.typed`) has no `tests/unit/<path>/test_<name>.py` in the same member, unless root `[tool.docchat.test-layout] exempt` lists it with a reason; the one exemption is `apps/ingestion/function_app.py`, a two-line entry point.
6. **Each coding rule's gate:**

   | Rule | Gate |
   |---|---|
   | Async end to end | Ruff `ASYNC`; the banned synchronous clients; mypy `unused-awaitable` (ADR 0128); the loop guard in every test and the loop-lag metric (ADR 0132); the load test |
   | Bound parameters only | Ruff `S608`; `sqlalchemy.text` and `pyodbc` banned outside the SQL layer; CodeQL |
   | Typed boundaries | a unit test in each app walks the OpenAPI document and the registered queue-message, Cosmos-document and settings models and fails when an input model lacks `extra="forbid"`; mypy strict with the Pydantic plugin |
   | Test-first | the pull request template asks for the failing test first; the layout gate and coverage enforce presence and depth |
   | Deep modules, shared logic in `packages/core`, no copy-paste between apps | import contracts 1 to 7; review |
   | No dead code, no speculative abstraction | Ruff `ERA`; deptry; coverage thresholds |
   | Never swallow an exception; log each failure once | Ruff `BLE001`, `S110`, `S112` and the `TRY` rules; failures logged once at the boundary (ADR 0014) |
   | Comments explain why | the review checklist in `.github/pull_request_template.md` |

7. **Where the gates run.** pre-commit runs `ruff-check --fix`, `ruff-format` and mypy before each commit; `ci.yaml` runs them all as required checks on `main`: `lint` (pre-commit over every file, `lint-imports`, the layout check, the outbound-client check of ADR 0150), `python-tests` (pytest with the coverage thresholds) and `security` (deptry with the other scanners).

## Alternatives considered

- **Review alone for the coding rules.** Rejected: rules that only reviewers enforce erode as the code base grows, and a failing build is cheaper than an incident.
- **Ruff's default rule set.** Rejected: it lacks the `ASYNC`, `S` and `TID` families that carry the async and injection rules.
- **Per-file ignores for `S608` or `ASYNC` where convenient.** Rejected: a single exception reopens the class of bug the rule exists for; SQL text lives only in `statements.py`, as module-level constants with bound parameters.
- **A coverage target alone, with no layout gate.** Rejected: a high total can hide a module with no tests at all; the layout gate proves every module has its test file, coverage proves depth.
- **One repository-wide coverage threshold.** Rejected: the shared package carries the most reused logic and gets the highest bar (90%); thin entry points get 80%.
- **pylint or flake8 beside Ruff.** Rejected: Ruff implements the rule families used here in one fast tool, and two linters would disagree.

## Consequences

- A blocking client, a coroutine left unawaited, string-built SQL, an Azure client built outside `docchat_core.clients`, a cross-app import, an unused dependency or an untested module each fails the pull request before review.
- The banned-API list and the per-file ignores are part of the security surface: CODEOWNERS routes `pyproject.toml` changes to the maintainers and the SQL layer and client modules to `docchat-security`.
- A new external client needs an entry in the banned list (its synchronous form), its construction in `docchat_core.clients` and, if it is an Azure SDK client, its explicit timeouts (ADR 0150).
- Tests (84-testing.md): CI runs every gate on each pull request; the layout check, the outbound-client check and the `extra="forbid"` walker each have unit tests that prove they fail on a planted violation and pass on the clean tree.

## References

- Ruff on PyPI (0.17.0): https://pypi.org/project/ruff/ (checked 2026-10-15)
- import-linter on PyPI (2.15): https://pypi.org/project/import-linter/ (checked 2026-10-15)
- deptry on PyPI (0.25.1): https://pypi.org/project/deptry/ (checked 2026-10-15)
- pytest-cov on PyPI (7.1.0): https://pypi.org/project/pytest-cov/ (checked 2026-10-15)
