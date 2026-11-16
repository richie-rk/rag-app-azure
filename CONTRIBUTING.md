# Contributing to DocChat

Thank you for contributing. This file covers setting up a machine, running the tests, the pull request rules and when a change needs an architecture decision record. Everyone who takes part follows the [code of conduct](CODE_OF_CONDUCT.md); report a vulnerability as [`SECURITY.md`](SECURITY.md) describes, never in an issue.

## Set up a machine

You need an x86-64 machine, because the local SQL Server container image is published for `linux/amd64` only, with Docker and Compose, uv 0.13.1 and Node.js 24.21.0. From a fresh clone:

1. Install the Python workspace from the lockfile: `uv sync --locked --all-packages --all-groups`
2. Install the git hooks: `uv run --frozen pre-commit install`
3. Install the web packages: `npm ci --prefix apps/web` and `npm ci --prefix tests/e2e`
4. Write the git-ignored `.env` with a generated local SQL Server password: `uv run docchat-ops --env local local init`
5. Start SQL Server, Azurite, the Cosmos DB emulator and the model stub: `docker compose up -d --wait`

Never commit `.env`, anything under `.azure/`, `apps/web/config.local.json`, an archive or build output: CI fails a pull request that tracks one, even when it was forced past `.gitignore`.

## Run the tests

- `uv run pytest` runs the Python unit and integration suites against the compose services; an integration test fails, never skips, when a service is down.
- `npm --prefix apps/web test` runs the web unit and component suites.
- `uv run --frozen pre-commit run --all-files` runs every hook CI runs: Ruff, mypy, the web format, lint and type checks, the lockfile check and gitleaks.

The test levels, their markers and the suites that need Azure are in [`docs/spec/84-testing.md`](docs/spec/84-testing.md).

## Pull requests

1. **Write the failing test first.** Every change starts with a test that fails before the change and passes after it; every module under a member's `src/` has its mirrored unit test (`tests/unit/<path>/test_<name>.py`), and each package stays at or above its coverage threshold.
2. **Fill in the template.** [`.github/pull_request_template.md`](.github/pull_request_template.md) asks for what changed and why (the ADR or design section it implements), the test that failed first and the levels that ran, and the review checklist: comments explain why, no copied logic, no unused code, dependency or configuration, new settings and ops-config keys documented, migrations that follow expand and contract, and no content value, token or personal data in a log or span.
3. **Every required check is green.** The main-branch ruleset requires every `ci.yaml` job and the CodeQL analysis, on a branch that is up to date with `main`; no test is skipped or re-run into a pass to merge.
4. **One code-owner approval.** [`.github/CODEOWNERS`](.github/CODEOWNERS) names the team that owns each path, and the ruleset requires one approval from it; changes to the security paths (`/.github/workflows/`, `/.github/security/`, the CI checks, the SQL statements module and the identity templates among them) need a `docchat-security` approval. A push dismisses earlier approvals, every review conversation must be resolved, and `main` keeps a linear history.

## When a change needs an ADR

Architecture decision records live in [`docs/adr/`](docs/adr/) ([ADR 0001](docs/adr/0001-record-architecture-decisions.md)). Write one, in the same pull request, for:

- a significant choice: one that shapes the structure, a security control or a quality attribute, or that is costly to reverse;
- a dependency on a preview feature, with its GA alternative;
- any change to the repository layout of [`docs/spec/01-repository.md` section 1](docs/spec/01-repository.md#1-repository-tree), including a new top-level subpackage of `docchat_core`, `docchat_api` or `docchat_ingestion` or a new workspace member;
- a test outcome that needs a decision an existing ADR doesn't make.

How to write one:

1. Take the next free number, from 0153 on; numbers are never reused. Name the file `docs/adr/NNNN-<slug>.md`, the slug the title in lower-case kebab case, and start it with `# ADR NNNN: <title in sentence case>`.
2. Give the header fields Status, Date, Decided by (`Product owner`, `Architect` or `Product owner and architect`), Specified in (the design documents that implement it) and Related ADRs, then the sections Context, Decision, Alternatives considered, Consequences and References. The Decision carries every name, value and unit a builder needs; references are official vendor documentation, each with the date it was checked.
3. Add the record to the index in `docs/adr/README.md`, with its title, status, decider and the files that specify it.
4. Merging the pull request accepts the record. An accepted record is never edited, except that a later record superseding it changes its Status to `Superseded by NNNN`, and a test the record names appends one `Outcome (YYYY-MM-DD): …` line to its Consequences.
