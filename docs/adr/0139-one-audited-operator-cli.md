# ADR 0139: One audited operator CLI

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 86-operations.md, 01-repository.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0008, 0024, 0026, 0028, 0029, 0033, 0054, 0137, 0142, 0147, 0149

## Context

Operators change ops-config values, point one user at another index for live testing, re-index, repair ingestion runs, export and delete a user's data, read a conversation under just-in-time access, rotate test-user passwords and look up the trace behind a user's reference ID. Runtime settings change only through an audited script, never through the admin UI. Each of these procedures reads or writes application data outside the apps' own request paths, so each needs the same guarantees: the operator's own identity, the right environment, a ticket and a reason, a confirmation in production, an audit row written with the change, and a value the apps will accept. Settings are typed data, never code: nothing an operator supplies is evaluated.

A collection of independent scripts would repeat sign-in, endpoint discovery, audit and confirmation logic, and each copy could get one of them wrong.

Vendor facts: `azure.identity.aio.AzureCliCredential` uses the identity of the current `az login`, a person or a pipeline identity after `azure/login`; SQL accepts an Entra access token per connection, and a contained user's `oid` identifies the caller; `argparse` exits with status 2 on a usage error.

## Decision

1. **One CLI.** Every operator procedure that reads or writes application data runs as `uv run docchat-ops --env <dev|prod|load|local> <group> <command> …` from the workspace member `scripts/ops` (package `docchat_ops`), built with `argparse`, async inside (`asyncio.run`), reusing the `docchat_core` repositories, models and validation, so an operator can never write a value the apps would reject. There is no other route: the admin UI and the API expose none of these procedures.
2. **Identity.** `AzureCliCredential`: the operator's own `az login`, or a pipeline identity after `azure/login`. Service-principal callers are refused with exit 3 for data-changing commands outside `--env load`, so `config set` in `dev` and `prod` is always a person; the read-only `chat capacity-check`, `ingestion status` and `testusers check` accept a service principal (the deploy identity runs the first after migrations; the CI test identity runs the last nightly). In `load` the `load` deploy identity applies the load-test ops-config values from the load workflow's jobs, writing audit rows with its own object ID.
3. **Endpoints.** Read from the azd environment file `.azure/<env>/.env` (exit 2 with "run azd env refresh -e <env>" when absent); `--env local` reads `.env` for the compose services; `testusers check` reads only the shared test vault (`--vault-uri`, default `DOCCHAT_TEST_VAULT_URI`). No command accepts a credential as an argument.
4. **Arguments every command honours.** `--ticket <id>` (1 to 64 characters of letters, digits, `-`, `_`, `#`) and `--reason <text>` (10 to 500 characters), both required for every command that reads personal data or changes anything, and refused by `chat capacity-check`, which does neither; `--yes` skips the confirmation prompt except in `prod`, where the operator always types the environment name; `--json` prints the report as JSON.
5. **Correlation and audit.** Each run creates one random W3C trace ID, prints it first and uses it as `audit_events.correlation_id` and as the trace ID of its spans, which aren't exported. Every change and every read of another person's data writes one `app.audit_events` row with `actor_type` `operator` (actor = the `oid` of the caller's SQL access token) in the same SQL transaction as the change.
6. **A mutating command, step by step.** (1) Refuse a missing or invalid `--ticket` or `--reason` (exit 2). (2) Print the correlation ID, load the environment file, build the credential, refuse a service principal where rule 2 says so (exit 3), acquire a token for each target store (exit 3, "sign in with az login", on failure). (3) Read the current state (for `config set`, the row and its `row_version`) and validate the new value with the owning component's model (exit 2, quoting the allowed range). (4) Print the change (old → new, target IDs, no content values) and ask for confirmation. (5) In one SQL transaction through the data-access layer with `retry="none"`, apply the change conditioned on the state read in step 3 and insert the audit row; zero rows affected rolls back and exits 6, "changed by someone else; run it again". (6) Print the result and the audit event ID and exit 0.
7. **Output.** Files with mode 0600 in directories with mode 0700; no content values on standard output except the command's own report.
8. **Exit codes, for every group:** 0 success; 1 unexpected error; 2 usage error, validation failure or refused precondition (nothing written; argparse's own exit 2 is kept); 3 access denied (no sign-in, missing just-in-time membership, `401` or `403` from a store, SQL login refused); 4 not found; 5 incomplete, run again (a multi-store operation stopped part-way and is idempotent); 6 conflict (a concurrent change was detected and the conflicting write wasn't made); 7 store unreachable (network path, firewall or timeout).
9. **Command groups and their owners** (each group's semantics belong to the component that owns the data): `local` (quality and operations: `init`, which writes the gitignored `.env` with a random 24-character SQL `sa` password); `config` (identity and access: `show`, `set`, `reset`); `override` (identity and access: `list`, `set`, `clear`); `user` (conversations and memory: `read-conversation`, `export`, `delete`, `verify`, `redact-document --document-id <uuid>`); `jit` (conversations and memory: `grant`, `revoke`, `list`, each with `--level read|user_data|config`); `reindex` (ingestion: `start`) and `ingestion` (ingestion: `status`, read-only with no audit row; `requeue-item`, `fail-item`, `cancel-run`, `abandon-rebuild`, `set-deployments`, `drain-poison`, all under `jit --level config`); `chat` (chat pipeline: `capacity-check`, read-only, with `--set <key>=<json>` to evaluate a proposed value without writing it; exit 2 when a share exceeds 25% in `prod`, 4 when a capacity is absent in `prod`, 7 when the store is unreachable); `testusers` (quality and operations: `rotate`, `check`); `trace` (quality and operations: `show --id <reference ID>`, through the Logs query API).

## Alternatives considered

- **One standalone script per procedure.** Rejected: each would repeat sign-in, endpoint discovery, confirmation and auditing, and the copies would drift; one framework puts every guarantee in one tested place.
- **Ops-config and override screens in the admin UI, or operator endpoints on the API.** Rejected: runtime settings stay out of the admin UI, and operator endpoints would put privileged operations on the one public entry point; operators act under just-in-time group membership, separate from app roles.
- **Raw SQL in runbooks.** Rejected: a hand-typed statement skips the owning model's validation, can't write its audit row in the same transaction, and is one typo from an injection.
- **Click or Typer.** Rejected: a third-party dependency for what `argparse` in the standard library does.
- **Allowing service principals to change data everywhere.** Rejected: an operator change in `dev` or `prod` must name a person in the audit row; only `load`, which lives at most 24 hours and runs unattended, takes changes from its deploy identity.

## Consequences

- Every operator change carries a ticket, a reason, the operator's object ID and a correlation ID in one audit row written with the change, so a change without its audit row can't exist.
- A value the apps would refuse can't be written, because the CLI validates with the same models the apps load.
- Runbooks quote exact `docchat-ops` commands and rely on the shared exit codes; a new procedure becomes a new command in its owner's group, with no new framework code.
- Operators need a machine on the operator allowlist, an `az login` and, for most groups, a just-in-time membership; a missing membership exits 3 before anything is read.
- Tests (84-testing.md): a missing `--ticket` or `--reason` exits 2, a service principal running `config set` in `prod` exits 3, and `prod` requires the typed environment name even with `--yes`; a stale `row_version` writes nothing and exits 6, and a forced audit failure rolls the change back; the printed correlation ID equals the audit rows' `correlation_id`; the exit-code table covers 0 to 7 and every message includes the correlation ID; the registered `chat capacity-check`, `ingestion status`, `user redact-document` and `jit grant` behaviours; each group's semantics are tested by its owner.

## References

- `AzureCliCredential` (async): https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.aio.azureclicredential?view=azure-python (checked 2026-10-21)
- Python `argparse`: https://docs.python.org/3.13/library/argparse.html (checked 2026-10-21)
