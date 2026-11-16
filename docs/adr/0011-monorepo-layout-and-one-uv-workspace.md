# ADR 0011: Monorepo layout and one uv workspace

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 84-testing.md, 86-operations.md, 90-build-order.md
- **Related ADRs:** 0001, 0002, 0003, 0010, 0013, 0026, 0027, 0096, 0097, 0124, 0128, 0129, 0131, 0136, 0139, 0141, 0142, 0144

## Context

DocChat is one repository with three deployable apps (`apps/web`, `apps/api`, `apps/ingestion`) and one shared Python package (`packages/core`), beside `db/migrations`, `infra/`, `scripts/`, `tests/`, `docs/` and `.github/`. Four more bodies of Python sit next to the apps: the Alembic migrations, the audited operator CLI through which operators change ops config, per-user index overrides, re-index runs and user data, the shared test support code, and the stubbed OpenAI-compatible model service that load and deployed tests select through the endpoint setting, never through a switch in production code. Each of them must lint, type-check and resolve against the library versions the apps ship, because a test that passes against a library version no image contains proves nothing, and an operator command must validate input with the same models the apps use.

The product needs every dependency pinned with hashes and checked in CI, an unused or undeclared dependency to fail the build, no dormant code or unbuilt-feature stub in any component, and a unit test for every module that a tool can check. Seven areas (identity and access, ingestion, chat, conversations and memory, web client, infrastructure, quality and operations) are built in parallel, so the skeleton below each app is settled before any of them starts.

Vendor facts that bound the choice (uv documentation, checked 2026-10-18): "the workspace shares a single lockfile, ensuring that the workspace operates with a consistent set of dependencies"; "`uv lock` operates on the entire workspace at once"; members are declared under `[tool.uv.workspace]` (`members` required, `exclude` optional); a workspace enforces "a single `requires-python` for the entire workspace"; `uv run` and `uv sync` take `--package` to act on one member; workspaces aren't "suited for cases in which members have conflicting requirements". `package = false` makes a "non-package ("virtual") project", whose dependencies alone are installed; with `required-version`, "if the version of uv does not meet the requirement at runtime, uv will exit with an error". `uv sync --locked` "requires that the lockfile is up-to-date". For `uv lock`, the documented check is `--check` ("Equivalent to `--locked`"); in the uv 0.13.1 source `LockArgs.locked` still exists with `hide = true`, so `uv lock --locked` works but is undocumented.

## Decision

1. **Top level.**
   - **uv workspace members, exactly seven:**

     | Path | Distribution | Import package | Notes |
     |---|---|---|---|
     | `packages/core` | `docchat-core` | `docchat_core` | shared library |
     | `apps/api` | `docchat-api` | `docchat_api` | API app |
     | `apps/ingestion` | `docchat-ingestion` | `docchat_ingestion` | ingestion app |
     | `db` | `docchat-db` | none | virtual member (`[tool.uv] package = false`) holding Alembic; depends on `docchat-core` |
     | `scripts/ops` | `docchat-ops` | `docchat_ops` | operator CLI, console script `docchat-ops` (ADR 0139) |
     | `tests/support` | `docchat-testing` | `docchat_testing` | test support, console script `docchat-load` |
     | `tests/model-stub` | `docchat-model-stub` | `docchat_model_stub` | stubbed model and Prompt Shields service |

     `apps/web` (`@docchat/web`) and `tests/e2e` (`@docchat/e2e`, Playwright) are npm packages outside the uv workspace, each with a committed `package-lock.json`.
   - **`scripts/`:** `ops/`; `ci/` (CI checks, Python standard library only); `bootstrap/`, `preflight/` and `deploy/` (infrastructure; database grants run as `scripts/deploy/sql_principals.py`); `identity_post_provision.py`. Every other operator procedure that reads or writes application data (ops-config changes, per-user index overrides, re-index, user data export and deletion) is a `docchat-ops` command group (`config`, `override`, `reindex`, `user`), never a loose script.
   - **`.github/`:** `workflows/` (`ci.yaml`, `codeql.yaml`, `deployed-tests.yaml`, `nightly.yaml`, `load-test.yaml`, `deploy.yaml`, `deploy-environment.yaml`), `dependabot.yml`, `CODEOWNERS`, `pull_request_template.md`, `security/waivers.yaml` (findings below high only, ADR 0136), `security/approvers.yaml`, `security/exceptions.yaml` (the one exception at high, ADR 0153), `rulesets/main.json`.
   - **`infra/`:** the infrastructure tree of ADRs 0096 and 0097 (`main.bicep`, `main.bicepparam`, `env/<env>.json` and the modules), with the monitoring modules `infra/modules/monitoring/alerts.bicep`, `workbooks.bicep` and `workbooks/<key>.workbook.json`; `ps-rule.yaml` and `.ps-rule/` at the root.
   - **`tests/`:** `support/`, `model-stub/`, `integration/` (`isolation/`, `contracts/`, `chat/`, `ingestion/`, `ops/`, `data_access/`), `deployed/`, `infra/` (the compiled-template tests), `e2e/` (`specs/web/`, `specs/web-deployed/`, `fixtures/web/`), `load/` (`k6/`, `k6.lock.json`, `aci/loadgen.bicep`, `aci/run.py`), `fixtures/` (`documents/`, `questions/`, `streams/`, `http/`). The ingestion chunker fixtures live in `tests/fixtures/documents/`.
   - **`docs/`:** `adr/` (these records), `spec/` (the design documents `00-overview.md` to `90-build-order.md`) and `runbooks/`, one file per runbook, `docs/runbooks/RNN-<slug>.md`, plus `docs/runbooks/rehearsals.md` (ADR 0142).
   - **Root files:** `azure.yaml`, `pyproject.toml`, `uv.lock`, `compose.yaml`, `CONTEXT.md`, `README.md`, `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `.editorconfig`, `.pre-commit-config.yaml`, `.python-version` (`3.13`), `.node-version` (`24.21.0`), `.gitattributes` (`* text=auto eol=lf`; `*.pdf *.docx *.pptx *.png binary`), `.gitleaks.toml`, `.env.example` (the two local compose variable names, no values) and `.dockerignore` (excludes `.git`, `**/tests`, `**/node_modules`, `.azure`, `.env*`, `reports`, `build`, `dist`, `load-results`).
   - **`.gitignore`, exactly:** `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `coverage.xml`, `reports/`, `build/`, `dist/`, `node_modules/`, `apps/web/dist/`, `apps/web/config.local.json`, `test-results/`, `playwright-report/`, `load-results/`, `*.zip`, `*.tar`, `*.tgz`, `.env`, `.azure/`. `scripts/ci/check_forbidden_files.py` runs `git ls-files -z` and fails CI, naming each path, when a tracked path matches `*.zip`, `*.tar`, `*.tgz`, `*.gz`, `*.7z`, `*.rar`, `*.jar`, `*.whl`, `*.map`, `*.pfx`, `*.p12`, `**/dist/**`, `**/build/**`, `**/node_modules/**`, a final component exactly `.env`, `.azure/**` or `apps/web/config.local.json`, with no allowlist; so no tracked directory carries one of those three directory names.
2. **Two levels below each app and package, exactly.** The owning area names the modules inside its subpackages and may add modules inside a listed directory; a new top-level subpackage of `docchat_core`, `docchat_api` or `docchat_ingestion`, or a new workspace member, needs an ADR. `apps/web` follows the web client's own tree, with unit tests beside each module as `*.test.ts` or `*.test.tsx`.

   ```text
   apps/api/               pyproject.toml, Dockerfile, gunicorn.conf.py, openapi.json (generated, committed; ADR 0013)
     src/docchat_api/      app.py (create_app), routes/, http/, chat/ (the stream writer is chat/stream.py), usage/, conversations/, openapi.py
     tests/unit/           mirrors src/docchat_api module for module;   tests/integration/ (in-process API on compose services)
   apps/ingestion/         pyproject.toml, Dockerfile, host.json, models.lock.json, image/, function_app.py
                           (two-line shim: from docchat_ingestion.function_app import app)
     src/docchat_ingestion/ function_app.py, starters.py, boundary.py, orchestrators/, activities/, methods/, isolation/, selfcheck.py
     tests/unit/  tests/integration/
   packages/core/          pyproject.toml;  src/docchat_core/ (subpackages below);  tests/unit/  tests/integration/
   db/                     pyproject.toml, alembic.ini;  migrations/ (env.py, script.py.mako);  migrations/versions/ (NNNN_<snake_slug>.py, linear)
   scripts/ops/            pyproject.toml;  src/docchat_ops/ (cli.py; framework/ with auth.py, endpoints.py, audit.py, confirm.py,
                           exitcodes.py; commands/);  tests/
   tests/support/          pyproject.toml;  src/docchat_testing/ (plugin.py, fixtures/, tokens.py, apiclient.py, logquery.py, builders/,
                           fakes/, clock.py, durable.py, loopguard.py, telemetry_capture.py, authz_matrix.py, isolation.py,
                           data_audit.py, streaming.py, resume_check.py, load/)
   tests/model-stub/       pyproject.toml, Dockerfile;  src/docchat_model_stub/ (app.py, chat.py, embeddings.py, shields.py,
                           directives.py, scripts.py, recorder.py, throttle.py, schema_fill.py);  tests/
   ```

   `scripts/ci/` holds `security_gate.py`, `check_test_layout.py`, `check_migrations.py`, `whatif_summary.py`, `check_k6_lock.py`, `check_contract_enums.py`, `check_forbidden_files.py`, `check_outbound_clients.py`, `check_codeql_dismissals.py`, `check_deploy_run.py` and `k6_selftest_server.py`. Build-time scripts of the ingestion image live in `apps/ingestion/image/` (`fetch_models.py`; self-check fixtures in `image/selfcheck/`), never under a `build/` directory. `boundary.py` is the ingestion failure boundary (ADR 0144). `scripts/ops/src/docchat_ops/commands/` holds `local.py`, `config.py`, `override.py`, `users.py`, `jit.py`, `reindex.py`, `ingestion.py`, `chat.py`, `testusers.py` and `trace.py`; each command group's behaviour belongs to the area that owns its data.

   | `docchat_core` subpackage | Holds | Owner |
   |---|---|---|
   | `settings/` | the `DocChatSettings` base (prefix `DOCCHAT_`, `extra="forbid"`, frozen), start-up validation, the local-emulator guard | quality framework; each area its settings classes |
   | `clients/` | `ClientRegistry`: every async Azure client created once per process with one shared `azure.identity.aio.ManagedIdentityCredential(client_id=DOCCHAT_CORE_IDENTITY_CLIENT_ID)`, closed at shutdown | quality framework; client options from the calling area |
   | `sql/` | `engine.py` (ADR 0026), `tables/<area>.py`, `repositories/`, `statements.py` (the only module that may call `sqlalchemy.text`) | quality; each table's owner for its metadata and repository |
   | `opsconfig/` | ops-config area models and `OpsConfigStore` | identity and access; each area's keys by their owner |
   | `auth/`, `errors/`, `models/` | token validation; problem types and the code and reason catalogues (ADR 0012); shared identifiers (ADR 0015) | identity and access |
   | `access/`, `users/`, `audit/` | authorisation functions and the index-record cache; first-sign-in provisioning and user search; audit catalogues and writers | identity and access |
   | `telemetry/` | `configure_telemetry`, scrubbing processors, attribute catalogue, instruments, event and log helpers, loop-lag monitor (ADR 0014) | quality |
   | `memory/` | memory-fact validators and the suggested-question test | conversations |
   | `chat/` | citation-token helpers (`chat.citations.strip_citation_tokens`) | chat |
   | `cosmos/` | Cosmos DB document models and repositories | conversations, chat |
   | `search/` | versioned index schemas and the filter builder | ingestion, chat |
   | `ingestion/` | method interface, method catalogue, queue message model, run helpers, name and path builders | ingestion |

3. **Lock policy.** The root `pyproject.toml` is the non-package project `docchat-workspace` (`[tool.uv] package = false`, `required-version = "==0.13.1"`) holding `[tool.uv.workspace]` and the `dev` dependency group, whose entries are exact pins: `ruff==0.17.0`, `mypy==2.4.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `pytest-timeout==2.4.0`, `pytest-cov==7.1.0`, `coverage==7.16.2`, `respx==0.23.1`, `pre-commit==4.7.0`, `pip-audit==2.10.1`, `deptry==0.25.1`, `import-linter==2.15`, `zizmor==1.30.1` and the member `docchat-testing`. Every member declares `requires-python = ">=3.13,<3.14"` (ADR 0010). Runtime dependencies of members declare a lower bound at the confirmed version and an upper bound below the next major (`fastapi>=0.143.1,<1`, `azure-cosmos>=4.17.1,<5`). `uv.lock` is committed and pins every exact version with hashes. Developers and CI install with `uv sync --locked --all-packages --all-groups`; the lock check is `uv lock --locked`; nothing in CI runs `uv lock` without `--locked`, and nothing exports with `--frozen`. Images install only from `uv export --locked`, one member at a time with hashes (ADRs 0010 and 0124).
4. **Test discovery.** Root `testpaths` lists `packages/core/tests`, `apps/api/tests`, `apps/ingestion/tests`, `scripts/ops/tests`, `tests/support/tests`, `tests/model-stub/tests` and `tests/integration`; `tests/deployed` runs only as `uv run pytest tests/deployed --deployed -m <suite>`, and `tests/infra` only in the `ci.yaml` job `infra-tests` as `uv run pytest tests/infra --compiled build/infra`. `scripts/ci/check_test_layout.py` fails CI when a module `src/<pkg>/<path>/<name>.py` (other than `__init__.py`, `__main__.py` and `py.typed`) has no `tests/unit/<path>/test_<name>.py` in the same member, unless root `[tool.docchat.test-layout] exempt` lists it with a reason; the initial list is `apps/ingestion/function_app.py`.

## Alternatives considered

- **Only the three Python deployables as workspace members.** The CLI, the test support and the stub would be locked separately or not at all, so a test could pass against a library version no image ships. Rejected.
- **One project and lockfile per app, with `packages/core` as a path dependency** (uv's suggestion when members conflict). Each app would resolve `docchat-core`'s dependencies on its own, so the API and the ingestion app could run different versions of the same Azure SDK, with three Dependabot streams to merge. Rejected.
- **Operator procedures as separate scripts in `scripts/`.** Each script would repeat sign-in, endpoint discovery, auditing and exit codes, and none could reuse the apps' validation, so an operator could write a value the apps reject. Rejected for one CLI with command groups (ADR 0139).
- **Test helpers inside `docchat_core`.** Fakes and fixtures would ship in every production image. Rejected: `docchat_testing` is its own member, and an import-linter contract proves that no production package imports it or the stub (ADR 0129).
- **A free layout below each app.** Areas building in parallel would collide on module names, and the per-module test rule couldn't be checked by a tool. Rejected.

## Consequences

- Every Python process runs one version of each library; one Dependabot `uv` entry at `/` (groups `azure-sdk`, `opentelemetry`, `dev-tools`) updates them together; `uv sync --locked` reproduces CI on a developer machine; hash-checked installs stop dependency confusion. deptry runs per member in CI, so an unused or undeclared dependency fails the build (ADR 0136).
- The mirrored test paths make "a unit test for every module" checkable, coverage gates measure depth, and import-linter contracts guard the package boundaries (ADR 0129). The tree has no place for a stub or a flag of an unbuilt feature (ADR 0002). Integration tests run against the compose emulators (ADR 0131).
- `.dockerignore` keeps every `tests` directory out of every image build context. CODEOWNERS routes paths to the owning team. Build stage 0 scaffolds exactly this tree (ADR 0141).
- Obligations: a pull request that adds a top-level subpackage or a workspace member adds an ADR; a new module ships with its mirrored test file or an exemption with a reason.
- Risk: one lock lets one member's constraint block another member's upgrade (the ingestion parsing stack is the likeliest). Each image still installs only its own member's dependencies through `uv export --package`, so a heavy dependency of one app never enters another app's image.
- Risk: `uv lock --locked` is a hidden flag in uv 0.13.1, equivalent to the documented `uv lock --check`. `required-version` pins uv exactly, so the behaviour can't change unnoticed; the pull request that raises `required-version` switches the lock check to `uv lock --check` if the new version drops the hidden flag.

## References

- uv, "Using workspaces": https://docs.astral.sh/uv/concepts/projects/workspaces/ (checked 2026-10-18)
- uv CLI reference (`uv lock --check`, `uv sync --locked`, `uv export`): https://docs.astral.sh/uv/reference/cli/ (checked 2026-10-18)
- uv settings reference (`package`, `required-version`, `workspace`): https://docs.astral.sh/uv/reference/settings/ (checked 2026-10-18)
- uv 0.13.1 source, `LockArgs`: https://raw.githubusercontent.com/astral-sh/uv/0.13.1/crates/uv-cli/src/lib.rs (checked 2026-10-18)
- uv 0.13.1 on PyPI: https://pypi.org/project/uv/ (checked 2026-10-14)
