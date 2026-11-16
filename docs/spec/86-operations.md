# 86 — Operations

This file specifies how people operate DocChat: the operator roles, the groups they belong to and the just-in-time access that is the only path from a person to application data (section 1); the operator command-line tool `docchat-ops`, with every command group and command, its arguments, ticket and confirmation rules, exit codes and audit row (section 2); every runbook, `R01` to `R37`, as numbered steps with exact commands and expected results (section 3); and how runbooks are rehearsed and their records kept (section 4). The mechanisms the runbooks drive are specified in the files each step names; this file owns the procedures, the CLI framework and the commands `testusers` and `trace`. Versions are those confirmed on 2026-10-14 in the cited files, plus the GitHub CLI version of section 1.5.

## 1. Operator roles, groups and just-in-time access

### 1.1 Roles

| Role | Who | Standing rights | Acts through | Runbooks |
|---|---|---|---|---|
| Operator | a person listed in `operators` of `scripts/bootstrap/bootstrap.config.json`, member of `sg-docchat-operators` | Reader on each environment group and on `rg-docchat-shared`; Log Analytics Reader on each environment workspace and on `log-docchat-security`; DocChat Test Secret Writer on the test-credential vault; no data-plane right on SQL, Cosmos DB, storage or search (`83-infrastructure.md` section 6.13) | `docchat-ops` under just-in-time membership; KQL through the Logs query API; Azure reads | most |
| On-call operator | the operator named on the organisation's rota for the current cover hours | as an operator | as an operator | R07, R20 |
| Approver | at least 2 named people per environment, listed in `approverObjectIds` of the bootstrap configuration and never in `operators`; owners of that environment's three just-in-time groups; members of `sg-docchat-operators`, which the bootstrap `groups` step adds them to | group ownership; the Azure reads of `sg-docchat-operators` (Reader, Log Analytics Reader), which give no data-plane right; DocChat Cosmos Data Access Granter at the environment's Cosmos DB account only while `cosmosGroupDataAccess` is `false` | PIM approval, or `docchat-ops jit` | R03 to R06, R08, R12, R18, R19, R32, R33 |
| Release approver | member of the GitHub team `docchat-release-approvers`; for the Azure reads of their runbook steps, listed in `operators` and so a member of `sg-docchat-operators` | required reviewer of the GitHub environment `prod` (`prevent_self_review: true`, `can_admins_bypass: false`); the reads of `sg-docchat-operators` | approval of a waiting `prod` deployment; Azure reads | R01, R02, R14, R18, R33 |
| Maintainer | member of the GitHub team `docchat-maintainers`; for the Azure reads of their runbook steps, listed in `operators` and so a member of `sg-docchat-operators` | repository administration; the reads of `sg-docchat-operators` | pull requests, merges, `gh workflow run`; Azure reads | R01, R02, R05, R09 to R11, R14, R17, R27, R33, R35, R36 |
| Tenant administrator | a person holding the Entra role Cloud Application Administrator (template ID `158c047a-c907-4556-b7ef-446551a6b5f7`) or Application Administrator (`9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3`); member of the GitHub team `docchat-tenant-admins` | that role over app registrations, service principals and their app-role assignments; required reviewer of the GitHub environment `load-access` | `scripts/identity_post_provision.py`, Microsoft Graph | R06, R11, R13, R22, R30 |
| User Administrator | a person holding the Entra role User Administrator (`fe930be7-5e62-47db-91af-98c3a49a38b1`); for an account that holds a directory role, Privileged Authentication Administrator (`7be44c8a-adaf-4e2a-84d6-ab2649e08a13`) | creating, disabling, re-enabling and deleting users, revoking their sign-in sessions; creating groups and changing their members | Azure CLI (`az ad`), Microsoft Graph | R06, R07, R13 |
| Conditional Access Administrator | a person holding that Entra role (`b1be1c3e-b65d-4f19-8427-f6fa0d97feb9`) and, for the reads of their steps, a member of `sg-docchat-operators` | Conditional Access policies | Microsoft Graph (`az rest`) | R11, R13 |
| Privileged Role Administrator | a person holding that Entra role (`e8611ab8-c189-46e8-94e1-60213ab1f814`) | administrative units and their members; directory role assignments; PIM for Groups | Microsoft Graph (`az rest`) | R13, R22 |
| Group owner | an owner of `sg-docchat-<env>-users` and `sg-docchat-<env>-admins`, listed in `groupOwners` of the bootstrap configuration (at least 2 people) | adding and removing those groups' members | Azure CLI (`az ad group member`) | R06, R07, R13 |
| Bootstrap operator | a person holding Owner on every subscription in use and the Entra roles Privileged Role Administrator, Groups Administrator and Security Administrator, signed in to `gh` as an owner of the GitHub organization (`83-infrastructure.md` section 11.1) | Owner | `scripts/bootstrap/`, Azure CLI | R06, R11, R14, R21 to R31, R34 |
| Security Administrator | a person holding that Entra role (`194ae4cb-b126-40b2-bd5b-6091b380977d`) | Entra diagnostic settings | Azure CLI | R21, R22, R30 |
| Rotating operator | an operator who also holds Password Administrator (`966707d0-3269-4727-9be2-8c3a10f19b9d`) scoped to the administrative unit `DocChat test users` | as an operator, plus that scoped role | `docchat-ops testusers` | R08, R13 |
| Security responder | member of the GitHub team `docchat-security` | approver of `.github/security/` changes | incident work beside the bootstrap operator | R07, R31 |
| Admin | a user holding the API's `Admin` app role (`11-api.md`) | the admin screens | the web client | R05, R12, R32 |

The Entra role template IDs above were confirmed on Microsoft Learn's built-in roles page on 2026-10-25 (`83-infrastructure.md` section 6.1).

**Who runs Azure reads.** Every person who runs a `docchat-ops` command, `azd env refresh` or an `az` read in a runbook step is a member of `sg-docchat-operators`, whose Reader on each environment group and on `rg-docchat-shared` and Log Analytics Reader on each environment workspace and on `log-docchat-security` permit those reads (`83-infrastructure.md` section 6.1). Operators are members as people listed in `operators`; approvers are members from `approverObjectIds`; a maintainer, release approver, tenant administrator or Conditional Access Administrator who performs such a step is listed in `operators`. A person outside the group hands the step to a member.

Separation rules, each enforced where named:

1. An approver is never an operator: the bootstrap stops with `approver_is_operator` when an `approverObjectIds` entry is in `operators` (`83-infrastructure.md` section 11.2), and PIM eligibility goes only to the people in `operators`, so whoever approves a grant is never one of the people a grant is made for. An approver's membership of `sg-docchat-operators` gives reads only.
2. Nobody grants themselves a level: `docchat-ops jit grant` refuses with exit 2 when `--member` is the signed-in user, and a self-grant made in Entra raises `docchat-alert-jit-self-grant`.
3. Nobody approves their own `prod` deployment or their own `load-access` gate: both environments have `prevent_self_review: true`.
4. Operators change no Azure resource directly: work that needs Contributor runs as an environment's deploy identity through `deploy.yaml` or `deploy-environment.yaml`, and work that needs Owner is the bootstrap operator's.

### 1.2 Groups

The bootstrap's `groups` step creates every group (`83-infrastructure.md` section 6.13); the Bicep templates bind their rights.

| Group | Members | Rights | Level |
|---|---|---|---|
| `sg-docchat-operators` | standing: the people in `operators` and in `approverObjectIds` (section 1.1, "Who runs Azure reads") | section 1.1 | none |
| `sg-docchat-<env>-conversation-readers` | none outside an active grant | Cosmos DB Built-in Data Reader on `/dbs/docchat/colls/conversations`; SQL role `docchat_conversation_reader` | `read` |
| `sg-docchat-<env>-user-data-operators` | none outside an active grant | Cosmos DB Built-in Data Contributor on `/dbs/docchat`; SQL role `docchat_user_data_operator`; Storage Blob Data Reader on the container `documents` | `user_data` |
| `sg-docchat-<env>-config-operators` | none outside an active grant | SQL role `docchat_config_operator`; Storage Queue Data Message Processor on `ingestion-requests-poison`; in `dev` only, where every account and service holds test data: Cognitive Services User (`a97b65f3-24c7-4388-baec-2e87135dc908`) at `rg-docchat-dev`, for the Azure OpenAI and Content Safety calls of `chat model-check` and the Prompt Shields and Document Intelligence live contract suites, and Search Service Contributor (`7ca78c08-252a-4471-8644-bb5ff32d4ba0`) with Search Index Data Contributor (`8ebe5a00-799e-43f5-93ac-243d3dce84a7`) at `rg-docchat-dev`, for the AI Search live contract suite and its own `contract-` index (`84-testing.md` section 1.6); the bootstrap's `assignments` step creates these three | `config` |
| `sg-docchat-<env>-sql-admins` | the environment's deploy identity only | Entra administrator of the SQL server | none; no person is ever a member |
| `sg-docchat-<env>-users`, `sg-docchat-<env>-admins` | managed by their owners, the people in `groupOwners` | the API's `User` and `Admin` app roles and the web client's default access | none; product access, not operator access |

The three level groups exist for `dev`, `prod` and `load`. Their object IDs are the fields `groups.conversationReaders`, `groups.userDataOperators` and `groups.configOperators` of `infra/env/<env>.json`, which `docchat-ops jit` reads from the checkout.

### 1.3 Just-in-time access

| Level | Group | What it allows | Commands that need it |
|---|---|---|---|
| `read` | `sg-docchat-<env>-conversation-readers` | reading one user's sessions, turns and traces | `user read-conversation` |
| `user_data` | `sg-docchat-<env>-user-data-operators` | reading and deleting one user's data; changing stored traces; reading audit rows; reading one uploaded file through `document fetch-source`, while its container-scope blob role permits a direct read of any blob of `documents` during the activation (`82-security.md` section 1.6) | `user export`, `user delete`, `user verify`, `user redact-document`, `user deletion-register`, `user audit-events`, `document fetch-source` |
| `config` | `sg-docchat-<env>-config-operators` | ops config, per-user index overrides, ingestion recovery, the poison queue | `config show`, `config set`, `config reset`, `override list`, `override set`, `override clear`, `reindex start`, every `ingestion` command, `chat capacity-check` run by a person, `chat model-check` (in `dev` only), `testusers rotate` (in `dev`, for its audit row) |

Rules for every level (`13-conversations-and-memory.md` section 10.1 and 10.6):

1. **Ticket first.** A grant names a ticket that states the purpose, the environment, the identifiers involved (user, session, turn, document, index, run or key) and a duration of at most 4 hours.
2. **Second person.** A group owner grants: through PIM for Groups where the tenant has Entra ID P2 or ID Governance (eligible membership; activation needs a justification, the ticket number, multi-factor authentication and a group owner's approval, for at most 4 hours), otherwise with `docchat-ops jit grant` (script mode, section 2.8.5).
3. **Fresh sign-in.** After the grant, the operator runs `az logout`, then `az login --tenant <tenant ID>`, so new tokens carry the membership.
4. **End.** In script mode the group owner runs `docchat-ops jit revoke` when the work ends and at `expiresAt` at the latest; PIM ends an activation by itself. The operator then runs `az logout`. A token issued during the membership stays valid until it expires, at most 90 minutes after issue, and the monthly access review (R19) matches each data-plane window to its grant with that margin.
5. **Fallback mode.** When the environment's `DOCCHAT_COSMOS_GROUP_DATA_ACCESS` is `false`, the levels `read` and `user_data` also create and remove a per-principal Cosmos DB data-plane assignment with the level's role and scope, under the approver's DocChat Cosmos Data Access Granter (`83-infrastructure.md` section 6.10).
6. **Signals.** Every membership change raises `docchat-alert-jit-grant`; a self-grant raises `docchat-alert-jit-self-grant`; a membership that outlives 4 hours 15 minutes raises `docchat-alert-jit-overdue`; every human read of the `docchat` Cosmos DB database raises `human-conversation-access`; every fallback assignment raises `docchat-alert-cosmos-data-access`. Grants and revocations are recorded by Entra ID's audit log and the Azure Activity Log, never as `app.audit_events` rows (`80-data-model.md` section 2.21, rule 9).

```mermaid
sequenceDiagram
  participant O as Operator
  participant T as Ticket
  participant G as Group owner (approver)
  participant E as Microsoft Entra ID
  participant S as SQL or Cosmos DB
  O->>T: ticket with purpose, identifiers, at most 4 hours
  O->>G: request the level, quoting the ticket
  G->>E: PIM approval, or docchat-ops jit grant
  E-->>G: membership; docchat-alert-jit-grant fires
  O->>O: az logout, az login --tenant
  O->>S: docchat-ops command with --ticket and --reason
  S-->>O: result; audit row in app.audit_events
  G->>E: docchat-ops jit revoke, or PIM expiry
  O->>O: az logout
```

### 1.4 Tickets and reasons

1. A ticket lives in the organisation's tracking system. Its ID is the value of `--ticket`: 1 to 64 characters of letters, digits, `-`, `_` and `#` (`^[A-Za-z0-9_#-]{1,64}$`), an identifier and never free text.
2. `--reason` is 10 to 500 characters stating the purpose. It never contains a person's name, an e-mail address, a document title or conversation content; it is stored only as `change_reason` of `app.ops_config` for a config change and as `reason` of `app.user_index_overrides` for an override, and never in audit metadata (`80-data-model.md` section 2.21, rule 4).
3. Findings go into the ticket by reference only: identifiers, counts, codes and times, never conversation text or document text.

### 1.5 Operator workstation

Every runbook assumes these prerequisites unless it states others:

| Item | Requirement |
|---|---|
| Azure CLI | 2.91.0 (at least 2.76.0, `83-infrastructure.md` section 9.4), with no extension installed and `az config set extension.use_dynamic_install=no` run once |
| Azure Developer CLI | 1.35.1 |
| uv | 0.13.1 |
| GitHub CLI | 2.103.2 (confirmed 2026-10-25 on its releases page), signed in with `gh auth login` |
| Bicep CLI | 0.47.16, for the bootstrap only |
| Node.js | 24.21.0, for a local web deploy only |
| Checkout | a clone of the repository on `main`, updated with `git pull --ff-only` and synced with `uv sync --locked`; `uv run docchat-ops …` and `uv run --frozen docchat-ops …` are then the same command |
| Sign-in | `az login --tenant <tenant ID>` as the operator's own account |
| azd environment | `.azure/<env>/.env`, written by `azd env refresh -e <env> --no-prompt`; on a machine without it, first `azd env new <env> --subscription <subscription ID> --location <region> --no-prompt` and `azd env set AZURE_RESOURCE_GROUP rg-docchat-<env>` |
| Network position | for SQL, Cosmos DB, the data account, AI Search, the environment Key Vault, or the Azure OpenAI and Content Safety accounts (`chat model-check`): a public IPv4 address in that environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST`; for the test-credential vault or the registry: an address in the shared allowlist (with an empty shared list neither admits an operator, so the operator adds the address with R24 step 4 first, while no test job reads the vault, and removes it the same way afterwards) |
| Output volume | an encrypted volume for every `--out` directory |

**KQL in runbooks** runs through the Logs query API v1, because the Azure CLI's query command is an extension:

```text
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

Runbook steps write this as `az rest … <KQL>` with the timespan named when it isn't `PT1H`. For `log-docchat-security` the workspace ID is that workspace's customer ID: `az monitor log-analytics workspace show --resource-group rg-docchat-identity --workspace-name log-docchat-security --query customerId -o tsv`.

**Approving a waiting deployment** (R01, R02, R11, R14, R18, R33): `gh api --method POST /repos/<owner>/<repo>/actions/runs/<run ID>/pending_deployments -F "environment_ids[]=<environment ID>" -f state=approved -f comment="<ticket>"`, with the environment ID from `gh api /repos/<owner>/<repo>/environments/<environment> --jq .id`; `-f state=rejected` rejects it.

## 2. The operator CLI `docchat-ops`

### 2.1 Package, layout and invocation

`scripts/ops` is the uv workspace member with distribution `docchat-ops` and console script `docchat-ops`; it ships no artifact and runs from a synced checkout (`01-repository.md` section 1.7). Code owners: `docchat-backend`.

| Path | Holds |
|---|---|
| `scripts/ops/src/docchat_ops/cli.py` | the `argparse` parser: the global options of section 2.3, one sub-parser per group and command; `main()` runs the chosen command with `asyncio.run` and maps its outcome to an exit code |
| `framework/auth.py` | the credential, the caller-type check and token acquisition (section 2.2) |
| `framework/endpoints.py` | reading `.azure/<env>/.env` and, for `--env local`, the repository's `.env` |
| `framework/audit.py` | building `AuditEvent` values and writing them through `docchat_core.audit.write_audit_event` |
| `framework/confirm.py` | the dry-run summary and the confirmation prompt (section 2.4) |
| `framework/exitcodes.py` | the eight exit codes (section 2.6) and the mapping of exceptions to them |
| `commands/local.py`, `config.py`, `override.py`, `users.py`, `jit.py`, `reindex.py`, `ingestion.py`, `chat.py`, `testusers.py`, `trace.py` | one module per command group (section 2.8) |
| `scripts/ops/tests/unit/` | unit tests mirroring `src/docchat_ops` |

The commands reuse the `docchat_core` repositories, document models and validation models, so no command can write a value or a document the apps would reject. Command semantics are owned by the files named in each group's section; this file owns the framework and the groups `testusers` and `trace`.

**Invocation.** `uv run docchat-ops --env <dev|prod|load|local> <group> <command> [arguments]`. `--env` is required and stands either before the group or after the command (`docchat-ops chat capacity-check --env prod` is the same command as `docchat-ops --env prod chat capacity-check`); a missing `--env`, or two different values, is a usage error (exit 2). `--env local` is accepted only by `local init`; every other command exits 2 with it.

### 2.2 Identity and endpoints

1. **Credential.** `azure.identity.aio.AzureCliCredential`: the operator's own `az login`, or a pipeline identity after `azure/login`. No command accepts a credential, key, password or connection string as an argument.
2. **Caller type.** `framework/auth.py` reads `az account show --query user.type -o tsv`. A `servicePrincipal` caller is refused with exit 3 for every command that changes data, outside `--env load`; the read-only commands `chat capacity-check`, `ingestion status` and `testusers check` accept a service principal in every environment (the deploy identity runs the first after migrations, and `id-docchat-citest` runs the last in the nightly `test-credential-expiry` job). In `load` the `load` deploy identity runs `config set` from the load workflow's configuration jobs (`81-config.md` section 3.3, item 7). `chat model-check` refuses a service principal with exit 3, because it writes an audit row and only a person runs it.
3. **Tokens.** SQL: an access token for `https://database.windows.net/.default`, passed to the `docchat_core` data-access layer, whose connection carries `Encrypt=strict` and `APP=docchat-ops`, so the SQL audit names the component. Cosmos DB: the async `CosmosClient` with the credential, `connection_timeout=5` and a per-operation timeout of 10 s. Logs query API: a token for `https://api.loganalytics.io/.default`, `httpx.AsyncClient` with a 30 s timeout. Key Vault: the async `SecretClient`. Azure OpenAI (`chat model-check`): the `AsyncOpenAI` client of `docchat_core.clients.openai` with a token provider for `https://ai.azure.com/.default` over the same credential (`83-infrastructure.md` section 6.2). Content Safety (`chat model-check`'s Prompt Shields probes): a token for `https://cognitiveservices.azure.com/.default` over the same credential, `httpx.AsyncClient` with a 5 s timeout and `follow_redirects=False`. Both calls are permitted by the `dev` group's Cognitive Services User at `rg-docchat-dev` (section 1.2). Microsoft Graph: a token for `https://graph.microsoft.com/.default`. Group membership and Cosmos DB role assignments (`jit`) and the `load` resource-group tags (`testusers`) go through the Azure CLI as a subprocess, which inherits the same sign-in.
4. **Actor.** Every audit row's `actor_object_id` is the `oid` claim of the caller's SQL access token.
5. **Endpoints.** Read only from `.azure/<env>/.env` (`83-infrastructure.md` section 8.8); a missing file or value exits 2 with the message "run azd env refresh -e <env>". The values each group reads:

| Group | Values from `.azure/<env>/.env` |
|---|---|
| `config`, `override` | `DOCCHAT_SQL_SERVER`, `DOCCHAT_SQL_DATABASE` |
| `reindex` | the SQL values; `DOCCHAT_SEARCH_MAX_INDEXES` (the index-capacity check) and `DOCCHAT_OPENAI_EMBEDDING_MODELS` (the embedding catalogue) |
| `chat` | the SQL values; `capacity-check` also `DOCCHAT_OPENAI_CHAT_CAPACITY`, `DOCCHAT_OPENAI_SMALL_CAPACITY`; `model-check` also `DOCCHAT_OPENAI_ENDPOINT` and `DOCCHAT_CONTENTSAFETY_ENDPOINT` |
| `ingestion` | the SQL values; `DOCCHAT_SEARCH_MAX_INDEXES` and `DOCCHAT_OPENAI_EMBEDDING_MODELS` (the run functions' capacity check and catalogue, and `set-deployments`); `DOCCHAT_STORAGE_QUEUE_ENDPOINT` (`drain-poison`) |
| `user` | the SQL values; `DOCCHAT_COSMOS_ENDPOINT`, `DOCCHAT_COSMOS_DATABASE` |
| `jit` | `DOCCHAT_COSMOS_GROUP_DATA_ACCESS`, `DOCCHAT_COSMOS_ACCOUNT_NAME`, `AZURE_RESOURCE_GROUP`; the group IDs from `infra/env/<env>.json` |
| `testusers rotate` | `.azure/dev/.env`: `TEST_CLIENT_ID`, `API_SCOPE` and the SQL values; the vault URI and user domain from `--vault-uri` and `--domain` |
| `testusers check` | none: the vault URI from `--vault-uri`, default the environment variable `DOCCHAT_TEST_VAULT_URI` |
| `trace` | `DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID` |

### 2.3 Global options

| Option | Rule |
|---|---|
| `--env <dev\|prod\|load\|local>` | required (section 2.1) |
| `--ticket <id>` | required by every command except the five below; section 1.4's pattern; a missing or invalid value exits 2 before any sign-in |
| `--reason <text>` | required wherever `--ticket` is; 10 to 500 characters; otherwise exit 2 |
| `--yes` | skips the confirmation prompt outside `prod`; in `prod` the prompt is always shown (section 2.4) |
| `--json` | prints the report as one JSON object instead of a table |

Five commands read no personal data and change nothing in an Azure store, so they take neither `--ticket` nor `--reason`, and `argparse` refuses either option with exit 2: `local init`, `config show`, `chat capacity-check`, `ingestion status` and `testusers check`.

### 2.4 Confirmation

1. Every command that changes a store prints a dry-run summary first: the environment, the targets by identifier, counts, and old and new values for ops config; never a name, an e-mail address or content.
2. Outside `prod` it asks `Type yes to continue`, unless `--yes` is given. In `prod` it always asks `Type the environment name to continue` and accepts only `prod`; with no terminal attached (standard input isn't a TTY) a `prod` change exits 2 before the summary.
3. A declined prompt exits 2 with nothing written.
4. The commands that confirm: `config set`, `config reset`, `override set`, `override clear`, `user delete` (which also requires `--confirm <oid>`), `user redact-document`, `jit grant`, `jit revoke`, `reindex start`, every `ingestion` command except `status`, `testusers rotate`, and `local init` when `.env` already exists. Reads (`show`, `list`, `export`, `fetch-source`, `read-conversation`, `verify`, `deletion-register`, `audit-events`, `capacity-check`, `model-check`, `check`, `trace show`) don't ask; `model-check` changes no store beyond its own audit row.

### 2.5 Correlation, audit and output

1. **Correlation ID.** Each run draws one random W3C trace ID (32 lower-case hexadecimal characters, not all zero), prints `correlationId: <id>` as the first line of standard error before any network call, writes it as `audit_events.correlation_id` of every row the run inserts, and names it in every error message. The CLI exports no spans (`85-observability.md` section 2.9).
2. **Audit.** A command that changes a SQL table writes its audit row in the same SQL transaction as the change; a command that changes Cosmos DB writes its row in the SQL transaction that records the outcome (sections 2.8.4 and 2.8.6). Every row has `actor_type` `operator`, the actor of section 2.2, the action of `80-data-model.md` section 2.21 and its content-free metadata, which includes `ticket` wherever the catalogue lists it. An ops-config value whose metadata would exceed 4,000 characters is replaced by `previousSha256` and `newSha256` (rule 7 there).
3. **No audit row** is written by `local init`, `config show`, `override list`, `jit grant`, `jit revoke`, `jit list` (Entra ID's audit log records membership changes), `user verify`, `user deletion-register`, `user audit-events`, `ingestion status`, `chat capacity-check`, `testusers check` and `trace show`: each changes no application data or prints identifiers, counts and times only. The catalogue's 43 actions are the complete list.
4. **Output.** The report goes to standard output (a table, or one JSON object with `--json` carrying `correlationId`, `command`, `environment`, `result` and the command's fields); progress and errors go to standard error. Standard output never carries conversation text, document text, a name or an e-mail address; content leaves the stores only in files under `--out`.
5. **Files.** `--out <dir>` is created with mode 0700 when absent; a directory that already holds files is refused with exit 2. Every file is written with mode 0600 and closed before the audit row commits.

### 2.6 Exit codes

| Code | Meaning | Typical causes |
|---|---|---|
| 0 | success | |
| 1 | unexpected error | a bug; the message names the correlation ID |
| 2 | usage error, validation failure or refused precondition; nothing written, except the `failed` audit row of `chat model-check` | `argparse` errors; a ticket or reason outside its rule; an unknown ops-config key or an out-of-range value; a declined prompt; a self-grant; a precondition of the command's table; a failed deployed model check |
| 3 | access denied | no sign-in; no just-in-time membership; a `401` or `403` from a store; SQL login refused; a service principal on a data-changing command outside `load` |
| 4 | not found | an unknown user, session, index, run, item or override; in `prod`, an absent capacity output for `chat capacity-check` |
| 5 | incomplete; run the same command once more | a multi-store operation stopped part-way; it is idempotent |
| 6 | conflict; nothing more written | a row or item changed since it was read; `jit grant` for a member already in the group; `user redact-document` when a trace's ETag conflicts remain after 3 attempts |
| 7 | store unreachable | Cosmos DB, SQL, a queue, Key Vault, the Logs query API or Microsoft Graph unreachable through the network path, the firewall or a timeout |

On a non-zero exit the command writes one line to standard error: the code's meaning, a message naming the cause and the next step, and the correlation ID; it never contains a token, a password, a key or content.

### 2.7 How a changing command runs

1. Parse the arguments; refuse a missing or invalid `--ticket` or `--reason` (exit 2).
2. Print the correlation ID; load `.azure/<env>/.env` (exit 2 when absent); build the credential; refuse a service-principal caller where section 2.2 says so (exit 3); acquire a token for each target store (exit 3, "sign in with az login", on failure).
3. Read the current state, including the version the change will be conditioned on (`row_version` for SQL rows, the ETag for Cosmos DB items, the item state and lease for ingestion items); validate the change with the owning file's model (exit 2, quoting the allowed range).
4. Print the dry-run summary and confirm (section 2.4).
5. Apply the change conditioned on the state read in step 3 and insert the audit row, in one SQL transaction through the data-access layer with no automatic retry. Zero rows affected rolls back and exits 6, "changed by someone else; read it and rerun the command".
6. Print the result and the audit event ID; exit 0. A firewall refusal or a timeout exits 7, and a `401` or `403` exits 3, both with nothing written, because step 5 is one transaction. A multi-store command (`user delete`, `user redact-document`, `testusers rotate`) that stops part-way exits 5 and finishes on a rerun.

### 2.8 Command catalogue

Each table lists, per command, its arguments beyond the global options, the just-in-time level it needs (section 1.3), its effect, the exit codes it adds to 0, 1, 3 and 7, and its audit action. Every command honours section 2.6.

#### 2.8.1 `local` (`commands/local.py`)

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `local init` | none; `--env local` only | writes the git-ignored `.env` at the repository root from `.env.example`, with `DOCCHAT_LOCAL_SQL_SA_PASSWORD` set to 24 characters from `secrets.choice` over upper-case letters, lower-case letters, digits and the symbols `!#%+-=?@^_`, holding at least one of each of the four classes (SQL Server's complexity rules); mode 0600; asks before replacing an existing `.env` (`81-config.md` section 2.11) | 2 for any other `--env` or a declined replacement | none |

#### 2.8.2 `config` (`commands/config.py`; semantics `81-config.md` section 3.3)

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `config show [--key <key>]` | `config` | prints each key (or one) with its value, default, last change time and actor object ID; reads no personal data; takes no `--ticket` or `--reason` | 2 unknown key | none |
| `config set --key <key> --value '<JSON value>'` | `config` | refuses a key outside the catalogue; validates the value with the area's model against the area's current values, cross-field rules included; prints old and new values; confirms; in one transaction updates the row `WHERE config_key = :key AND row_version = :rv`, storing `change_reason` and `updated_by`, and inserts the audit row; prints that the value is live within 60 seconds | 2 unknown key, out-of-range value, broken cross-field rule, reason outside 10 to 500 characters; 3 also a service-principal caller in `dev` or `prod`; 6 the row changed since it was read | `ops_config.set` (`area`, `previous`, `new`, `ticket`; `previousSha256` and `newSha256` above 4,000 characters) |
| `config reset --key <key>` | `config` | sets `value_json` to `default_json` under the same checks | as `set` | `ops_config.reset` (`area`, `previous` or `previousSha256`, `ticket`) |

#### 2.8.3 `override` (`commands/override.py`; semantics `81-config.md` section 3, `80-data-model.md` section 2.6)

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `override list [--user <oid>] [--project <id>]` | `config` | prints the matching `app.user_index_overrides` rows: user object ID, project ID, index ID, `set_by`, `set_at`, `expires_at` and an `expired` flag; never the reason | none added | none |
| `override set --user <oid> --project <id> --index <id> [--days <1 to 30>]` | `config` | `--days` defaults to 7; requires a `users` row for the user and a mapping of the index to the project; in one transaction updates the row under `UPDLOCK, HOLDLOCK` or inserts it, storing `--reason` in `reason`, and inserts the audit row; prints the expiry; the override applies to the user's next chat request | 2 index not mapped to the project, `--days` outside 1 to 30; 4 no `users` row | `user_index_override.set` (`searchIndexId`, `expiresAt`, `replaced`, `ticket`) |
| `override clear --user <oid> --project <id>` | `config` | deletes the row and inserts the audit row | 4 no row | `user_index_override.cleared` (`searchIndexId`, `ticket`) |

Expired rows stay until `set` or `clear` replaces or removes them; chat ignores them.

#### 2.8.4 `user` (`commands/users.py`; semantics `13-conversations-and-memory.md` sections 10.5 to 10.11)

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `user read-conversation --user <oid> --session <sessionId> [--turn <turnId>]… --out <dir>` | `read` | point-reads the session; reads the session's turns and traces (the given turns, at most 50, or the newest 50); writes `<out>/<sessionId>.json` without Cosmos DB system fields; inserts the audit row; prints counts | 2 more than 50 `--turn`; 4 session not found | `conversation.read` (`turnIds`, `ticket`, `level` `read`) |
| `user export --user <oid> --out <dir>` | `user_data` | writes the packages `subject/` and `internal/` and `manifest.json` of section 10.8 there; inserts the audit row | 4 no data in any store | `user_data.exported` (`ticket`, `counts`, `manifestSha256`) |
| `user delete --user <oid> --confirm <oid> --assignment-removed` | `user_data` | prints the counts; deletes the user's `conversations` items (traces and turns before sessions, 8 at a time), the `memory` document and the `usage` items; then in one SQL transaction the `user_index_overrides`, `project_members` and `users` rows with the audit row; recounts | 2 `--confirm` differs from `--user` or `--assignment-removed` missing; 5 something remains | `user_data.deleted` (`ticket`, `counts`) |
| `user verify --user <oid>` | `user_data` | counts the user's items in the three containers and rows in `users`, `project_members` and `user_index_overrides`; run no sooner than 2 hours after the assignment removal | 5 any count above 0 | none |
| `user redact-document --document-id <uuid>` | `user_data` | finds every `turn_trace` that kept the document, prints the count, confirms, replaces each of that document's source blocks with `[removed]` under the item's ETag (at most 3 attempts) and sets `redactedAt`; after the last page inserts the audit row and prints the (`userId`, `sessionId`) pairs of the traces found; answers and summaries stay unchanged | 5 stopped part-way; 6 an ETag conflict remains after 3 attempts | `document.traces_redacted` (`documentId`, `traceCount`, `ticket`) |
| `user deletion-register --since <UTC time>` | `user_data` | runs the constant statement of section 10.11 with `:restore_point` = `--since` (ISO 8601 UTC, `yyyy-MM-ddTHH:mm:ssZ`) and prints each row's `action`, `subject_object_id`, `target_id` and `occurred_at`, oldest first; `--json` gives the list a restore works from | 2 a malformed or future time | none |
| `user audit-events --actor <oid> --since <UTC time>` | `user_data` | prints the `app.audit_events` rows whose `actor_object_id` is the given object ID and whose `occurred_at` is at or after `--since`, newest first: `occurred_at`, `action`, `target_type`, `target_id`, `project_id` and `metadata_json` (content-free by rule 4 of `80-data-model.md` section 2.21); used to review a person's recent admin changes | 2 a malformed or future time, or `--since` more than 400 days back | none |

`user delete` attests with `--assignment-removed` that, before the run, the tenant administrator removed the user's direct assignments to the web client and API enterprise applications and a group owner removed the user from `sg-docchat-<env>-users` and `sg-docchat-<env>-admins`, or a User Administrator deleted the account (R06 step B1): a user who can still sign in is provisioned anew by the next request. It keeps every `audit_events` row, every actor column and every organisational document and blob.

#### 2.8.5 `jit` (`commands/jit.py`; semantics `13-conversations-and-memory.md` section 10.6)

Run by a group owner in script mode, when the tenant has no PIM for Groups. The commands call Microsoft Graph through the Azure CLI under group ownership and, in fallback mode, Azure Resource Manager under DocChat Cosmos Data Access Granter.

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `jit grant --level read\|user_data\|config --member <operator oid> --hours <1 to 4>` | group owner | resolves the level's group; runs `az ad group member add`; in fallback mode, for `read` and `user_data`, also `az cosmosdb sql role assignment create` with the level's role and scope and a new UUIDv4 assignment ID; prints the receipt `{group, member, level, grantedAt, expiresAt, ticket, mode, assignmentId}`, `expiresAt` = `grantedAt` + `--hours` | 2 `--member` is the signed-in user (`az ad signed-in-user show`), `--hours` outside 1 to 4; 6 the member already belongs to the group | none (Entra audit log; `docchat-alert-jit-grant`) |
| `jit revoke --level read\|user_data\|config --member <oid>` | group owner | runs `az ad group member remove`; in fallback mode deletes every data-plane assignment of that member at the level's scope; prints a receipt | 4 the member is in neither the group nor an assignment | none |
| `jit list` | any signed-in operator or owner | prints the members of the environment's three groups and, in fallback mode, the per-principal data-plane assignments; every list is empty outside active tickets | none added | none |

Nothing removes a script-mode membership automatically; `docchat-alert-jit-overdue` fires when one outlives 4 hours 15 minutes.

#### 2.8.6 `reindex` and `ingestion` (`commands/reindex.py`, `commands/ingestion.py`; semantics `14-ingestion.md` section 19)

Every command calls `docchat_core.ingestion.runs`, the functions the API calls, writes its audit row in the transaction of the change and prints IDs, states, counts and times only. A run an operator creates starts at the dispatcher's next wake (within 60 seconds while work is outstanding, within `ingestion.idle_wake_minutes` when idle), because operators hold no queue-send right.

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `reindex start --index-id <uuid>` | `config` | the checks and transaction of `adminStartReingest`; prints the run ID and the mode (`same_index` or `new_physical_index`) | 2 status doesn't allow a re-ingest or the search service has no room; 4 unknown index; 6 concurrent change | `ingestion.reindex_started` (`runId`, `mode`, `itemCount`, `ticket`) |
| `ingestion status` | `config`; a service principal is accepted | prints the `ingestion_dispatcher_state` row, queued and running item counts per priority, the oldest queued item's age, the open runs and the last 5 scanner canaries; takes no `--ticket` or `--reason` | none added | none |
| `ingestion requeue-item --item-id <uuid>` | `config` | a `running` item whose lease has expired becomes `queued` with `lease_expires_at` null; `claim_count` stays | 2 the lease is valid or the item isn't `running`; 6 the row changed | `ingestion.item_requeued` (`runId`, `claimCount`, `ticket`) |
| `ingestion fail-item --item-id <uuid>` | `config` | a `queued` item, or a `running` item whose lease has expired, becomes `failed` with `processing_interrupted` and every failure side effect | 2 a valid lease or a finished item; 6 the row changed | `ingestion.item_failed` (`runId`, `outcomeCode`, `ticket`) |
| `ingestion cancel-run --run-id <uuid>` | `config` | cancels the run's `queued` items; `running` ones finish | 2 finished run; 4 unknown run; 6 the run changed | `ingestion.run_cancelled` (`indexId`, `cancelledItemCount`, `ticket`) |
| `ingestion abandon-rebuild --index-id <uuid>` | `config` | moves the building physical index to `retiring` with `delete_after` = now, cancels its queued items, adds a `delete_physical_index` item in a `maintenance` run and recomputes the logical index's status | 2 no building physical index; 6 changed meanwhile | `ingestion.rebuild_abandoned` (`indexId`, `generation`, `cancelledItemCount`, `runId`, `ticket`) |
| `ingestion set-deployments --physical-index-id <uuid> --query <name> --ingestion <name>` | `config` | sets the physical index's query and ingestion deployment names | 2 unless the `DOCCHAT_OPENAI_EMBEDDING_MODELS` entry with those names has the index's model and model version | `ingestion.deployments_changed` (`indexId`, `generation`, the four deployment names, `ticket`) |
| `ingestion drain-poison` | `config` | the dry run peeks at most 32 messages of `ingestion-requests-poison` (`peek_messages(max_messages=32)`, which changes nothing) and prints their count, written "at least 32" when it saw 32, and the distinct run IDs they name; after confirmation it receives up to 32 messages (`receive_messages(max_messages=32)`), deletes each received message (`delete_message`), and repeats until a receive returns none; then prints the number drained and inserts the audit row | 5 a receive or delete failed part-way (the count drained so far is printed and a rerun drains the rest); 7 the queue is unreachable before any delete | `ingestion.poison_drained` (`deletedCount`, `ticket`) |

#### 2.8.7 `chat` (`commands/chat.py`; semantics `12-chat-pipeline.md` sections 3.7 and 10.7)

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `chat capacity-check [--set <key>=<JSON value>]…` | `config` for a person; the deploy identity in the `postprovision` hook | reads the capacities `DOCCHAT_OPENAI_CHAT_CAPACITY` and `DOCCHAT_OPENAI_SMALL_CAPACITY` (an empty value counts as absent) and the values of `app.ops_config`; each repeatable `--set` replaces one value in memory, validated by its area's model, and writes nothing; prints the chat share and the chat-small share; takes no `--ticket` or `--reason` | 0 both shares at or below 25%, and always in `dev` and `load`; 2 in `prod` a share above 25%, or a `--set` value its model refuses; 4 in `prod` a capacity absent | none |
| `chat model-check` | `config` in `dev`; `--env dev` only; a person only (a service principal exits 3); from an address in `dev`'s operator allowlist | the deployed model check, the tokenizer check, the chat-small latency check and the two Prompt Shields probes of `12-chat-pipeline.md` section 10.7 against the `dev` deployments and the `dev` Content Safety account. Reads `DOCCHAT_OPENAI_ENDPOINT`, `DOCCHAT_CONTENTSAFETY_ENDPOINT` and the SQL values from `.azure/dev/.env`; the deployment names, models and encodings from `openAi.deployments.chat` and `openAi.deployments.chatSmall` of `infra/env/dev.json`; the answer and chat-small reasoning efforts and prompt-cache settings from key `api` of `infra/config/app-settings.json`; `summary.reasoning_effort`, `summary.max_completion_tokens`, `summary.max_input_tokens`, `summary.timeout_seconds`, `memory.reasoning_effort`, `memory.max_completion_tokens` and `memory.timeout_seconds` from `app.ops_config`. Calls `chat.completions.create` through the `AsyncOpenAI` client of `docchat_core.clients.openai` with a token from `azure.identity.aio.AzureCliCredential` for `https://ai.azure.com/.default`: one call per deployment and configured effort with its prompt-cache setting; on chat small strict structured output for `standalone_query`, `search_passage`, `followup_questions` and the memory-extraction schema (10 fixture questions); one tokenizer probe per deployment (about 2,000 tokens, `max_completion_tokens` 16) compared with `usage.prompt_tokens`; 20 `summary`-shaped and 20 `memory`-shaped calls on chat small, whose p95 durations it compares with `summary.timeout_seconds` and `memory.timeout_seconds`; at most 60 calls, each with the client's 60-second read timeout, sequentially. Then sends the request probe `{"userPrompt": "Works on supplier contract renewals", "documents": []}` and the empty-`userPrompt` probe with `SHIELD_PROBE_DOCUMENT` to `{DOCCHAT_CONTENTSAFETY_ENDPOINT}/contentsafety/text:shieldPrompt?api-version=2024-09-01`, with a token for `https://cognitiveservices.azure.com/.default` from the same credential, 5-second timeout, no retry. Both data planes are permitted by Cognitive Services User through `sg-docchat-dev-config-operators`. Prints each call's deployment, kind, effort and status, each tokenizer probe's local and reported counts, each latency set's p95 and timeout, and each Prompt Shields probe's status code, `attackDetected` and outcome (`passed`, `documents_refused`, `user_prompt_refused` or `failed`), and writes every one of these results to its audit row; changes no store except that row; asks for no confirmation | 2 any `--env` other than `dev`, any model call answered `400`, a structured output that fails its schema, a tokenizer difference above 1%, a latency p95 above its timeout or a failed latency call, or a Prompt Shields probe outcome `failed` (the audit row records `failed`); 3 a service-principal caller, or `401` or `403` from either account (no Cognitive Services User or no level); 7 either account unreachable (an address outside the allowlist, or a connection timeout) | `model_deployments.checked` (`environment`, `result`, `callCount`, `failures`, `tokenizer`, `latency`, `promptShields`, `ticket`) |

#### 2.8.8 `testusers` (`commands/testusers.py`)

`testusers rotate --set <dev|load|all> [--vault-uri <uri>] [--domain <domain>]`, with `--env dev` only and level `config` in `dev` (for its audit row), run by a rotating operator (section 1.1) from an address in the shared allowlist:

1. Reads `--vault-uri` and `--domain` (defaults: the environment variables `DOCCHAT_TEST_VAULT_URI` and `DOCCHAT_TEST_USER_DOMAIN`, exported from the GitHub environment `dev-tests`) and `TEST_CLIENT_ID` and `API_SCOPE` from `.azure/dev/.env`. Takes a Graph token, a Key Vault token and a SQL connection to `dev` as the operator; any failure exits 3 before anything changes. When the sets include `load`, reads `az group show --name rg-docchat-load --query tags -o json`; a `docchat-load-run` tag exits 2 with `load_run_in_progress` and changes nothing, because a running load test holds the current passwords.
2. For each alias of `testUserAliases` (`81-config.md` section 4.7; the `dev` set `member`, `outsider`, `norole`, `admin` first, then `load-01` to `load-20` and `load-admin`): generates a 32-character password with `secrets.choice`; `PATCH https://graph.microsoft.com/v1.0/users/<upn>` with `passwordProfile {password, forceChangePasswordNextSignIn: false}` (a `403` means the Password Administrator assignment on the administrative unit is missing: exit 3).
3. Writes the secret: `SecretClient.set_secret("test-user-<alias>", password, content_type="text/plain", expires_on=now + 30 days, tags={"docchat-test-user": "<alias>"})`. DocChat Test Secret Writer sets secrets and reads metadata, never a value.
4. For the `dev` set only: waits 30 s, then proves the resource-owner password sign-in of that user against the `dev` test client for `API_SCOPE` (up to 3 attempts 30 s apart); `AADSTS50126` after the third attempt exits 5 naming the alias, and a rerun completes the rotation. The `load` set has no registrations between runs; the next load run's token step proves it.
5. Inserts the `dev` audit row `test_users.rotated` (`environment` `dev`, `aliasCount` 1 to 25) and prints each alias with its new expiry.

Any failure stops the run, names the alias and leaves the remaining aliases on their earlier passwords.

`testusers check --set <dev|load|all> [--min-hours <n>] [--vault-uri <uri>]`, with `--env dev` or `--env load`, no level, no azd environment file, no `--ticket` or `--reason`, and a service principal accepted: lists each selected secret's properties with `list_properties_of_secrets` (no value is read), prints alias and `exp`, and exits 5 when any `exp` has passed, or falls within `n` hours when `--min-hours` is given; `--json` adds a `warnings` list of the aliases whose `exp` falls within 7 days. The nightly `test-credential-expiry` job runs `uv run docchat-ops --env dev testusers check --set all --json` as `id-docchat-citest`: by default inside the vault's read window, which the job opens with `firewall.py open --target vault` in the step before and closes in the step after (`83-infrastructure.md` section 10.16); under the private-networking option through the vault's private endpoint.

#### 2.8.9 `trace` (`commands/trace.py`)

`trace show <trace ID>`, any operator (Log Analytics Reader on the environment workspace), no just-in-time level: refuses an ID that isn't 32 lower-case hexadecimal characters (exit 2); runs the reference-ID query of `85-observability.md` section 2.11 through the Logs query API against `DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID` over the last 30 days, then the dispatcher-link query of the same section; prints, in time order, each record's time, type, name, duration, success, result code and role name, and from `Properties` only the attributes `docchat.error_code`, `docchat.session_id`, `docchat.turn_id` and `enduser.id` where present. No row exits 4. Audit: none.

#### 2.8.10 `document` (`commands/document.py`; semantics `14-ingestion.md` section 19.1)

No API operation and no web client action serves an uploaded file (`82-security.md` SEC-54); this command is the supported way to retrieve one, for an audit or legal request (runbook R37). The level's Storage Blob Data Reader role on the container `documents` also permits a direct read of any blob of the container during the activation; the activation and the `StorageRead` entries of `StorageBlobLogs` record such a read, and `82-security.md` section 1.6 accepts it as an operator-trust residual.

| Command | Level | Effect | Exits | Audit |
|---|---|---|---|---|
| `document fetch-source --version <uuid> --out <path>` | `user_data` | reads the version's `blob_path`, `sha256` and `content_type` from `app.document_versions`; prints that the file is untrusted, an upload whose content no one has checked for safety, to be opened only in an isolated environment; downloads the source from the container `documents` with `download_blob()` under the operator's own credential (Storage Blob Data Reader of `sg-docchat-<env>-user-data-operators`), checks its SHA-256 against `document_versions.sha256`, writes it to `<path>` with mode 0600 and no execute bit (`os.open(<path>, os.O_WRONLY \| os.O_CREAT \| os.O_EXCL \| os.O_NOFOLLOW, 0o600)`, so no existing file is replaced and no symbolic link is followed), then inserts the audit row and prints the version ID, the media type, the size and the hex SHA-256; never a name or content | 2 `<path>` exists, or the version is `failed` with `source_missing`; 4 unknown version or missing blob; 5 the bytes don't match the version's SHA-256 (the partial file is removed); 7 Blob Storage unreachable | `document.source_fetched` (`indexId`, `documentId`, `versionNumber`, `sizeBytes`, `ticket`), in a transaction of its own after the file is written and checked; a failed insert removes the file and exits 1 |

### 2.9 Proof of the framework

The operator CLI unit tests (`84-testing.md`) cover: the ticket pattern and the reason length; the five commands refusing `--ticket` and `--reason`; `--env` in both positions and the two-value refusal; the `prod` typed confirmation and the no-terminal refusal; the service-principal refusal and the three read-only exceptions; the correlation ID printed first and written to the audit row; one test per exit code; output file and directory modes; and, per command, the audit action and metadata model of section 2.8 against `80-data-model.md` section 2.21.

## 3. Runbooks

### 3.1 Conventions

1. **Form.** Each runbook states its performer, its trigger, its prerequisites beyond section 1.5, numbered steps with one exact command and its expected result each, its verification, its undo and the audit record it produces. Its text is the runbook file of section 4.1.
2. **Placeholders.** Values in angle brackets are filled in at run time; a value from `.azure/<env>/.env` is written by its output name (`<DOCCHAT_API_APP_NAME>`). `<current release commit>` is the commit the environment serves: `az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv`.
3. **`docchat-ops` outcomes.** Exit 3: check the membership, then `az logout` and `az login --tenant <tenant ID>`. Exit 5: run the same command once more. Exit 6: read the state and rerun. Exit 7: check that the machine's address is in the allowlist (section 1.5) and that the store is healthy. Every run prints its correlation ID first; the ticket records it.
4. **Alerts and workbooks** are named as `85-observability.md` names them; a workbook tile is named by its title, for example the `ingestion` tile "Latest failed items".
5. **Credentials and their rotation.** No registration has a client secret or certificate, no service uses an account key outside fallback 1, and the repository holds no Actions secret. The credentials that exist:

| Credential | Where | Rotation | Runbook |
|---|---|---|---|
| Static Web Apps deployment token | the Static Web App, read at deploy time | every 90 days and on suspected exposure | R14 |
| Test-user passwords (`test-user-<alias>`) | the shared test-credential vault | every 30 days and on suspected exposure | R08 |
| Host-account key | `ingestion-content-share-connection` in the environment vault, under fallback 1 only | every 90 days and on suspected exposure | R25 |
| Local SQL `sa` password | the developer's git-ignored `.env` | `docchat-ops local init` writes a new one | none |

### 3.2 Runbook index

| ID | Title | Performer | Trigger |
|---|---|---|---|
| R01 | Deploy | automatic in `dev`; release approver in `prod` | a merge to `main` |
| R02 | Rollback | release approver | a release that caused an incident |
| R03 | Change an ops-config value | operator, level `config` | a ticket for a value; an alert naming a key |
| R04 | Per-user index override | operator, level `config` | a ticket to test an index for one user; `index-override-used` |
| R05 | Re-index an index | admin, or operator at level `config` | a method or parameter change; a new embedding model |
| R06 | User data export and deletion | operator, level `user_data`; tenant administrator, group owner or User Administrator for the access removal; bootstrap operator for a restore | a data-subject request; a point-in-time restore |
| R07 | Incident response | on-call operator; security responder; User Administrator, group owner and tenant administrator for containment | a Sev1 alert; an alert whose runbook is this one; a security event |
| R08 | Test-user password rotation | rotating operator | every 30 days; suspected exposure |
| R09 | Model version upgrade | maintainer; operator, level `config` in `dev`, for the model check | a `Model retirement:` issue; `model_retires_soon`; a model alert |
| R10 | Zone-redundancy conversions | maintainer | a zone-redundancy decision |
| R11 | Run the load test | maintainer; tenant administrator at the gate; Conditional Access Administrator for the policy changes; bootstrap operator for a failed teardown | a change that needs load evidence |
| R12 | Defender acceptance check | operator | first provision with scanning on; `uploads-scan-gate-closed` |
| R13 | First admins and test identities | User Administrator, Privileged Role Administrator, tenant administrator, Conditional Access Administrator, group owner, each for its part | a new tenant or environment; `role-not-assigned` |
| R14 | Static Web Apps deployment token reset | bootstrap operator, then maintainer | every 90 days; suspected exposure |
| R15 | Find the trace behind a user's reference ID | operator | a user's error report |
| R16 | Repository and GitHub settings | organization owner | once; after a repository re-creation |
| R17 | Dependency, base-image and tool updates | maintainer | weekly, monthly and quarterly |
| R18 | Ingestion failures and stuck runs | operator, level `config` | the ingestion alerts |
| R19 | Read a user's conversation; access review | operator, level `read` | a ticket that needs content; monthly; conversation alerts |
| R20 | Alert response index and runbook maps | on-call operator | any alert |
| R21 | Bootstrap a tenant and subscription | bootstrap operator | a new tenant or subscription; a bootstrap configuration change |
| R22 | First provision of an environment | maintainer, with the post-provision performers | a new environment |
| R23 | Re-tighten role-assignment conditions | bootstrap operator | a re-created workload identity; `dev` scanning about to turn on |
| R24 | Change the operator allowlist | operator with GitHub environment admin rights; bootstrap operator for the shared list | an address change |
| R25 | Rotate the host-account key (content-share fallback 1 only) | bootstrap operator | every 90 days under fallback 1 |
| R26 | Remove a stray runner rule | bootstrap operator | `rule_close_failed`; a stray rule, among them a registry or test-vault address outside the shared allowlist, which no job removes |
| R27 | Content-share fallback activation | maintainer and bootstrap operator | the content-share spike fails |
| R28 | Prune the registry | bootstrap operator | quarterly |
| R29 | Budget alert at 80% or 100% | operator | `actual-80`, `actual-100` |
| R30 | Decommission an environment | bootstrap operator; tenant administrator for the registrations; Security Administrator for `docchat-prod-audit` | the product owner's decision |
| R31 | Suspected pipeline compromise | bootstrap operator with a security responder | an unexplained security alert; a leaked pipeline credential; an unreviewed change on `main` |
| R32 | Remove a withdrawn document's text from conversation data (legal removal) | operator, level `user_data` | a legal removal request |
| R33 | Reset the Durable task hub | maintainer; operator, level `config`; release approver in `prod` | a dispatcher that doesn't recover |
| R34 | Repair the CI runner group | bootstrap operator | under the private-networking option only: a test job without a runner or vault access |
| R35 | Change the API origin or add a custom domain | maintainer | a new web host name or API origin |
| R36 | Change conversation or memory retention | maintainer | a retention decision |
| R37 | Retrieve an uploaded file for an audit or legal request | operator, level `user_data` | an audit or legal request for an uploaded file |

### 3.3 R01 — Deploy

Performer: automatic for `dev`; a release approver for `prod`; a maintainer follows the run. Trigger: a merge to `main` (`83-infrastructure.md` section 10.6).

1. Follow the run: `gh run watch $(gh run list --workflow deploy.yaml --branch main --limit 1 --json databaseId -q '.[0].databaseId')`. Expected: `build`, `publish`, `deploy-dev` and `test-dev` succeed, and `deploy-prod` waits for approval.
2. Confirm the `dev` tests: `gh run view <run ID> --json jobs -q '.jobs[] | select(.name | startswith("test-dev")) | .conclusion'` returns `success`. A failure leaves `dev` on the new release and `prod` untouched: fix forward, or roll `dev` back with R02.
3. Complete the release checklist (section 3.3.1) in the release ticket.
4. Approve `prod` with the approval command of section 1.5 and the ticket as comment. Expected: `deploy-prod` runs; its `postprovision` hook prints both capacity shares to the job summary. `chat_capacity_exceeded`: raise the capacity in `infra/env/prod.json` by pull request (preflight check 7 confirms the quota) or lower the per-user limit with R03, then deploy once more.
5. After `test-prod`: its `prod_smoke` suite passed, and `az rest … AppRequests | where TimeGenerated > ago(15m) | summarize requests = sum(ItemCount) by AppVersion` (the `prod` workspace, timespan `PT15M`) lists only the new commit; `git fetch --tags` shows `deploy-prod-<yyyymmdd-hhmm>` on it.
6. Watch the `availability` and `ttft` workbooks for 30 minutes. A Sev1 alert starts R07, and the approver decides within 30 minutes between fixing forward and R02.
7. When the release changes the web client, run the release sign-in check in `prod` (`10-web-client.md` section 13.4) and record it in the checklist.
8. Record the run ID, the commit, the approver and the checklist in the ticket.

Failures inside the run: `health_check_failed` or `ingestion_start_failed` → R02; `prod_test_client_present` or `prod_vault_not_empty` → R07 (the release stays deployed); `image_attestation_invalid` → R31; `migration_failed` or `sql_principal_failed` → fix forward (nothing was deployed). The first deployment of a new environment follows R22. Undo: R02. Audit record: the workflow run with its approver, the `deploy-prod-*` tag, and the deployment in the Activity Log.

#### 3.3.1 The release checklist

The approver completes it in the release ticket before approving `prod`, and links the ticket in the approval comment. "The web client changed" means `git diff --stat <current release commit of prod> <commit> -- apps/web` lists a file.

| # | Item | Evidence | Blocks the release when |
|---|---|---|---|
| 1 | `dev` deployed and `test-dev` passed | the run ID | it failed |
| 2 | Load evidence for a change to a plan SKU, the Gunicorn worker count, Durable concurrency, the Search tier or replicas, or a new model call on the chat path | the R11 run ID and its verdict table | a verdict failed or is missing |
| 3 | The web client changed: the release accessibility check (`10-web-client.md` section 11.6) passed | its results | any check failed |
| 4 | The web client changed: the release sign-in check in `dev` (`10-web-client.md` section 13.4) passed | its result | it failed |
| 5 | A new model or a price change: `pricing.model_rates` holds a rate for every deployed model in `prod` | `config show --key pricing.model_rates` output | a deployed model has no rate |
| 6 | No app setting removed or renamed, and no value outside the range the running release accepts, before the code that stops needing it is in `prod` | the reviewer's note on `infra/config/app-settings.json`, `infra/modules/compute/app-settings.bicep` and `infra/env/prod.json` | such a change ships with the code |
| 7 | After the deploy: `prod_smoke` passed; the release sign-in check in `prod` passed when item 3 applies | the run and the check | it failed (R02 or a fix forward) |

### 3.4 R02 — Rollback

Performer: a release approver. Trigger: a release that caused an incident (R07), or a failed `test-dev` or `test-prod`.

1. List good releases: `gh run list --workflow deploy.yaml --status success --limit 10 --json databaseId,headSha,createdAt`; for `prod`, the commits of the `deploy-prod-*` tags: `git fetch --tags`, then `git for-each-ref --sort=-creatordate --format "%(refname:short) %(*objectname)" "refs/tags/deploy-prod-*"`.
2. Check the schema: `git diff --name-only --diff-filter=A <good commit> <current release commit> -- db/migrations/versions | xargs -r grep -l "Contract: completes"` prints nothing. A contract revision in that range means fixing forward, because the database is never downgraded.
3. Check the settings: `git diff <good commit> <current release commit> -- infra/config/app-settings.json infra/modules/compute/app-settings.bicep infra/env/<env>.json` removes or renames no app setting and sets no value outside the range the good commit's settings models accept; otherwise the earlier code stops at start-up, so fix forward. Ops-config rows need no check: an area ignores keys it doesn't know.
4. Dispatch: `gh workflow run deploy.yaml -f environment=<dev|prod> -f image_commit=<good commit> -f rollback=true`. The `resolve` job finds the images by commit tag and verifies their attestations, `resolve-web` builds the web client at that commit, and `deploy-environment.yaml` runs `images-only`: no provision and no migration (`83-infrastructure.md` section 10.12). In `prod`, approve it (section 1.5); the release-ordering check is skipped.
5. Verify: the run's `test-dev` or `test-prod` passes, and `az rest … AppRequests | where TimeGenerated > ago(15m) | summarize sum(ItemCount) by AppVersion` (timespan `PT15M`) shows the good commit.
6. Record the rollback in the ticket and open an issue for the fix forward.

A rollback reaches any commit whose images the registry still holds; R28 keeps every image a `deploy-prod-*` tag references. Undo: R01 with the fix. Audit record: the workflow run and its approval.

### 3.5 R03 — Change an ops-config value

Performer: an operator at level `config` (`81-config.md` section 3.3). Trigger: a ticket for a value; the alerts `ops-config-invalid`, `quota-rejections` and `rate-rejections-share`; a step of another runbook that names a key.

1. Open a ticket with the environment, the key, the new JSON value and the reason.
2. Get the membership: PIM activation, or a group owner runs `uv run docchat-ops --env <env> jit grant --level config --member <operator object ID> --hours 1 --ticket <id> --reason "<reason>"` and sends the receipt to the ticket (exit 6: the operator is already a member).
3. `az logout`, then `az login --tenant <tenant ID>`.
4. Linked checks, before the change:
   - In `prod`, a key in either capacity formula (`ratelimit.chat_requests_per_minute`, `chat.context_token_budget` or any input of the chat-small charge, `12-chat-pipeline.md` section 3.7): `uv run docchat-ops --env prod chat capacity-check --set <key>=<JSON value>`. Exit 0: continue. Exit 2: raise the deployment's capacity in `infra/env/prod.json` by pull request, deploy it (R01), run the check once more and continue only on exit 0.
   - `ingestion.max_parallel_items` raised: R18 step 8 first.
   - `pricing.model_rates` or `pricing.currency`: the rates come from the Azure OpenAI pricing page for each deployment's type, region and currency.
5. `uv run docchat-ops --env <env> config show --key <key>`. Expected: the current value, default, last change time and actor; note the value for the undo.
6. `uv run docchat-ops --env <env> config set --key <key> --value '<JSON value>' --ticket <id> --reason "<reason>"`; in `prod` type `prod` when asked. Expected: exit 0 and "live within 60 seconds". Exit 2 quotes the allowed range or the broken rule; exit 6 means someone changed the row: repeat from step 5.
7. Wait 60 seconds; `config show --key <key>` shows the new value and your object ID; check the workbook of the key's area: `api.*`, `ratelimit.*` and `quota.*` on `quota`; `chat.*`, `retrieval.*` and `followups.*` on `ttft`; `pricing.*` on `budget`; `conversations.*`, `summary.*`, `memory.*` and `feedback.*` on `conversations`; `ingestion.*` on `ingestion`.
8. Revoke: `uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"`, or let PIM expire; then `az logout`.

Undo: `uv run docchat-ops --env <env> config reset --key <key> --ticket <id> --reason "<reason>"`, or `config set` with the value noted in step 5. Audit record: `ops_config.set` or `ops_config.reset`.

Changes this runbook carries:

| Change | Rule |
|---|---|
| Memory off platform-wide | `memory.enabled` `false`: extraction and memory blocks stop within 60 s; users keep their memory screens, and facts stay stored until users clear them or R06 deletes them (`13-conversations-and-memory.md` section 10.12) |
| Prices | `pricing.model_rates` and `pricing.currency` for every deployed model; afterwards the `budget` tile "Models without a price" stays empty and `prices-missing` silent for an hour; `prod` opens to users only once every deployed model has a rate |
| Quotas | `quota.*` are global: raising one raises it for everyone, and no per-user exception exists |
| Retrieval tuning | `retrieval.reranker_threshold` and `retrieval.min_confident_results`, once evaluation results exist; then watch the `ttft` tiles "Broadened-search rate" and "Time to first token p50, p95, p99" for a day |
| Summary or memory completion caps | `summary.max_completion_tokens` and `memory.max_completion_tokens` are inputs of the chat-small charge: the capacity check of step 4 applies in `prod` |
| Ingestion retention | `ingestion.run_retention_days` and `ingestion.durable_history_retention_days`; the next 02:40 UTC maintenance applies them |
| `ops-config-invalid` | the event names the area and key: `config show --key <key>` shows the stored value; set a valid value or `config reset`; the area keeps its last valid values meanwhile |
| `quota-rejections`, `rate-rejections-share` | the `quota` tiles "Rejections by reason" and "Distinct users rejected per hour, by operation": many users near a limit is a limit to raise (with the capacity check in `prod`); one user is R07 step 2's case |
| `load` values | the load workflow's `seed-config`, `stream-reset`, `s4-set1-config`, `s4-set2-config` and `s4-reset` jobs apply them (`81-config.md` section 3.9); nobody sets them by hand |

### 3.6 R04 — Per-user index override

Performer: an operator at level `config`. Trigger: a ticket to test an index against one user's live questions; `index-override-used` (daily list in `prod`).

1. Open a ticket with the user's object ID, the project ID, the index ID, the number of days (1 to 30) and the reason; get the membership as R03 steps 2 and 3.
2. `uv run docchat-ops --env <env> override set --user <oid> --project <projectId> --index <indexId> --days <n> --ticket <id> --reason "<reason>"`. Expected: exit 0 and the expiry. Exit 2: the index isn't mapped to the project, so an admin maps it in the Projects screen first; exit 4: the user hasn't signed in yet.
3. After the user's next turn: `az rest … AppDependencies | where Name == 'docchat.access.resolve_index' and tostring(Properties['docchat.access.index_source']) == 'override' | summarize n = count()` returns at least 1, and `uv run docchat-ops --env <env> override list --user <oid> --ticket <id> --reason "verify the override"` shows the row with `expired` false.
4. End it early with `uv run docchat-ops --env <env> override clear --user <oid> --project <projectId> --ticket <id> --reason "<reason>"`; otherwise it lapses at its expiry.
5. Revoke as R03 step 8.
6. On `index-override-used`: for each user, project and index in the `access` tile "Index overrides in use", `override list` and match the row to its ticket; an override without a ticket is cleared at once and starts R07.

Undo: `override clear`. Audit record: `user_index_override.set` or `user_index_override.cleared`.

### 3.7 R05 — Re-index an index

Performer: an admin (normal path) or an operator at level `config` (automation path). Trigger: a method or parameter change; a new embedding model or version (R09); a new method version (`14-ingestion.md` section 9.5).

1. Room on the search service: a rebuild into a new physical index holds two physical indexes for one logical index (Basic allows 15 indexes, S1 50). The start refuses (`409 search_index_capacity` in the admin screen, exit 2 from the command) when there is no room; answer it by deleting unused indexes (`adminDeleteIndex`) or by raising `search.sku` in `infra/env/<env>.json` through a pull request and R01.
2. Normal path: in the admin Indexes screen change the method or its parameters, then Start re-ingest (`adminStartReingest`).
3. Automation path: membership as R03 steps 2 and 3; `uv run docchat-ops --env <env> reindex start --index-id <uuid> --ticket <id> --reason "<reason>"`; in `prod` type `prod`. Expected: the run ID and the mode `same_index` or `new_physical_index`. Exit 2: the index's status doesn't allow a re-ingest, or no room (step 1); exit 4: unknown index; exit 6: rerun.
4. A faster rebuild: raise `ingestion.max_parallel_items` with R03 after R18 step 8's checks, and set the earlier value back when the run finishes.
5. Watch `rebuildProgress` on the index in the admin Indexes screen and the `ingestion` workbook; ingestion alerts follow R18.
6. To give up a rebuild: `uv run docchat-ops --env <env> ingestion abandon-rebuild --index-id <uuid> --ticket <id> --reason "<reason>"`.
7. Revoke as R03 step 8.

Undo: step 6 while the rebuild runs; a finished rebuild is undone by another re-index with the earlier method. Audit record: `index.reingest_started` (admin) or `ingestion.reindex_started` (operator).

### 3.8 R06 — User data export and deletion

Performer: an operator at level `user_data` (`13-conversations-and-memory.md` sections 10.8 to 10.11); for part B step 1, the tenant administrator (Cloud Application Administrator, section 1.1) for the app-role assignments, a group owner for the group memberships, or a User Administrator for an account deletion; for part C, also the bootstrap operator and a maintainer. Trigger: a data-subject request; a point-in-time restore.

**A. Export**

1. Open a ticket for the data-subject request with the user's object ID.
2. A group owner grants: `uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 4 --ticket <id> --reason "<reason>"` (or PIM); `az logout`; `az login --tenant <tenant ID>`.
3. `uv run docchat-ops --env <env> user export --user <oid> --out <directory on an encrypted volume> --ticket <id> --reason "<reason>"`. Expected: exit 0, the item counts and the manifest's SHA-256.
4. Deliver `subject/` through the organisation's data-request channel; keep `internal/` with the ticket; delete both packages 30 days after the ticket closes, or at once when a later legal removal (R32) covers a document whose text a package holds.
5. Revoke: `uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "work finished"`; `az logout`.

**B. Deletion**

1. Remove the user's access, one of:
   - the tenant administrator (Cloud Application Administrator) removes the user's direct DocChat assignments: `az rest --method GET --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments`, then `az rest --method DELETE --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments/<assignment ID>` for each assignment whose `resourceDisplayName` starts with `DocChat`; and a group owner of `sg-docchat-<env>-users` and `sg-docchat-<env>-admins` removes the user from both (`az ad group member remove --group <group> --member-id <oid>`);
   - or a User Administrator deletes the account (`az ad user delete --id <oid>`; Privileged Authentication Administrator when the account holds a directory role).

   A `403` from Microsoft Graph means the person lacks the role this sub-step names: hand the sub-step to a holder; nothing else changed.
2. Grant `user_data` as in A2; `az logout`; `az login --tenant <tenant ID>`.
3. `uv run docchat-ops --env <env> user delete --user <oid> --confirm <oid> --assignment-removed --ticket <id> --reason "<reason>"`; type the environment name in `prod`. Exit 5: run it once more until it exits 0.
4. Revoke as A5.
5. No sooner than 2 hours after step 1: grant once more, run `uv run docchat-ops --env <env> user verify --user <oid> --ticket <id> --reason "verify deletion"`; exit 5 means a request recreated data: repeat step 3, then this step; revoke; close the ticket.

The `user_data.deleted` row is the deletion-register entry that part C re-applies.

**C. Point-in-time restore and the deletion register**

Restores keep TTL enabled, so items past their retention period disappear from the restored stores too, and a restored store is used only after the deletion register has been applied to it (`80-data-model.md` section 3.13). Windows: SQL point-in-time restore for `sql.pitrDays` (7 days in `dev` and `load`, 14 in `prod`); Cosmos DB continuous backup for 7 days in `dev` and `load` and 30 days in `prod`.

1. Open a ticket with the incident, the stores to restore, the restore point `T` in UTC and the product owner's approval.
2. SQL, the bootstrap operator: `az sql db restore --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat --dest-name docchat-restore-<yyyymmddhhmm> --time "<T as yyyy-MM-ddTHH:mm:ss>"`; wait until `az sql db show --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-restore-<yyyymmddhhmm> --query status -o tsv` returns `Online`.
3. Close the app, the bootstrap operator: `az webapp stop --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>` and `az functionapp stop --name <DOCCHAT_INGESTION_APP_NAME> --resource-group rg-docchat-<env>`. `api-down` fires; acknowledge it with the ticket and post a service notice.
4. The register, from the live database, by an operator at level `user_data`: `umask 077`, then `uv run docchat-ops --env <env> user deletion-register --since <T as yyyy-MM-ddTHH:mm:ssZ> --ticket <id> --reason "deletion register for restore" --json > <encrypted volume>/register-<id>.json`. Expected: exit 0 and every `user_data.deleted` and `document.traces_redacted` row since `T`.
5. SQL switch, the bootstrap operator. In `prod` first remove the database's lock: `az lock list --resource-group rg-docchat-prod --query "[?contains(id, '/databases/docchat/')].id" -o tsv`, then `az lock delete --ids <lock ID>`. Then `az sql db rename --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat --new-name docchat-replaced-<yyyymmddhhmm>` and `az sql db rename --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-restore-<yyyymmddhhmm> --new-name docchat`.
6. Cosmos DB, only when the incident deleted the database `docchat` or one of its containers: the bootstrap operator restores it into the same account with Azure CLI 2.58.0 or later, the database before its containers and at most three restores at once: `az cosmosdb sql database restore --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --name docchat --restore-timestamp <T as yyyy-MM-ddTHH:mm:ssZ>`, or `az cosmosdb sql container restore --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <conversations|memories|usage> --restore-timestamp <T as yyyy-MM-ddTHH:mm:ssZ>`, never with `--disable-ttl`; wait until `az cosmosdb sql container show --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <container> --query name -o tsv` returns the name. Items changed or deleted inside a live container are recovered only by restoring the whole account into a new account, which no template connects to the app: that case goes to the product owner as a new ADR and this runbook stops here, with the app reopened by step 9.
7. Re-apply the register, the operator at level `user_data`, for each row of step 4: `user_data.deleted` → confirm with B1's Graph read that the user holds no DocChat assignment, then `uv run docchat-ops --env <env> user delete --user <subject_object_id> --confirm <subject_object_id> --assignment-removed --ticket <id> --reason "deletion register after restore"` until it exits 0; `document.traces_redacted` → `uv run docchat-ops --env <env> user redact-document --document-id <target_id> --ticket <id> --reason "deletion register after restore"` until it exits 0, and when the restored database lists that document, an admin deletes it in the Documents screen after step 9.
8. Verify: `user verify --user <oid>` exits 0 for each user (when the row is younger than 2 hours, wait until it is 2 hours old), and one more `user redact-document` per document reports `traceCount` 0. Revoke the level.
9. Reopen: `az webapp start --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>` and `az functionapp start --name <DOCCHAT_INGESTION_APP_NAME> --resource-group rg-docchat-<env>`; a maintainer then dispatches `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false`, approved in `prod`, whose provision re-applies the database's and containers' declared settings and diagnostic settings and whose migrations bring a restored schema to `head` (calls that need a newer column fail until they finish). In `prod` the bootstrap operator then runs `uv run --frozen python scripts/bootstrap/lock_prod.py` and expects exit 0.
10. After a SQL restore the ingestion ledger is at `T` while the search indexes and blobs are current: admins re-ingest every index that changed after `T` (R05).
11. The bootstrap operator deletes the replaced database once the ticket closes, at most 7 days later: `az sql db delete --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-replaced-<yyyymmddhhmm> --yes`.

Verification of part C: the deploy run's tests pass and every step 8 check exited 0. Undo: until step 11, steps 3 and 5 with the two names swapped back. Audit record: one `user_data.deleted` row per re-applied user and one `document.traces_redacted` row per document; the restore, rename, lock, stop and start events in the Activity Log.

### 3.9 R07 — Incident response

Performer: the on-call operator; a security responder joins every security incident; for step 6, a User Administrator, a group owner and the tenant administrator (section 1.1); for a restart, the bootstrap operator. Trigger: any Sev1 alert; an alert whose runbook is "Incident response" (R20); a security event; a failed `prod_checks.py`.

1. Acknowledge the alert (Sev1 within 30 minutes during the organisation's cover hours) and open an incident ticket with the alert name, the environment and the time it fired.
2. Classify by the alert and go to its step:

| Class | Alerts and events | First look | Step |
|---|---|---|---|
| Availability and capacity | `api-down`, `api-instance-unhealthy`, `api-5xx-high`, `api-5xx-elevated`, `event-loop-lag`, `sql-failures`, `api-capacity-exhausted`, `audit-write-failures`, `telemetry-silent`, `telemetry-scrub-errors`, `telemetry-dropped-attributes` | `availability`, then `capacity` | 3 |
| Chat | `chat-failed-turns`, `screening-failures`, `usage-recording-failures`, `content-filter-volume`; user reports about answers, sources or downloads | `ttft`, `openai`, `quota` | 4 |
| Access | `user-provisioning-failures`, `invalid-token-share`, `signing-key-fetch-failures`, `signing-key-age`, `request-limit-rejections` | `access`, `quota` | 5 |
| Security | `malware-detected` after R18; every alert of `log-docchat-security` that a pipeline run or ticket explains (R20); a gitleaks finding; a suspected credential exposure; an urgent access removal | the alert's operation and caller | 6 |

3. **Availability.** Read the `availability` tiles "Availability %", "5xx by operation and code", "Healthy-instance percentage" and "Problems by code", and the `capacity` tiles "API plan CPU", "Autoscale capacity and actions", "Instances per role" and "SQL retries, failures and pool"; list instances with `az webapp list-instances --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> -o table`.
   - A release started it (`az rest … AppRequests | where TimeGenerated > ago(2h) | summarize sum(ItemCount) by AppVersion, bin(TimeGenerated, 10m)`, timespan `PT2H`): R02.
   - One unhealthy instance: the bootstrap operator runs `az webapp restart --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>` (operators hold only Reader).
   - `api-capacity-exhausted` with autoscale at its maximum: raise `api.maxInstances` in `infra/env/<env>.json` by pull request and R01.
   - `sql-failures` or `audit-write-failures`: the SQL server's health and the `capacity` tile "SQL pool wait"; in `dev`, a paused serverless database resumes on the first login.
   - `telemetry-silent`: in `dev` the workspace's daily cap (`monitoring.dailyCapGb`): `az rest … _LogOperation | where Category =~ 'Ingestion' and Detail contains 'OverQuota'` (timespan `P1D`); otherwise the apps' health.
   - `telemetry-scrub-errors` or `telemetry-dropped-attributes`: open an issue naming the attribute key and fix forward (`85-observability.md` section 3.6).
   - A regional problem: Azure Service Health for the subscription and region in the Azure portal.
4. **Chat.**
   - `chat-failed-turns`: the `ttft` tile "Turns by outcome" and the `openai` tile "429s by deployment, chat path and ingestion"; throttling follows R09 step 7.
   - `screening-failures`: chat and memory fail closed while Prompt Shields is unavailable, by design, and no bypass exists; check Content Safety's service health in the environment's region and post a service notice.
   - `content-filter-volume`: the `openai` tile "Screening and filters" by project; read affected turns only under R19; a threshold change is a reviewed Bicep change to the content filter policy (`83-infrastructure.md` section 5.6).
   - Flagged sources: `az rest … AppEvents | where Name == 'source_screening_flagged' | summarize n = count() by documentId = tostring(Properties['documentId'])` (timespan `P7D`); an admin reviews each document and deletes it or uploads a corrected version.
   - `usage-recording-failures`: the `quota` tile "Usage-recording failures" and Cosmos DB throttling on the `capacity` tile "Cosmos DB request charge per operation".
   - "Too many answers in progress" reports: stream leases expire within `chat.max_turn_seconds` + 30 s (at most 250 s), so no manual step exists; reports after that point to `docchat.ratelimit.rejections` and instance health.
   - "Too many sources loading" reports and download aborts: download leases expire within 300 s; a rise of `slow_client` aborts from one user is a slow reader or a scripted client, and the limits apply per user.
   - Turns ended with `service_restarting` outside deploy windows (the `availability` tile "Citation outcomes, download aborts, stream disconnects and drained turns"): instance health and the platform's restart events.
5. **Access.**
   - `signing-key-fetch-failures` or `signing-key-age`: confirm the API reaches `login.microsoftonline.com` over its default outbound path (the `access` tile "Signing-key age and fetches"); a restart (step 3) forces a fetch at start-up; after an emergency key roll by Microsoft, tokens signed with the new key work within 300 seconds per instance without action.
   - `invalid-token-share`: the `access` tile "Token validations by outcome and reason": one reason dominating after a release is a client fault (R02); many reasons from few callers is probing (step 6).
   - `user-provisioning-failures`: the `availability` tile "Problems by code" and SQL health (step 3).
   - `request-limit-rejections`: find the users behind the `429`s with `az rest … AppRequests | where AppRoleName == 'docchat-api' and ResultCode == '429' | summarize n = sum(ItemCount) by UserAuthenticatedId, Name | top 10 by n`; one account at the line rate is a script or a compromised account (step 6), many accounts at once a misbehaving client release (step 3).
6. **Security containment and urgent access removal**, in this order (`11-api.md` section 1.4.6):
   1. A User Administrator (Privileged Authentication Administrator when the account holds a directory role, section 1.1) disables the account and revokes its sessions: `az ad user update --id <upn> --account-enabled false`, then `az rest --method POST --url https://graph.microsoft.com/v1.0/users/<oid>/revokeSignInSessions`.
   2. A group owner removes the user from `sg-docchat-<env>-users` and `sg-docchat-<env>-admins`, and the tenant administrator (Cloud Application Administrator) deletes any direct DocChat assignment, both as R06 step B1's first option.
   3. An admin removes the user's project memberships in the Projects screen; they take effect on the next request. A token issued earlier stays valid for at most 90 minutes after issue for anything else its roles allow.
   4. Review the person's recent admin changes at level `user_data`: `uv run docchat-ops --env <env> user audit-events --actor <oid> --since <UTC time of the suspected start> --ticket <id> --reason "incident review of admin changes"`; admins undo each unwanted change in the admin screens.
   5. A leaked test-user password → R08 at once; a leaked Static Web Apps token → R14; a pipeline credential, or an alert of `log-docchat-security` that nothing explains → R31; any other credential → revoke it at its issuer first, then remove it from history with `git filter-repo --replace-text <expressions file>` and a force-push coordinated by a `docchat-maintainers` administrator (gitleaks findings are never waived).
   6. `malware-detected`: the `document.malware_detected` row names the uploader as its subject; treat the uploader's account as in sub-steps 1 to 4 when the upload wasn't expected.
7. **Evidence.** Save the KQL results that show the event (identifiers and times only) to the ticket; never paste content.
8. **A user-reported error** with a reference ID: R15.
9. **Sev1 updates** go every 60 minutes to the stakeholders named in the ticket.
10. **Close** with a post-incident review within 5 business days, recording any runbook or alert change it decides.

Undo: each containment step is undone by its inverse once the review clears the account (a User Administrator runs `az ad user update --id <upn> --account-enabled true`; a group owner restores the group membership). Audit record: the incident ticket; Entra audit entries for account and group changes.

### 3.10 R08 — Test-user password rotation

Performer: a rotating operator (section 1.1), on a machine in the shared allowlist (the test vault) and in `dev`'s allowlist (the audit row). Trigger: every 30 days for both sets (the nightly `test-credential-expiry` job warns 7 days before an expiry), and at once after a suspected exposure; never while a load test runs (`83-infrastructure.md` section 2.17).

1. With an empty shared allowlist, add this machine's address first (R24 step 4).
2. `az login --tenant <tenant ID>`; `azd env refresh -e dev --no-prompt`; `export DOCCHAT_TEST_VAULT_URI=$(gh variable get DOCCHAT_TEST_VAULT_URI --env dev-tests)`; `export DOCCHAT_TEST_USER_DOMAIN=$(gh variable get DOCCHAT_TEST_USER_DOMAIN --env dev-tests)` (both variables exist only in `dev-tests` and `load-tests`).
3. Get the `config` membership in `dev` (R03 steps 2 and 3).
4. `uv run docchat-ops --env dev testusers rotate --set all --ticket <id> --reason "30-day test password rotation"`. Expected: 25 aliases (4 of the `dev` set, 21 of the `load` set) with new expiry dates, exit 0.
5. Exit 5: run the same command once more; it completes the remaining aliases. Exit 3: check the Password Administrator assignment on the administrative unit, the `config` membership and both allowlists. Exit 2 with `load_run_in_progress`: run `--set dev` now and `--set load` after the load run's teardown.
6. Verify: `uv run docchat-ops --env dev testusers check --set all` exits 0 and lists every expiry about 30 days ahead; `gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke` passes. The `load` set is proven by the next load run's token step.
7. Revoke the `config` membership (R03 step 8), and remove the address added in step 1 (R24 step 4 with the earlier list).

Undo: none; the earlier passwords are gone. Audit record: the `dev` row `test_users.rotated`; the Graph password changes in Entra's audit log; the secret versions in the vault.

### 3.11 R09 — Model version upgrade

Performer: a maintainer; for step 4, an operator at level `config` in `dev` (section 1.3). Trigger: a `Model retirement: <deployment> <model> <version>` issue from the nightly `model-watch` job; the preflight's `model_retires_soon`; the alerts `ttft-p95`, `chat-openai-throttling`, `prices-missing`, `openai-first-chunk-latency` and `openai-call-latency`; a planned upgrade.

1. Choose the replacement version and its deployment type (`GlobalStandard` unless the product owner decides otherwise) from Microsoft Learn's model availability and retirement pages for the environment's region; a `model_retires_soon` finding is resolved within its 6-month window.
2. A new model name first needs its row in the model capability table, a code release (`12-chat-pipeline.md` section 10.7).
3. Edit the deployment's object (model, version, type, capacity) in `openAi.deployments` or `openAi.embeddingModels` of `infra/env/dev.json` (`81-config.md` sections 4.3 and 4.4) and open the pull request. Its `preflight-whatif` job (`83-infrastructure.md` section 10.5) runs the preflight against `dev`, model checks included, and the what-if: expect the job to pass and its summary to show the deployment change; a failed model check fails the job and names the deployment, and the version is chosen once more (step 1).
4. Chat and chat-small deployments, after the `dev` deploy: an operator holding level `config` in `dev` (section 1.3), whose group's Cognitive Services User at `rg-docchat-dev` permits the Azure OpenAI and Content Safety calls, runs the deployed model check, the tokenizer check, the chat-small latency check and the Prompt Shields probes (`12-chat-pipeline.md` section 10.7) with `uv run docchat-ops --env dev chat model-check --ticket <id> --reason "<reason>"` from an address in `dev`'s operator allowlist and expects exit 0; exit 2 names each failed call, latency set or probe. `prod` runs the same pinned model names and versions, whose availability the preflight checks against each deployment's type in the region (step 9). A tokenizer difference above 1% means setting the encoding the model's documentation names (`DOCCHAT_OPENAI_CHAT_ENCODING` or `DOCCHAT_OPENAI_SMALL_ENCODING`) by pull request. When a capacity changed, run `chat capacity-check` (R03 step 4).
5. Before users reach the new model, set its price with R03 (`pricing.model_rates`); `prices-missing` stays silent for an hour.
6. Embedding deployments are never changed in place (`NoAutoUpgrade`): add a new `openAi.embeddingModels` entry with a new key, deploy, re-index every logical index that uses the old pair into a new physical index (R05), and remove the old entry only when no physical index names its deployments.
7. Throttling (`chat-openai-throttling`): compare the deployment's capacity with the `openai` tile "Tokens by deployment and kind"; raise the capacity in its object, or lower `chat.answer_max_completion_tokens` with R03, and run `chat capacity-check` after either change.
8. Latency (`openai-first-chunk-latency`, `openai-call-latency`) with no model change in flight: compare the `openai` tiles "First-chunk latency p95 per deployment" and "Mean duration of successful calls by deployment and call" with the deployment's capacity and the region's service health; a threshold that doesn't fit the chosen models (`alerts.ttftP95Ms`, `alerts.openAiFirstChunkP95Ms`, `alerts.openAiCallMeanMs`) changes in `infra/env/<env>.json` by pull request.
9. `prod`: the same change in `infra/env/prod.json`, deployed with R01, its price set before the approval (release checklist item 5).
10. Close the issue when the next nightly `model-watch` passes.

Undo: the pull request reverted and deployed. Audit record: the pull requests, the deploy runs, the `model_deployments.checked` row of step 4 and the `ops_config.set` row of the price.

### 3.12 R10 — Zone-redundancy conversions

Performer: a maintainer, in a change window, with the bootstrap operator for the Azure CLI conversions (Owner; operators hold only Reader). Trigger: a decision to make a resource zone-redundant. Which settings are derived and which are parameters is in `83-infrastructure.md` section 2.20.

**Storage, LRS to ZRS**

1. `az storage account show --name <account> --resource-group rg-docchat-<env> --query "{sku: sku.name, kind: kind}"` shows `Standard_LRS` and `StorageV2`.
2. `az storage account migration start --account-name <account> --resource-group rg-docchat-<env> --sku Standard_ZRS --no-wait`.
3. Daily: `az storage account migration show --account-name <account> --resource-group rg-docchat-<env> --migration-name default --query properties.migrationStatus` until `Complete` (the conversion usually starts within 72 hours and has no completion time guarantee).
4. Align the template: `main.bicep` derives the SKU from the environment name (`Standard_ZRS` in `prod` and `load`, `Standard_LRS` in `dev`), so `prod` and `load` already declare it; for `dev`, the derivation changes by pull request. What-if then shows no change.

**Cosmos DB, the home region through a temporary region**

1. Choose a temporary region with availability zones and Cosmos DB capacity, and add it zone-redundant: `az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<home> failoverPriority=0 isZoneRedundant=false --locations regionName=<temporary> failoverPriority=1 isZoneRedundant=true`.
2. Wait for `az cosmosdb show --name <account> --resource-group rg-docchat-<env> --query provisioningState` = `Succeeded`, and confirm the private endpoint lists the new regional endpoint: `az network private-endpoint show --name <Cosmos DB endpoint> --resource-group rg-docchat-<env> --query "customDnsConfigs[].fqdn"`.
3. Fail over: `az cosmosdb failover-priority-change --name <account> --resource-group rg-docchat-<env> --failover-policies <temporary>=0 <home>=1`.
4. Remove the home region: `az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<temporary> failoverPriority=0 isZoneRedundant=true`.
5. Add the home region zone-redundant: `az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<temporary> failoverPriority=0 isZoneRedundant=true --locations regionName=<home> failoverPriority=1 isZoneRedundant=true`.
6. Fail back: `az cosmosdb failover-priority-change --name <account> --resource-group rg-docchat-<env> --failover-policies <home>=0 <temporary>=1`.
7. Remove the temporary region: `az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<home> failoverPriority=0 isZoneRedundant=true`.
8. Confirm the declaration (`isZoneRedundant: true`) and a what-if with no change; run the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`).

**App Service plan or SQL database**

1. Plan only: `az appservice plan show --name asp-docchat-api-<env> --resource-group rg-docchat-<env> --query properties.maximumNumberOfZones`. Greater than 1: step 2. Exactly 1: the plan sits on a scale unit without zones, can't switch in place, and a deploy with `api.zoneRedundant: true` would fail; such a plan becomes zone-redundant only by redeployment into a new resource group, which the `rg-docchat-<env>` layout doesn't provide, so `api.zoneRedundant` stays `false`, the reading goes into the runbook record, and the product owner decides through a new ADR (ADR 0102). SQL needs no check.
2. Set `api.zoneRedundant` or `sql.zoneRedundant` (read only in `prod`) to `true` in `infra/env/prod.json` by pull request and deploy with R01; both switch on in place, and with `api.zoneRedundant` the plan's instances and every autoscale minimum become at least 2.

Undo: the same procedures in reverse, by the same performers. Audit record: the Activity Log entries of each conversion and the pull requests.

### 3.13 R11 — Run the load test

Performer: a maintainer starts the run; the tenant administrator (Cloud Application Administrator or Application Administrator, section 1.1), who is also in `sg-docchat-operators` for step 3's reads, clears the access gate; a Conditional Access Administrator in `sg-docchat-operators` makes the policy changes of steps 5 and 11; the bootstrap operator performs step 13's resource deletes and purges. Trigger: a change that release checklist item 2 names, or a capacity question. The workflow, scenarios and verdicts are in `84-testing.md` section 7; the environment's lifecycle is `83-infrastructure.md` section 10.14.

1. Pick a release: `gh run list --workflow deploy.yaml --status success --limit 5 --json databaseId,headSha`.
2. Start: `gh workflow run load-test.yaml -f deploy_run_id=<run ID> -f scenarios=all`. Expected: `manifest`, `create` and then `access-gate` waiting for review.
3. At the gate, from a machine in the shared allowlist: `az login --tenant <tenant ID>`; `azd env new load --subscription <subscription ID> --location <region> --no-prompt`; `azd env set AZURE_RESOURCE_GROUP rg-docchat-load`; `azd env refresh -e load --no-prompt`.
4. `uv run python scripts/identity_post_provision.py --environment load --resource-token <DOCCHAT_RESOURCE_TOKEN> --users-group <sg-docchat-load-users object ID> --admins-group <sg-docchat-load-admins object ID> --test-users-group <sg-docchat-load-test-users object ID> --test-admins-group <sg-docchat-load-test-admins object ID>`; expect exit 0.
5. The Conditional Access Administrator adds the run's API and test-client applications to the excluded applications of the Conditional Access policy `DocChat test users: non-production APIs only`: `az rest --method PATCH --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies/<policy ID> --body @<file with the full excludeApplications list>`.
6. `uv run docchat-ops --env load testusers check --set load --min-hours 24 --vault-uri $(gh variable get DOCCHAT_TEST_VAULT_URI --env load-tests)`; expect exit 0 (every `load` password stays valid for 24 hours). Exit 5: reject the gate (section 1.5, `-f state=rejected -f comment="load passwords expire"`), let the run's `teardown` finish, rotate the `load` set (R08 with `--set load`) and start a new run.
7. Approve the gate (section 1.5, environment `load-access`, comment "identities ready").
8. Follow the run: `gh run watch <run ID>`.
9. Read the verdict table in the run summary; download the evidence: `gh run download <run ID> --pattern "load-results-*"`.
10. After `teardown`: `az resource list --resource-group rg-docchat-load --query "length(@)"` returns 0; `az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-load-<DOCCHAT_RESOURCE_TOKEN>-web')"` answers `404`, and the same for `-api` and `-test`; `az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<app ID>')"` answers `404` for `WEB_CLIENT_ID`, `API_CLIENT_ID` and `TEST_CLIENT_ID` of step 3; `az rest --method GET --url https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.application --query "value[?uniqueName && starts_with(uniqueName,'docchat-load-<DOCCHAT_RESOURCE_TOKEN>-')] | length(@)"` returns 0, and `az rest --method GET --url https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.servicePrincipal --query "value[?appId=='<app ID>'] | length(@)"` returns 0 for each of the three app IDs.
11. The Conditional Access Administrator removes the run's applications from the policy's exclusions (step 5 with the earlier list); the maintainer attaches the verdicts to the release ticket they support.
12. An `inconclusive` verdict: read its cause and measurements in the scenario's verdict file under `load-results/`, and start the follow-up run that `84-testing.md` section 7 prescribes for that cause with `gh workflow run load-test.yaml -f deploy_run_id=<run ID> -f scenarios=<scenario>` and the inputs it names; a cause that section sends to the product owner goes there with the measurements, and a cause that names a `load` capacity is raised in `infra/env/load.json` by pull request before the next run.
13. Teardown failed (`teardown_incomplete` or `graph_delete_failed`): `gh run rerun <run ID> --failed`. When resources or registrations remain: the bootstrap operator deletes each remaining resource (`az resource delete --ids <resource ID>`); the tenant administrator reads each remaining registration (`az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-load-<DOCCHAT_RESOURCE_TOKEN>-<web|api|test>')" --query "{id: id, appId: appId}"`), deletes its service principal (`az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<appId>')" --query id -o tsv`, then `az rest --method DELETE --url https://graph.microsoft.com/v1.0/servicePrincipals/<service principal ID>` and `az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<service principal ID>`; a `404` means it is gone), then the application (`az rest --method DELETE --url https://graph.microsoft.com/v1.0/applications/<object ID>` and `az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<object ID>`). The bootstrap operator purges a soft-deleted Key Vault or Cognitive Services account (`az keyvault purge --name <vault>`, `az cognitiveservices account purge --name <account> --resource-group rg-docchat-load --location <region>`, both under Owner) only when a name collision blocks a run. Then repeat step 10.

Undo: the teardown. Audit record: the workflow run, the gate approval and the Activity Log; the load jobs' `ops_config.set` rows in the `load` database, gone with it.

### 3.14 R12 — Defender acceptance check

Performer: an operator. Trigger: the first provision of an environment with malware scanning on (`prod` by default); any re-creation of the data storage account; `uploads-scan-gate-closed`. `dev` scans only while the deployed malware-scanning test runs: the stage-2 acceptance performer sets `storage.malwareScanning` to `true` with one pull request and back to `false` with a second as soon as the suite ends (`81-config.md` section 4.2), after R23's scanner case.

1. Confirm the setting: `az rest --method GET --url "https://management.azure.com<data storage account ID>/providers/Microsoft.Security/defenderForStorageSettings/current?api-version=2025-06-01" --query "properties.malwareScanning"` shows `onUpload.isEnabled: true`.
2. Wait for the first canary: the 02:40 UTC maintenance writes one daily, and the first sweep writes one when none exists; Defender can take up to 24 hours on a new account.
3. `az rest … AppEvents | where Name == 'scanner_canary_result' | top 5 by TimeGenerated desc | project TimeGenerated, result = tostring(Properties['result']), minutes = toint(Properties['minutes'])` (timespan `P2D`) shows `clean`.
4. Defender's own record: `az rest … StorageMalwareScanningResults | where TimeGenerated > ago(2d) | take 20` (timespan `P2D`) lists the canary blob.
5. An admin's upload preflight in the Documents screen succeeds (no `malware_scanning_not_ready`).
6. Log the acceptance in `docs/runbooks/rehearsals.md` with the canary's time; for a first provision, also in the R22 record.
7. No result after 24 hours: re-read step 1's `operationStatus`, check that the scanner's resource-instance rule exists (`az storage account show --name <account> --query networkRuleSet.resourceAccessRules`), and open a Microsoft support case; uploads stay closed.
8. Monthly cap: when the month's uploads reach `storage.malwareScanCapGbPerMonth` (`az rest … AppMetrics | where TimeGenerated > startofmonth(now()) and Name == 'docchat.uploads.size' | summarize gb = sum(Sum) / 1e9`, timespan `P31D`), Defender stops scanning until the month ends: new versions fail closed as `scan_unavailable`, the canary gets no clean result and `uploads-scan-gate-closed` fires. Raise the cap in `infra/env/<env>.json` by pull request and R01, then admins re-upload the versions that failed as `scan_unavailable`.
9. Uploads closed after acceptance (`503 malware_scanning_not_ready`, `uploads-scan-gate-closed`): read step 3's query, and at level `config` (R03 steps 2 and 3) `uv run docchat-ops --env <env> ingestion status`, which lists the last 5 canaries: `unavailable` points at Defender (step 7) or the monthly cap (step 8); `failed` at the scanner's error value. The gate opens by itself at the first clean canary.
10. Turning `prod` scanning off is a product owner decision recorded in an ADR. When it is made, the bootstrap operator removes the data account's `docchat-protect` lock (`az lock delete --ids <lock ID>` from `az lock list --resource-group rg-docchat-prod`), the pull request setting `storage.malwareScanning` to `false` deploys with R01, and `uv run --frozen python scripts/bootstrap/lock_prod.py` exits 0 in the same session.

Undo: none needed; the check changes nothing. Audit record: the rehearsal log entry; the canary rows in `app.scan_canaries`.

### 3.15 R13 — First admins and test identities

Performer: per part, as stated. Trigger: a new tenant (parts A and C), the first provision of an environment or a re-created registration (part B), `role-not-assigned` and access requests (part D). On a new tenant the parts run in the order A, then B for `dev`, then C, then B for `prod`, because part B in `dev` names the test groups and part C's policy names `dev`'s applications.

**A. Test identities, once per tenant** (a User Administrator for users and groups, a Privileged Role Administrator for the administrative unit and the role assignment; section 1.1. Microsoft Learn names Privileged Role Administrator for creating an administrative unit and adding members to it, confirmed 2026-10-25. A `403` from Microsoft Graph means the step's role is missing: hand the step to a holder; earlier steps stay in place.)

1. The Privileged Role Administrator: `az rest --method POST --url https://graph.microsoft.com/v1.0/directory/administrativeUnits --body '{"displayName": "DocChat test users", "description": "Automated test accounts for DocChat non-production environments"}'`; note the unit ID.
2. For each alias of `testUserAliases`, the User Administrator: `az ad user create --display-name "DocChat test <alias>" --user-principal-name <name>@<initial domain> --password <random, discarded> --force-change-password-next-sign-in false`, where `<name>` is `docchat-test-<alias>` for the `dev` set and `docchat-<alias>` for the `load` set (`docchat-load-01` to `docchat-load-20`, `docchat-load-admin`); then the Privileged Role Administrator adds the user to the unit: `az rest --method POST --url "https://graph.microsoft.com/v1.0/directory/administrativeUnits/<unit ID>/members/\$ref" --body '{"@odata.id": "https://graph.microsoft.com/v1.0/users/<user ID>"}'`.
3. The User Administrator creates the groups `sg-docchat-dev-test-users`, `sg-docchat-dev-test-admins`, `sg-docchat-dev-test-norole`, `sg-docchat-load-test-users` and `sg-docchat-load-test-admins` with `az ad group create --display-name <name> --mail-nickname <name>`, and add the members `84-testing.md` section 6 assigns to each with `az ad group member add --group <name> --member-id <user ID>`. Part B reads their object IDs with `az ad group show --group <name> --query id -o tsv`.
4. The Privileged Role Administrator gives each rotating operator Password Administrator on the unit only: `az rest --method POST --url https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments --body '{"principalId": "<operator object ID>", "roleDefinitionId": "966707d0-3269-4727-9be2-8c3a10f19b9d", "directoryScopeId": "/administrativeUnits/<unit ID>"}'`.

**B. Every environment, after its first provision** (the tenant administrator: Cloud Application Administrator or Application Administrator, section 1.1, in `sg-docchat-operators` for step 1's refresh; `83-infrastructure.md` section 7.5)

1. `az login --tenant <tenant ID>`; `azd env refresh -e <env> --no-prompt`.
2. `uv run python scripts/identity_post_provision.py --environment <env> --resource-token <DOCCHAT_RESOURCE_TOKEN> --users-group <sg-docchat-<env>-users object ID> --admins-group <sg-docchat-<env>-admins object ID> [--admin <object ID> …]`; in `dev` add `--test-users-group <sg-docchat-dev-test-users object ID> --test-admins-group <sg-docchat-dev-test-admins object ID> --test-norole-group <sg-docchat-dev-test-norole object ID>`. The script refuses the test groups in `prod` and `--test-norole-group` outside `dev` with exit 1 before any Graph call; a `load` run's test groups are assigned at its gate (R11 step 4).
3. Exit 2 naming the no-role step means Microsoft Graph refused the default-access assignment of `sg-docchat-dev-test-norole` on the API after every other step was done: a repository administrator records the skip with `gh variable set DOCCHAT_TEST_NOROLE_SKIP_REASON --env dev-tests --body graph_default_access_refused`. Any other non-zero exit: fix the cause it names and run the script once more.
4. Verify: a first admin signs in and `GET /v1/me` returns `isAdmin: true`; a second run of the script changes nothing.

**C. Conditional Access and first passwords, once per tenant after part B in `dev`** (Conditional Access Administrator, then a rotating operator)

1. Add the five test groups to `conditions.users.excludeGroups` of every tenant policy that requires multi-factor authentication for all users (`az rest --method PATCH --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies/<policy ID> --body @<file>`).
2. Create the policy `DocChat test users: non-production APIs only` with `az rest --method POST --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies --body @<file>`, whose body sets `displayName`, `state: "enabled"`, `conditions.users.includeGroups` = the five groups, `conditions.applications.includeApplications: ["All"]`, `conditions.applications.excludeApplications` = the `dev` API and `dev` test-client application IDs, `conditions.clientAppTypes: ["all"]` (Microsoft Graph refuses the request with `400` without it) and `grantControls: {"operator": "OR", "builtInControls": ["block"]}`.
3. Set both sets' first passwords with R08 (`--set all`), then `gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke` passes (its `norole` check reports skipped when part B step 3 recorded a reason).

**D. Day-to-day access**

1. People get access by joining `sg-docchat-<env>-users` or `sg-docchat-<env>-admins`, whose owners (the people in `groupOwners`, section 1.1) add them with `az ad group member add --group <group> --member-id <object ID>`; project access is granted in the admin Projects screen (`adminAddProjectMember`), and new users join every default project at their first sign-in.
2. `role-not-assigned`: the `access` tile "Access denials by kind and operation" names the operations; a person who should have access is added to `sg-docchat-<env>-users`; a sudden rise after a registration change means part B runs once more for that environment.

Undo: a User Administrator deletes part A's users and groups with `az ad user delete` and `az ad group delete`, and a Privileged Role Administrator the unit and the role assignments; part B and C changes are undone by the inverse Graph calls, by the role each part names. Audit record: Entra's audit log for every user, group, role and policy change; the script's printed summary and correlation ID in the ticket.

### 3.16 R14 — Static Web Apps deployment token reset

Performer: the bootstrap operator for the reset (operators hold only Reader, which lacks `Microsoft.Web/staticSites/resetapikey/action`), then a maintainer for the redeploy. Trigger: every 90 days, and at once on suspected exposure (`83-infrastructure.md` section 10.10).

1. The bootstrap operator: `az staticwebapp secrets reset-api-key --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env>`. The old token stops working at once; the site keeps serving its content.
2. A maintainer redeploys the current release, whose web step reads the new token: `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=true` (the same images, the web client built at that commit, no provision); `prod` waits for its approval (section 1.5).
3. Verify: the run's web step succeeded (`deploy_web.py` exit 0), and `curl -s -o /dev/null -w "%{http_code}" https://<web host>/config.json` prints `200`.
4. Record the reset date in the ticket that tracks the token.

Undo: none; the old token can't be restored. Audit record: the reset by the bootstrap operator and every later `Microsoft.Web/staticSites/listSecrets/action` by the deploy identity, both in `log-docchat-security` for 365 days.

### 3.17 R15 — Find the trace behind a user's reference ID

Performer: an operator (Log Analytics Reader on the environment workspace). Trigger: a user's error report quoting a reference ID.

1. Take the 32-character reference ID from the user's message: the problem's `correlationId`, which is the trace ID the browser sent (`85-observability.md` section 2.11).
2. `uv run docchat-ops --env <env> trace show <reference ID> --ticket <id> --reason "user error report"`. Expected: the trace's records in time order with outcomes, error codes, session and turn IDs and the user's object ID; exit 4 means no record within 30 days.
3. Read the session and turn IDs and the error code; the code's meaning is in the error catalogue of the file that owns the operation (`11-api.md` section 2).
4. When the answer needs the content of the turn: R19.
5. A sign-in failure's reference ID is Entra ID's correlation ID: `az rest … SigninLogs | where CorrelationId == '<id>'` against `log-docchat-security` (timespan `P30D`).

Undo: none. Audit record: none; the command reads metadata only.

### 3.18 R16 — Repository and GitHub settings

Performer: an owner of the GitHub organization. Trigger: once, and after any re-creation of the repository.

1. Create the teams `docchat-maintainers`, `docchat-backend`, `docchat-frontend`, `docchat-platform`, `docchat-security`, `docchat-release-approvers` and `docchat-tenant-admins`: `gh api --method POST /orgs/<org>/teams -f name=<team> -f privacy=closed`.
2. Grant access: `gh api --method PUT /orgs/<org>/teams/<team>/repos/<org>/<repo> -f permission=<admin|maintain|push|pull>` (`docchat-maintainers` admin; `docchat-backend`, `docchat-frontend`, `docchat-platform` and `docchat-security` push; the two reviewer teams pull).
3. Apply the ruleset: `gh api --method POST /repos/<org>/<repo>/rulesets --input .github/rulesets/main.json`.
4. Security features: `gh api --method PATCH /repos/<org>/<repo> --input <file setting security_and_analysis.secret_scanning and secret_scanning_push_protection to enabled>`; `gh api --method PUT /repos/<org>/<repo>/vulnerability-alerts`; `gh api --method PUT /repos/<org>/<repo>/automated-security-fixes`; `gh api --method PUT /repos/<org>/<repo>/private-vulnerability-reporting`.
5. Actions: `gh api --method PUT /repos/<org>/<repo>/actions/permissions -F enabled=true -f allowed_actions=selected`; `gh api --method PUT /repos/<org>/<repo>/actions/permissions/selected-actions --input <file listing the pinned actions of 83-infrastructure.md section 10.3>`; `gh api --method PUT /repos/<org>/<repo>/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false`.
6. Keep CodeQL on its workflow: `gh api --method PATCH /repos/<org>/<repo>/code-scanning/default-setup -f state=not-configured`.
7. The GitHub environments come from the bootstrap's `github` step (`configure_github.py`). Read the two protected ones back: `gh api /repos/<org>/<repo>/environments/prod --jq '{reviewers: [.protection_rules[] | select(.type == "required_reviewers") | .reviewers[].reviewer.slug], prevent_self_review: ([.protection_rules[] | select(.type == "required_reviewers") | .prevent_self_review] | first), can_admins_bypass}'` returns `["docchat-release-approvers"]`, `true` and `false`; the same for `load-access` returns `["docchat-tenant-admins"]`, `true` and `false`. A difference: the bootstrap operator runs `uv run --frozen python scripts/bootstrap/bootstrap.py --step github`, which stops with `github_protection_mismatch` until both read back correctly.
8. Verify: a pull request with a failing test can't merge.
9. Keep `.github/security/approvers.yaml` equal to the `docchat-security` team now and after every team change: `gh api /orgs/<org>/teams/docchat-security/members --jq ".[].login"`; a difference is corrected by a pull request, which only `docchat-security` can approve.

Undo: the inverse API calls. Audit record: the organization's audit log.

### 3.19 R17 — Dependency, base-image and tool updates

Performer: a maintainer. Trigger: weekly, monthly and quarterly as listed; a high or critical finding.

1. Weekly: review Dependabot pull requests and merge when CI passes; a major version, `openai`, an `azure-*` package, MSAL or Fluent UI also needs the full browser suite and a `dev` `smoke` run before it reaches `prod`.
2. The Functions host base image (Dependabot on `apps/ingestion/Dockerfile`) merges only after the ingestion integration suite and the image's offline self-check pass.
3. Parsing model pins change with `python apps/ingestion/image/fetch_models.py --update <repository> <revision>`, which records the new SHA-256 values; CI builds the image and runs the offline self-check; a new method version or new parsing models then reach indexes through R05 (`14-ingestion.md` section 19.3). A pull request that changes the pinned `pikepdf`, or the qpdf it bundles, also raises `RENDITION_RULES_VERSION` by 1 and adds its entry to the rendition rules version unit test (`14-ingestion.md` section 14.8 item 6); without both, CI fails, and after the deploy sweep duty 12 re-renders every served version under the new library.
4. Monthly: `pre-commit autoupdate --freeze` in a pull request.
5. Quarterly: read the latest k6 release's `k6-v<version>-checksums.txt` and update `tests/load/k6.lock.json` (version, URL, SHA-256 and member path); `scripts/ci/check_k6_lock.py` passes. The load generator's image needs no update of its own: `docchat-load aci-params` reads it from the ingestion Dockerfile, which step 2 keeps current.
6. Quarterly: compare every `br/public:avm/res/<path>:<version>` pin in `infra/` with the Azure Verified Modules index, read each newer version's changelog for API-version and default changes, and move the pins in one pull request that passes the infrastructure gate (`bicep build`, `bicep build-params` for the three environments, lint, PSRule, `check_bicep_modules.py` and the what-if against `dev`). A version that deploys a preview API version isn't adopted; one that moves diagnostic settings to a GA API version is adopted at once.
7. Quarterly, and as soon as a stable PSRule for Azure release ships: move `ps-rule.yaml`'s `requires` range for `PSRule.Rules.Azure` and its baseline together to the new release and its newest GA baseline in one pull request that passes the gate, resolving each newly failing built-in rule as `83-infrastructure.md` section 13.4 says.
8. Quarterly: compare the tool versions of `83-infrastructure.md` section 9.4 and section 1.5 here with their release pages and move them by pull request.
9. A new Python minor version is adopted only through an ADR, once both Azure Functions and `unstructured` support it.
10. A high or critical finding with no patched version fails every build until it is gone. Remove it in this order: merge the base-image update that drops it; remove the package from the image's final stage when the app doesn't need it at run time; move to another pinned base image by pull request; when none of these works, the product owner decides through a new ADR, because no waiver exists for high or critical findings. Two such decisions exist. ADR 0154's `unfixed-os-packages` entries (one per image) pass an OS-package finding only while the scan reports no fix for it: before their `review_by` date (at most 90 days after `added`; the gate warns 14 days ahead and fails after the date), a `docchat-security` member checks for a newer base digest that drops the findings, merges it if one exists, and renews the entries by pull request with `added` set to that day and a new `review_by` at most 90 days later; a finding that gains a fix fails the next build or the next nightly rescan and is fixed as this step says. ADR 0153's is the exception for Trivy's DS002 on the ingestion image: before its `review_by` date in `.github/security/exceptions.yaml` (the gate warns 14 days ahead and fails after the date), a `docchat-security` member confirms that the ingestion host still needs UID 0 for the parsing child's user switch and renews the entry by pull request with `added` set to that day and a new `review_by` at most 184 days later, or removes it once the image no longer raises DS002. An npm finding whose package has a patched release that the range of the dependency bringing it excludes isn't such a case: force the patched release with an exact `overrides` entry (`01-repository.md` section 8.1) in a pull request whose CI passes, and remove the entry as `01-repository.md` section 8.3 item 9 says.

Undo: a revert pull request. Audit record: the pull requests and their CI runs.

### 3.20 R18 — Ingestion failures and stuck runs

Performer: an operator at level `config` for the `ingestion` and `reindex` commands (`14-ingestion.md` section 19); a maintainer for the workflow modes. Trigger: `ingestion-item-failures`, `ingestion-queue-age`, `ingestion-dispatcher-stale`, `ingestion-poison-messages`, `malware-detected`, `ingestion-throttling`, `ingestion-run-failed`, `rerender-failed`.

1. `ingestion-item-failures`, `ingestion-run-failed`: the `ingestion` tiles "Item outcomes by code" and "Latest failed items"; apply the remedy of `14-ingestion.md` for each outcome code (a re-upload, a method parameter, a scan failure). DOCX and PPTX items failed with `convert_timeout`, `convert_memory_exceeded` or `convert_failed`:
   - read the `ingestion` tile "Office conversions": the outcomes of `docchat.ingestion.convert.duration` by content type;
   - a few failures on large or damaged files: the index's admin splits the file, or saves it as PDF in Word or PowerPoint, and uploads it as a new version (the version's `failureMessage` says which);
   - failures of most conversions since a release: the converter of that image is at fault, so roll back with R02 and open an issue naming the release; a re-ingest of the failed documents (R05) after the fix converts them, because no converted PDF was stored for a failed version.
2. `ingestion-queue-age`, `ingestion-dispatcher-stale`: read the `ingestion` tile "Dispatcher heartbeat age and poison messages" and, at level `config` (R03 steps 2 and 3), `uv run docchat-ops --env <env> ingestion status`: the dispatcher's state row, queued and running counts, the oldest queued item's age, the open runs. The keep-alive restarts a stale dispatcher by itself. When the heartbeat stays stale beyond the watchdog limits for more than 10 minutes, the function app itself is unhealthy: a maintainer dispatches `gh workflow run deploy-environment.yaml -f environment=<env> -f mode=restart-ingestion` (approved in `prod`, section 1.5). Expected: the job summary names the app, the restart time and the `ingestion_worker_started` event's time; `ingestion_restart_failed` means nothing changed and the mode runs once more; `ingestion_start_failed` means no worker started on the release within 10 minutes (R07). Items whose leases expire requeue at the next sweep.
3. To act on one item or run at once:
   - `uv run docchat-ops --env <env> ingestion requeue-item --item-id <uuid> --ticket <id> --reason "<reason>"` for a `running` item with an expired lease;
   - `uv run docchat-ops --env <env> ingestion fail-item --item-id <uuid> --ticket <id> --reason "<reason>"` for an item that must stop;
   - `uv run docchat-ops --env <env> ingestion cancel-run --run-id <uuid> --ticket <id> --reason "<reason>"` for a run to stop;
   - `uv run docchat-ops --env <env> ingestion abandon-rebuild --index-id <uuid> --ticket <id> --reason "<reason>"` for a rebuild to give up;
   - `uv run docchat-ops --env <env> ingestion set-deployments --physical-index-id <uuid> --query <name> --ingestion <name> --ticket <id> --reason "<reason>"` to move a physical index to a renamed deployment pair of the same model and version.
4. `ingestion-poison-messages`: confirm with `ingestion status` that the runs the messages name have their items queued or finished, then `uv run docchat-ops --env <env> ingestion drain-poison --ticket <id> --reason "<reason>"`; type `prod` in `prod`. Expected: `deletedCount`.
5. `malware-detected`: the source blob was deleted at detection and stays soft-deleted for 14 days; the `document.malware_detected` row names the version and, as its subject, the uploader; no ingestion output exists for that version. Then R07 step 6.
6. `ingestion-throttling`: compare the ingestion deployments' tokens per minute with the `openai` tile "429s by deployment, chat path and ingestion"; lower `ingestion.max_parallel_items` (R03) or raise the deployment's capacity (R09).
7. Uploads paused by the scan gate: R12 step 9.
8. Raising `ingestion.max_parallel_items` to `p`: check the ingestion embedding and table-summary deployments' tokens per minute (a document can draw `p` × 2 embedding requests of up to 60,000 tokens and `p` × 4 summary calls at once), Document Intelligence's 15 analyze transactions per second, and that `ingestion.maxBurst` allows `ceil(p / 2)` EP2 instances. In `prod` above 2, first raise `ingestion.alwaysReady` in `infra/env/prod.json` to `ceil(p / 2)` by pull request and R01; then set the value with R03.
9. A failed `rerender` item (`rerender-failed` for `render_failed` and `render_too_large`; for every code, the tile "Latest failed items", operation `rerender`; `14-ingestion.md` section 6.15): its chunk set stays served with the renditions of the earlier `RENDITION_RULES_VERSION`, every page of them verified under those rules, and its citations answer as before.
   - `storage_unavailable`, `activity_timeout` or `processing_interrupted`: nothing to do; sweep duty 12 queues the chunk set again 24 hours after the failure. A second failure with the same code goes to the storage or dispatcher steps of this runbook.
   - `render_failed` or `render_too_large`: the deployed rules refuse a page of this PDF, and sweep duty 12 doesn't queue it again while the item's row exists (180 days). Read the pull request that raised `RENDITION_RULES_VERSION`, whose description names what the change leaves out. When the change closed a way for a rendition to act on a reader's viewer, ask the index's admin, through the ticket, to delete the document or upload a changed file as a new version, within 2 working days; otherwise the earlier renditions stay until the admin replaces the document.
   - `source_changed` or `source_missing`: the stored PDF doesn't match the chunk set or is missing, and the renditions stored under the lower rules version stay served; ask the index's admin, through the ticket, to delete the document and upload the file again.
   - Any other code: apply the remedy of `14-ingestion.md` section 17.2 for it.
10. Last resort, when the dispatcher stays stale after step 2's restart: R33.
11. Revoke the level (R03 step 8).

Undo: per command, none (each records its change); a raised parallelism goes back with R03. Audit record: the `ingestion.*` rows of section 2.8.6; the workflow runs of step 2.

### 3.21 R19 — Read a user's conversation; access review

Performer: an operator at level `read` (`13-conversations-and-memory.md` section 10.12, procedures 1, 2 and 8 to 10). Trigger: a ticket that needs a turn's content; the monthly review; `summary-failures`, `extraction-failures`, `abandoned-turns`, `client-id-conflicts`, `human-conversation-access`, `docchat-alert-jit-grant`, `docchat-alert-jit-self-grant`, `docchat-alert-jit-overdue`.

**Reading a conversation**

1. From R15, take the user's object ID (`enduser.id`) and the session and turn IDs; open a ticket with them, the reason and a duration of at most 4 hours.
2. A group owner grants: `uv run docchat-ops --env <env> jit grant --level read --member <operator object ID> --hours <1 to 4> --ticket <id> --reason "<reason>"` (or PIM); `az logout`; `az login --tenant <tenant ID>`.
3. `uv run docchat-ops --env <env> user read-conversation --user <oid> --session <sessionId> --turn <turnId> --out <directory on an encrypted volume> --ticket <id> --reason "<reason>"`. Expected: exit 0 and the counts; `human-conversation-access` fires, and its acknowledgement names the ticket.
4. Record findings in the ticket by reference only; delete the output directory; the owner revokes (`uv run docchat-ops --env <env> jit revoke --level read --member <operator object ID> --ticket <id> --reason "work finished"`) or PIM expires; `az logout`.

**Monthly access review**

5. Run the human-access query over the last 31 days: `az rest … CDBDataPlaneRequests | where DatabaseName == 'docchat' and AadPrincipalId != '<DOCCHAT_API_IDENTITY_PRINCIPAL_ID>' | summarize requests = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by AadPrincipalId` (timespan `P31D`).
6. Match every principal and window to a ticket and to a grant in PIM history or in the Entra `AuditLogs` of `log-docchat-security`: `az rest … AuditLogs | where OperationName in ('Add member to group', 'Remove member from group') | project TimeGenerated, OperationName, group = tostring(TargetResources[1].displayName), member = tostring(TargetResources[0].id)` (that workspace, timespan `P31D`).
7. `uv run docchat-ops --env <env> jit list --ticket <id> --reason "monthly access review"` shows the three groups empty.
8. Any unmatched access, `docchat-alert-jit-self-grant`, or a `docchat-alert-jit-overdue` without a ticket starts R07 (security), and R31 when it can't be explained. A `docchat-alert-jit-grant` is acknowledged with its ticket.

**Conversation alerts and reports**

9. `summary-failures`, `extraction-failures`: filter `docchat.summary.rebuild` or `docchat.memory.extract` spans by `docchat.error_code`: `model_unavailable` → R09 step 7 (throttling, quota, deployment health); `content_filtered` → expected for some sessions, no action unless it exceeds 5% of calls; `invalid_output` dominated by finish reason `length` → raise `summary.max_completion_tokens` or `memory.max_completion_tokens` with R03 (capacity check in `prod`), or lower the reasoning effort; `store_conflict` → Cosmos DB throttling and `docchat.conversations.store_conflicts`.
10. "An answer is still being written" reports and `abandoned-turns`: the block ends at the turn's `deadlineAt` (receipt + `chat.max_turn_seconds` + 30 s, 210 s by default); past that, check Cosmos DB availability, `docchat.conversations.store_conflicts` and the `conversations` tile "Abandoned, superseded and duplicate turns".
11. `client-id-conflicts`: the same tile; a single client repeating IDs is a client fault (R02 when a release caused it).
12. Feedback review: trends from the `conversations` tiles "Thumbs up and down per project per day, with the down rate" and "Negative feedback by category" (metadata only); the content of a turn behind a negative rating is read with steps 1 to 4 and a ticket.

Undo: none. Audit record: `conversation.read` with `turnIds`, `ticket` and `level`; the Entra membership changes; the review's results in its ticket.

### 3.22 R20 — Alert response index and runbook maps

Performer: the on-call operator. Every alert's description names its runbook (`85-observability.md` section 6); this runbook is the index.

| Alerts | Runbook | First action |
|---|---|---|
| `api-down`, `api-instance-unhealthy`, `api-5xx-high`, `api-5xx-elevated`, `user-provisioning-failures`, `telemetry-silent`, `audit-write-failures`, `event-loop-lag`, `sql-failures`, `telemetry-scrub-errors`, `telemetry-dropped-attributes`, `api-capacity-exhausted` | R07 | `availability` and `capacity`; the last release; R02 when a release caused it |
| `chat-failed-turns`, `screening-failures`, `usage-recording-failures`, `content-filter-volume` | R07 | `ttft`, `openai` and `quota` |
| `signing-key-fetch-failures`, `signing-key-age`, `invalid-token-share`, `request-limit-rejections` | R07 | `access` and `quota` |
| `ttft-p95`, `chat-openai-throttling`, `prices-missing`, `openai-first-chunk-latency`, `openai-call-latency` | R09 | `ttft` and `openai`; capacity, prices and the region's service health |
| `quota-rejections`, `rate-rejections-share`, `ops-config-invalid` | R03 | `quota` or `access` tile "Ops-config health"; the key involved |
| `index-override-used` | R04 | each override's ticket |
| `uploads-scan-gate-closed` | R12 | steps 7 to 9 |
| `ingestion-item-failures`, `ingestion-queue-age`, `ingestion-dispatcher-stale`, `ingestion-poison-messages`, `ingestion-throttling`, `ingestion-run-failed`, `rerender-failed` | R18 | `ingestion` |
| `malware-detected` | R18, then R07 | the uploader and the version |
| `summary-failures`, `extraction-failures`, `abandoned-turns`, `client-id-conflicts`, `human-conversation-access` | R19 | `conversations` |
| `role-not-assigned` | R13 | part D |
| `docchat-alert-jit-grant`, `docchat-alert-jit-overdue` | R19 | the grant's ticket |
| `docchat-alert-jit-self-grant` | R19, then R31 | the initiator and the member |
| `docchat-alert-federated-credentials`, `docchat-alert-governance`, `docchat-alert-role-assignments`, `docchat-alert-cosmos-data-access`, `docchat-alert-diagnostic-settings`, `docchat-alert-app-registrations`, `docchat-alert-registry-writes` | R07 when a pipeline run or a ticket explains the operation; R31 otherwise | the operation and its caller; write the run ID or ticket in the acknowledgement |
| `docchat-alert-key-access` | R07 when a provision run of that environment explains it (under fallback 1 every provision's `listKeys()` on the host account raises it); R31 otherwise | match the caller (that environment's deploy identity) and the time to a run of `deploy.yaml`, or of `load-test.yaml` for `load` |
| `actual-80`, `actual-100` | R29 | cost by `docchat-component` tag |
| `actual-50` | none | compare spend with the day of the month on the `budget` workbook |

Infrastructure procedures (`83-infrastructure.md`) and their runbooks:

| Procedure | Runbook |
|---|---|
| Bootstrap a tenant and subscription | R21 |
| First provision of an environment, with the post-provision steps | R22 |
| Re-tighten role-assignment conditions | R23 |
| Change the operator allowlist or the ops mailboxes | R24 |
| Change a model version, type or capacity; respond to `model_retires_soon` | R09 |
| Raise ingestion parallelism above 2 in `prod` | R18 step 8 |
| Rotate the Static Web Apps deployment token | R14 |
| Rotate test-user passwords | R08 |
| Rotate the host-account key (fallback 1 only) | R25 |
| Convert storage LRS to ZRS; make Cosmos DB zone-redundant; switch App Service or SQL zone redundancy | R10 |
| Clean up after a failed load teardown | R11 step 13 |
| Remove a stray runner rule | R26 |
| Content-share fallback activation | R27 |
| Prune the registry | R28 |
| Budget alert at 80% or 100% | R29 |
| Decommission an environment | R30 |
| Suspected pipeline compromise | R31 |
| Reset the Durable task hub (`deploy-environment.yaml` `mode: reset-task-hub`) | R33 |
| Restart the ingestion app (`mode: restart-ingestion`) | R18 step 2; R25 step 4 under fallback 1 |
| Repair the CI runner group | R34 |
| Turn `prod` scanning off | R12 step 10 |

Procedures of the other files and their runbooks:

| Procedure | Owner | Runbook |
|---|---|---|
| Read a user's conversation; monthly access review; summary and extraction failures; unfinished answers; feedback review | `13-conversations-and-memory.md` section 10.12, items 1, 2, 8, 9, 10 | R19 |
| Export and delete a user's data; re-apply deletions after a restore | the same, items 3, 4, 5 | R06 |
| Change retention | the same, item 6 | R36 |
| Turn memory off platform-wide | the same, item 7 | R03 |
| Legal removal of a document's text | the same, item 11 | R32 |
| Stuck-run recovery, poison messages, parallelism, malware detected | `14-ingestion.md` sections 19.2 and 19.3 | R18 |
| Search capacity; a new method version | `14-ingestion.md` section 19.3, items 2 and 6 | R05, R17 |
| Uploads paused by the scan gate | `14-ingestion.md` section 19.3, item 4 | R12 step 9 |
| Ingestion retention | `14-ingestion.md` section 19.3, item 5 | R03 |
| Task-hub reset | `14-ingestion.md` section 19.2 | R33 |
| Chat and quota values, prices, retrieval tuning, the capacity check | `12-chat-pipeline.md` section 3.7; `81-config.md` section 3 | R03 |
| Model, deployment, reasoning effort or prompt-cache change; deployed model and tokenizer checks | `12-chat-pipeline.md` section 10.7 | R09 |
| Prompt Shields unavailable; content-filter volume; flagged sources; stream, download and drain reports | `12-chat-pipeline.md` | R07 step 4 |
| Post-provision consent and first admins; day-to-day access | `83-infrastructure.md` section 7.5 | R13 |
| Urgent access removal; signing-key alerts | `11-api.md` | R07 steps 5 and 6 |
| Web rollback; changing the API origin; a custom domain | `10-web-client.md` section 2.3 | R02, R35 |
| Release accessibility check; release sign-in check | `10-web-client.md` sections 11.6 and 13.4 | R01 (release checklist) |

### 3.23 R21 — Bootstrap a tenant and subscription

Performer: the bootstrap operator: a person holding Owner on every subscription in use and the Entra roles Privileged Role Administrator, Groups Administrator and Security Administrator, signed in to `gh` as an owner of the GitHub organization (`83-infrastructure.md` section 11.1). Trigger: a new tenant or subscription set; any change to `scripts/bootstrap/bootstrap.config.json`; a step to repair. Prerequisites: Bicep CLI 0.47.16; `bootstrap.config.json` filled in (tenant, subscriptions, region, GitHub organization and repository, `ciPrivateNetworking` (`false` by default: standard GitHub-hosted test runners and the test vault's transient runner rule; `true` only for an organization on GitHub Team or GitHub Enterprise Cloud, with `ciNetworkLocation`), `operators` with every person of section 1.1's rule "Who runs Azure reads" other than the approvers, at least two approvers per environment, at least two `groupOwners`, `securityAlertEmails`, `testUserAliases`); `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported in the shell, holding the operator's own address for the duration of the run.

1. `az login --tenant <tenant ID>`; `gh auth status` shows the account, and `gh api /orgs/<organization>/memberships/<login> --jq .role` returns `admin`.
2. `uv run --frozen python scripts/bootstrap/bootstrap.py`. Expected: the steps `prerequisites` to `checklist` each report success (the `ci-runner` step reports `skipped` unless `ciPrivateNetworking` is `true`), and the script exits 0. A failed step stops with its code (`missing_prerequisite`, `approver_is_operator`, `unknown_action`, `oidc_subject_shared`, `oidc_subject_customised`, `graph_permission_failed`, `policy_definition_missing`, `github_protection_mismatch`, and under the private-networking option `ci_region_unsupported`, `ci_private_networking_unavailable`, `ci_runner_mismatch`; `83-infrastructure.md` section 10.18) and leaves the earlier steps in place: fix the cause, then `uv run --frozen python scripts/bootstrap/bootstrap.py --step <name>` and each later step. After `ci_private_networking_unavailable` (the organization's plan offers no larger runners), set `ciPrivateNetworking` to `false` by a reviewed pull request and re-run the `shared`, `assignments`, `github` and `ci-runner` steps. The `shared` step rewrites the test vault's `ipRules` and so removes a live test-job runner rule: on a re-run, start it while no test job reads the vault.
3. Merge the `infra/env/*.json` changes that the `config` step prints through a pull request.
4. Create the test identities (R13 part A); their Conditional Access policy and first passwords follow `dev`'s first provision (R22, then R13 part C), because the policy names `dev`'s applications.
5. Verify: `az role assignment list --assignee <dev deploy identity client ID> --all -o table` shows the rights of `83-infrastructure.md` section 6.8; `az role assignment list --assignee <id-docchat-citest client ID> --all -o table` shows exactly Key Vault Secrets User on the test vault, DocChat Test Vault Firewall Editor on the test vault while `ciPrivateNetworking` is `false` (none under the private-networking option), Log Analytics Reader and DocChat Load Test Operator at `rg-docchat-load` and, once `dev`'s first provision has been followed by R22 step 3, Log Analytics Reader on `log-docchat-dev` (`83-infrastructure.md` section 6.8); `gh api /repos/<organization>/<repository>/environments --jq ".environments[].name"` lists `dev`, `prod`, `load`, `load-access`, `dev-tests` and `load-tests`; `az policy assignment list --resource-group rg-docchat-prod --query "[].name" -o tsv` lists the `docchat-deny-*` assignments of `83-infrastructure.md` section 12.1, `docchat-deny-store-deletion` included.
6. Remove the operator's address from the shared allowlist (R24 step 4 with the list it held before) unless password rotation (R08) follows in the same session.
7. Under fallback 1, a rerun of the `policies` step redeploys `docchat-deny-storage-shared-key` without the host account in `notScopes`: repeat R27 step 1 for each environment under fallback 1 in the same session.
8. Log the run (date, operator handle, commit) in `docs/runbooks/rehearsals.md`.

Undo: R30 per environment. Audit record: the subscription Activity Log and Entra `AuditLogs` in `log-docchat-security`; the GitHub organization's audit log.

### 3.24 R22 — First provision of an environment

Performer: a maintainer starts it; the post-provision steps need a tenant administrator (Cloud Application Administrator or Application Administrator), the bootstrap operator and, in `prod`, a Security Administrator and a Privileged Role Administrator. Trigger: a new environment. Prerequisites: R21 done; the environment's `infra/env/<env>.json` merged; `DOCCHAT_OPERATOR_IP_ALLOWLIST` and `DOCCHAT_OPS_ALERT_EMAILS` set on the GitHub environment (`prod`'s preflight refuses an empty mailbox list). The steps follow `83-infrastructure.md` sections 11.4 and 11.5 in table order, and the record keeps each step's output.

1. Provision and deploy: `dev` by a merge to `main` (R01); `prod` with `gh workflow run deploy.yaml -f environment=prod -f image_commit=<release commit on main> -f rollback=false`, approved by `docchat-release-approvers`. Expected: the run succeeds; every API call answers `401` or `403` until step 2. The first provision raises `docchat-alert-role-assignments` once; acknowledge it with this runbook's ticket.
2. Post-provision step 1, consent and first admins: R13 part B; after `dev`'s, R13 part C once per tenant.
3. Post-provision step 2, `dev` and `prod`: in `dev` the bootstrap operator first runs `uv run --frozen python scripts/bootstrap/bootstrap.py --step assignments`, which now finds `log-docchat-dev` and gives `id-docchat-citest` Log Analytics Reader on it (`az role assignment list --assignee <id-docchat-citest client ID> --scope <log-docchat-dev resource ID> -o table` shows it; when `prod` was tightened before, R23 for `prod` follows, because the re-run resets its conditions). Then the bootstrap operator runs `uv run --frozen python scripts/bootstrap/tighten_conditions.py --env <env>` and expects exit 0 (`principal_ids_missing`: the provision didn't finish; `condition_update_failed`: run it once more), then commits the configuration diff it prints. In `dev` without a scanner it prints that it must run once more before `dev` scanning turns on (R23).
4. Post-provision step 3, `prod`: the bootstrap operator runs `uv run --frozen python scripts/bootstrap/lock_prod.py` and expects exit 0 (exit 2 with `prod_not_protected`: rerun `bootstrap.py --step policies`, then this step); `az lock list --resource-group rg-docchat-prod -o table` shows exactly six `docchat-protect` locks, and `az policy assignment show --name docchat-deny-store-deletion --resource-group rg-docchat-prod` succeeds. Paste the output into the record.
5. Post-provision step 4, `prod`: a Security Administrator creates `docchat-prod-audit`: `az rest --method PUT --url "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings/docchat-prod-audit?api-version=2017-04-01" --body @<file naming the prod workspace ID and the AuditLogs category>`.
6. Post-provision step 5, scanning on (`prod` by default): R12.
7. Post-provision step 6, once after the bootstrap: the first test-user passwords (R13 part C step 3).
8. Post-provision step 7, only when `cosmosGroupDataAccess` is `false`: the bootstrap operator assigns DocChat Cosmos Data Access Granter to each approver: `az role assignment create --assignee-object-id <approver object ID> --assignee-principal-type User --role f6b96867-d30e-4380-9a16-fbac2370fcd8 --scope <Cosmos DB account ID>`.
9. Post-provision step 8, tenants with Entra ID P2 or ID Governance: a Privileged Role Administrator configures PIM for Groups on the three just-in-time groups: eligible assignments for the people in `operators` only, never an approver, whose activation needs a group owner's approval, a justification, a ticket number and MFA, with a maximum activation of 4 hours (`13-conversations-and-memory.md` section 10.1).
10. Post-provision step 9, only under fallback 1: R27 step 1.
11. Prices: R03 with `pricing.model_rates` and `pricing.currency` for every deployed model; `prices-missing` stays silent for an hour before users are assigned.
12. Verify: the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`) passes; log the first provision and every step's output in `docs/runbooks/rehearsals.md`.

Undo: R30. Audit record: the workflow runs, the Activity Log, Entra's audit log, the rehearsal log entry.

### 3.25 R23 — Re-tighten role-assignment conditions

Performer: the bootstrap operator. Trigger: a workload identity of `dev` or `prod` re-created; `dev` scanning about to turn on after a tightening that ran before the subscription's Defender scanner existed. `load` is never tightened.

1. Confirm the trigger: `az identity show --name id-docchat-api-<env> --resource-group rg-docchat-<env> --query principalId -o tsv` (and the same for `id-docchat-ingest-<env>`) differs from the principal ID in the deploy identity's condition: `az role assignment list --assignee <deploy identity principal ID> --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --query "[?condition!=null].condition" -o tsv`. A provision that re-created an identity stops at that identity's role assignments, because the condition still names the old principal; that failure is the expected signal. Scanner case (`dev`): `az resource show --ids /subscriptions/<subscription ID>/providers/Microsoft.Security/datascanners/StorageDataScanner --query identity.principalId -o tsv` returns a principal the condition doesn't name; run step 2 before the pull request that sets `storage.malwareScanning` to `true` in `infra/env/dev.json` merges.
2. `uv run --frozen python scripts/bootstrap/tighten_conditions.py --env <env>`; expect exit 0 and commit the configuration diff it prints (`principal_ids_missing`: finish the provision first; `condition_update_failed`: run it once more).
3. Provision: `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false`, approved in `prod`.
4. Verify: the run succeeds and `az role assignment list --all --assignee <new principal ID> --query "[].roleDefinitionName" -o tsv` lists the identity's roles of `83-infrastructure.md` section 6.5 or 6.6.

Undo: none; the old principal is gone. Audit record: the condition update in the Activity Log; `docchat-alert-role-assignments` fires and its acknowledgement names the ticket.

### 3.26 R24 — Change the operator allowlist

Performer: an operator with admin rights on the repository's GitHub environments for an environment list; the bootstrap operator for the shared list. Trigger: an operator's address changes, joins or leaves; the ops mailboxes change.

1. Open a ticket with the change: single public IPv4 addresses, at most 20 per list.
2. Environment list: `gh variable set DOCCHAT_OPERATOR_IP_ALLOWLIST --env <env> --body "<comma-separated addresses>"`; for `dev` also `gh variable set DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST --body "<the same list>"` (the pull-request what-if reads it). Ops mailboxes: `gh variable set DOCCHAT_OPS_ALERT_EMAILS --env <env> --body "<comma-separated shared mailboxes>"`.
3. Apply it with a full deploy: `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false`, approved in `prod`. The preflight refuses a malformed list with `allowlist_invalid` (exit 2) before anything changes.
4. Shared list (registry and test vault), the bootstrap operator, while no test job reads the vault (the step rewrites the vault's `ipRules` and so removes a live read window): `export DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST="<comma-separated addresses, or empty>"`, then `uv run --frozen python scripts/bootstrap/bootstrap.py --step shared`.
5. Verify: `az sql server firewall-rule list --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --query "[].name" -o tsv` shows one `operator-<n>` per address and no `ci-runner-*` rule; `az cosmosdb show --name <DOCCHAT_COSMOS_ACCOUNT_NAME> --resource-group rg-docchat-<env> --query "ipRules[].ipAddressOrRange" -o tsv` lists the addresses; for the shared list, `az keyvault network-rule list --name kv-docchat-tst-<sharedToken> --query "ipRules[].value" -o tsv` lists exactly its addresses while it holds any, and `az keyvault show --name kv-docchat-tst-<sharedToken> --query properties.publicNetworkAccess -o tsv` returns `Enabled` by default whatever the list holds (only its addresses and a test job's read window reach the vault), and `Disabled` while the list is empty under the private-networking option.
6. Record the change in the ticket.

Undo: the earlier list, set and applied the same way. Audit record: the variable change in the GitHub audit log; the firewall and network-rule writes in the Activity Log (registry writes raise `docchat-alert-registry-writes`).

### 3.27 R25 — Rotate the host-account key (content-share fallback 1 only)

Performer: the bootstrap operator; a maintainer for the deploy. Trigger: every 90 days under fallback 1, and at once on suspected exposure; in a change window with no ingestion work running, because the host can't reach its content share between steps 2 and 4.

1. Confirm fallback 1 applies: `az storage account show --name <host account> --resource-group rg-docchat-<env> --query allowSharedKeyAccess` returns `true`. Confirm nothing runs: `az rest … AppMetrics | where Name == 'docchat.ingestion.items.running' | top 1 by TimeGenerated desc | project running = Sum` (timespan `PT15M`) returns 0.
2. `az storage account keys renew --account-name <host account> --resource-group rg-docchat-<env> --key key1`. `docchat-alert-key-access` fires; acknowledge it with the ticket.
3. Write the secret `ingestion-content-share-connection` from the new key with a full deploy: `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false`, approved in `prod`.
4. Restart the ingestion app so the platform reads the Key Vault reference once more: `gh workflow run deploy-environment.yaml -f environment=<env> -f mode=restart-ingestion`, approved in `prod`; the job waits for the new worker's `ingestion_worker_started`.
5. Verify: within 10 minutes `az rest … AppTraces | where AppRoleName == 'docchat-ingestion' | summarize n = count()` (timespan `PT15M`) returns a row; no host record mentions Azure Files or the content share; the next ingestion run completes.
6. Record the rotation date in the ticket that tracks the key.

Undo: none; the old key stops working when renewed. Audit record: `regenerateKey` and the provision's `listKeys` in the Activity Log and `log-docchat-security`.

### 3.28 R26 — Remove a stray runner rule

Performer: the bootstrap operator, from a machine in the allowlist of the resource concerned (the environment's for SQL, the shared one for the registry and the test vault). Trigger: a job failure with `rule_close_failed`; step 1 finding a rule during any review. Prerequisites for the registry and the test vault: `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported, holding the shared allowlist the bootstrap's `shared` step last applied, and for the vault `DOCCHAT_TEST_VAULT_URI` (`gh variable get DOCCHAT_TEST_VAULT_URI --env dev-tests`). No CI job holds the shared allowlist (`83-infrastructure.md` section 10.4), so no job removes a registry or test-vault address other than its own runner's: this runbook is the only sweep of both.

1. Find stray rules. SQL: `az sql server firewall-rule list --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --query "[?starts_with(name,'ci-runner-')].name" -o tsv`. Registry: `az acr network-rule list --name <registry> --query "ipRules[].ipAddressOrRange" -o tsv`; any address outside the shared allowlist is stray. Test vault: `gh variable get DOCCHAT_CI_PRIVATE_NETWORKING`, then `az keyvault network-rule list --name kv-docchat-tst-<sharedToken> --query "ipRules[].value" -o tsv`; by default (`false`) an address outside the shared allowlist while no test job runs is a read window its job didn't remove; under the private-networking option (`true`) the vault never takes a runner rule, so such an address is a change no pipeline makes → R31.
2. Confirm that no job holding a rule on that target runs, because a sweep also removes a live rule: `gh api "/repos/<organization>/<repository>/actions/runs?status=in_progress&per_page=100" --jq '.workflow_runs[].path'` lists none of the workflows that take a rule on it (`83-infrastructure.md` section 4.6); otherwise wait for them and repeat step 1.
3. `uv run --frozen python scripts/deploy/firewall.py sweep --target <sql|registry|vault> --env <env>` (`--env dev` for the shared registry and vault). Expected: exit 0, each removed rule printed. A warning that `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` is not set means nothing was removed: export it and run the step again. On SQL, without this step the next job that uses SQL removes the rule in its own sweep; a registry or test-vault leftover stays until this step, or until the next run of the bootstrap's `shared` step rewrites both IP rule lists.
4. Verify by repeating step 1: nothing stray remains.
5. Find who added each rule, in `rg-docchat-<env>` for SQL and in `rg-docchat-shared` for the registry and the test vault: `az monitor activity-log list --resource-group <rg-docchat-<env> or rg-docchat-shared> --offset 7d --query "[?operationName.value=='Microsoft.Sql/servers/firewallRules/write' || operationName.value=='Microsoft.ContainerRegistry/registries/write' || operationName.value=='Microsoft.KeyVault/vaults/write'].{caller: caller, time: eventTimestamp}" -o table`. SQL or registry: a caller other than a pipeline identity → R31. Test vault: a caller other than `id-docchat-citest` or the bootstrap operator (whose `shared` step writes the vault's rules) → R31.

Undo: none needed. Audit record: the rule deletes in the Activity Log.

### 3.29 R27 — Content-share fallback activation

Performer: a maintainer (pull request) and the bootstrap operator (policy). Trigger: the content-share spike fails any of its criteria 1 to 6 (`83-infrastructure.md` section 2.7) and ADR 0103 records fallback 1.

1. Before any deploy that carries the fallback, the bootstrap operator excludes the host account from the shared-key assignment: `az policy assignment update --name docchat-deny-storage-shared-key --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --not-scopes <host account resource ID>`. `docchat-alert-governance` fires; acknowledge it with the ticket.
2. A pull request with the fallback-1 changes of `83-infrastructure.md` section 2.7 and `81-config.md` section 2.8: the host account's shared-key exception, its `file` private endpoint and the zone `privatelink.file.core.windows.net`, the share `func-docchat-ingest-<env>`, the secret `ingestion-content-share-connection` written from `listKeys()`, the three app settings, `keyVaultReferenceIdentity`, both outbound-routing switches, the NAT gateway `ng-docchat-<env>` with `pip-docchat-<env>-nat` on `snet-ingest-integration`, and the ingestion identity's Key Vault Secrets User on that secret; PSRule and the what-if pass.
3. Deploy it (R01), then run the spike's criteria 1 to 6 against the app. When criterion 4, the scale-out, still fails with fallback 1 deployed, stop: fallback 2 goes to the product owner as a new ADR.
4. Verify: `az network vnet subnet show --resource-group rg-docchat-<env> --vnet-name <virtual network> --name snet-ingest-integration --query natGateway.id -o tsv` names `ng-docchat-<env>`; the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`) passes and an ingestion run completes.
5. From then on R25 rotates the host key every 90 days, every provision of the environment raises `docchat-alert-key-access` (R20), and every rerun of the bootstrap's `policies` step is followed by step 1 (R21 step 7).

Undo: a revert pull request deployed, then `az policy assignment update --name docchat-deny-storage-shared-key --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --not-scopes ""`. Audit record: the policy assignment change and the deployment in the Activity Log.

### 3.30 R28 — Prune the registry

Performer: the bootstrap operator (Owner on `rg-docchat-shared`), from a machine in the shared allowlist (R24 step 4). Trigger: quarterly.

1. The commits `prod` ran: `git fetch --tags`; `git for-each-ref --format "%(*objectname)" "refs/tags/deploy-prod-*"` (the set P).
2. For each repository `docchat/api`, `docchat/ingestion` and `docchat/model-stub`: `az acr manifest list-metadata --registry <registry> --name docchat/<image> --query "[?lastUpdateTime < '<UTC date 400 days ago>'].{digest: digest, tags: tags}" -o json`.
3. For each listed manifest none of whose tags is in P: `az acr repository delete --name <registry> --image docchat/<image>@<digest> --yes`.
4. Verify: each commit in P still resolves (`az acr manifest show-metadata --registry <registry> --name docchat/<image>:<commit> --query digest -o tsv`) and `az acr show-usage --name <registry> -o table` shows storage below the Premium tier's 500 GiB.
5. Record the number of images deleted in the ticket, and remove the address added for this run.

Undo: none; an image of a commit outside P comes back only by building that commit once more. Audit record: the registry's `ContainerRegistryRepositoryEvents` in `log-docchat-security`.

### 3.31 R29 — Budget alert at 80% or 100%

Performer: an operator (Reader on the environment group). Trigger: `actual-80` or `actual-100` for `budget-docchat-<env>`.

1. Cost by component this month: `az rest --method post --url "https://management.azure.com/subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env>/providers/Microsoft.CostManagement/query?api-version=2026-06-01" --body "{\"type\": \"ActualCost\", \"timeframe\": \"MonthToDate\", \"dataset\": {\"granularity\": \"None\", \"aggregation\": {\"totalCost\": {\"name\": \"Cost\", \"function\": \"Sum\"}}, \"grouping\": [{\"type\": \"TagKey\", \"name\": \"docchat-component\"}]}}"`.
2. A forgotten load environment: `az group show --name rg-docchat-load --query tags` shows `docchat-load-run` and `az resource list --resource-group rg-docchat-load --query "length(@)"` returns more than 0; when its run has ended, R11 step 13 (the nightly guard removes it otherwise).
3. Model spend: the `budget` tiles "Estimated model cost per day by deployment" and "Tokens per day by deployment and kind" and the `quota` workbook. One user or project driving the spike: lower `quota.tokens_per_day` with R03, knowing that it applies to every user; broad growth: step 4.
4. Expected growth: raise `budget.amount` in `infra/env/<env>.json` by pull request and R01.
5. Record the cause and the decision in the ticket.

Undo: none needed. Audit record: the ticket; any R03 or pull-request change it led to.

### 3.32 R30 — Decommission an environment

Performer: the bootstrap operator (Owner) for every Azure step; the tenant administrator (Cloud Application Administrator or Application Administrator, section 1.1) for step 2; in `prod`, a Security Administrator for step 6. Trigger: the product owner's decision, recorded in the ticket; for `prod` also the organisation's retention decision for its conversations and documents, with R06 exports first when required.

1. `prod`: remove the six locks and the delete-deny assignment, which would otherwise refuse the deletion: `az lock list --resource-group rg-docchat-prod --query "[].id" -o tsv`, then `az lock delete --ids <lock ID>` for each; then `az policy assignment delete --name docchat-deny-store-deletion --resource-group rg-docchat-prod`. `docchat-alert-governance` fires for each; acknowledge with the ticket.
2. The tenant administrator deletes the registrations and their service principals permanently. For `web`, `api` and `test`: read the application (`az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-<env>-<DOCCHAT_RESOURCE_TOKEN>-<web|api|test>')" --query "{id: id, appId: appId}"`) and the service principal (`az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<appId>')" --query id -o tsv`); delete the service principal (`az rest --method DELETE --url https://graph.microsoft.com/v1.0/servicePrincipals/<service principal ID>`, then `az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<service principal ID>`), then the application (`az rest --method DELETE --url https://graph.microsoft.com/v1.0/applications/<object ID>` and `az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<object ID>`). A `404` on a service principal means it is gone.
3. Delete the resource group: `az group delete --name rg-docchat-<env> --yes` (in `prod` only after step 1; otherwise it answers `403`).
4. Purge the soft-deleted accounts that would block re-creation: each Cognitive Services account (`az cognitiveservices account purge --name <account> --resource-group rg-docchat-<env> --location <region>`) and, outside `prod`, the Key Vault (`az keyvault purge --name <DOCCHAT_KEY_VAULT_NAME>`); `prod`'s purge protection keeps its vault for 90 days.
5. Remove the pipeline's access: `az identity federated-credential delete --name github-environment-<env> --identity-name id-docchat-deploy-<env> --resource-group rg-docchat-identity --yes`, and for `dev` and `load` also `az identity federated-credential delete --name github-environment-<env>-tests --identity-name id-docchat-citest --resource-group rg-docchat-identity --yes`; the deploy identity's subscription-scope assignments (`az role assignment list --assignee <deploy identity principal ID> --scope /subscriptions/<subscription ID> --query "[].id" -o tsv`, then `az role assignment delete --ids <ID>` for each); the GitHub environments (`gh api --method DELETE /repos/<organization>/<repository>/environments/<env>`, and for `dev` and `load` the same for `<env>-tests`).
6. `prod`: the Security Administrator deletes `docchat-prod-audit`: `az rest --method DELETE --url "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings/docchat-prod-audit?api-version=2017-04-01"`.
7. Verify: `az group exists --name rg-docchat-<env>` returns `false`; step 2's reads answer `404`; record the decommission in the ticket.

Undo: R21's steps for the environment, then R22. Audit record: the Activity Log, Entra's audit log and the GitHub audit log, all kept 365 days in `log-docchat-security` or by GitHub.

### 3.33 R31 — Suspected pipeline compromise

Performer: the bootstrap operator with a member of `docchat-security`. Trigger: a security alert that no pipeline run or ticket explains; a credential in a workflow log; an unreviewed change on `main`; `image_attestation_invalid`.

1. Contain: stop every workflow (`gh api --method PUT /repos/<organization>/<repository>/actions/permissions -F enabled=false`) and delete the federated credentials of each implicated identity (`az identity federated-credential delete --name <credential> --identity-name <identity> --resource-group rg-docchat-identity --yes`: `github-environment-<env>` of the deploy identities, `github-pull-request` of `id-docchat-pr`, `github-environment-dev-tests` and `github-environment-load-tests` of `id-docchat-citest`).
2. Investigate in `log-docchat-security`: `az rest … AzureActivity | where Caller in ('<pipeline identity principal IDs>') | summarize n = count() by OperationNameValue, ResourceGroup, bin(TimeGenerated, 1h)` (that workspace, timespan `P30D`); the security alerts' history; Entra `AuditLogs` for registration and group changes; the GitHub organization's audit log for the repository.
3. Remove what doesn't belong: Cosmos DB data-plane assignments the templates didn't create (`az cosmosdb sql role assignment list --account-name <account> --resource-group rg-docchat-<env> -o table`, then `az cosmosdb sql role assignment delete --account-name <account> --resource-group rg-docchat-<env> --role-assignment-id <ID> --yes`); role assignments outside `83-infrastructure.md` section 6 (`az role assignment list --resource-group rg-docchat-<env> -o table`, then `az role assignment delete --ids <ID>`); unexpected federated credentials, registration credentials and redirect URIs. In `prod`, an item under one of the six locked resources is deleted only after removing that one lock (`az lock delete --ids <lock ID>` from `az lock list --resource-group rg-docchat-prod`); then `uv run --frozen python scripts/bootstrap/lock_prod.py` runs in the same session and exits 0.
4. Rotate what the pipeline could reach: the Static Web Apps token (R14 step 1 now, its redeploy in step 6), the test-user passwords (R08), and the host key under fallback 1 (R25).
5. Restore the pipeline: `uv run --frozen python scripts/bootstrap/bootstrap.py --step identity`, then `--step assignments`; R23 for `dev` and `prod`; re-enable Actions (`gh api --method PUT /repos/<organization>/<repository>/actions/permissions -F enabled=true -f allowed_actions=selected`, then R16 step 5's selected-actions list).
6. Redeploy a known-good commit to each environment in full mode, so the templates restore every declared setting: `gh workflow run deploy.yaml -f environment=<env> -f image_commit=<known-good commit> -f rollback=false`, approved in `prod`.
7. Finish with R07 steps 9 and 10.

Undo: none; the containment is lifted by steps 5 and 6. Audit record: the incident ticket and the records of step 2.

### 3.34 R32 — Remove a withdrawn document's text from conversation data (legal removal)

Performer: an operator at level `user_data`, after an admin deleted the document (`13-conversations-and-memory.md` section 10.12, procedure 11). Trigger: a legal removal request.

1. Open a ticket naming the document ID and the legal basis.
2. An admin deletes the document in the Documents screen (`adminDeleteDocument`), which hides it from readers at once and starts the clean-up of its chunks, renditions and blobs; step 4 doesn't need the document's SQL row, so it also works after that clean-up.
3. A group owner grants: `uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 4 --ticket <id> --reason "<legal basis>"` (or PIM; exit 6 means the operator is already a member); `az logout`; `az login --tenant <tenant ID>`.
4. `uv run docchat-ops --env <env> user redact-document --document-id <documentId> --ticket <id> --reason "<legal basis>"`: it prints the number of matching turn traces, asks for confirmation (in `prod` type `prod`) and replaces every source block of that document with `[removed]`, then prints the (`userId`, `sessionId`) pairs of the traces it found; copy them into the ticket. Exit 5 or 6: run it once more until it exits 0; exit 7: check the allowlist and the network.
5. Verify: one more run exits 0 and reports `traceCount` 0.
6. Record in the ticket that the stored answers, session summaries and follow-ups of the listed sessions may still quote the document: the command changes only source blocks (`13-conversations-and-memory.md` section 10.10), and no procedure deletes one session or edits one answer. When the ticket's approver decides that the legal basis requires removing a listed user's quotes, run R06 part B for that user, which also removes the user's DocChat app-role assignments, memberships, memory and every session; otherwise close with the quotes kept, as `82-security.md` section 1.6 accepts.
7. Revoke: `uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "legal removal finished"` (or let PIM expire); `az logout`.
8. Delete every `internal/` export package kept with an open ticket that holds the document's text (R06 part A step 4).

Undo: none; the replaced text can't be brought back. Audit record: one `document.traces_redacted` row per run (`documentId`, `traceCount`, `ticket`), part of the deletion register that R06 part C re-applies after a restore.

### 3.35 R33 — Reset the Durable task hub

Performer: a maintainer dispatches; an operator holding level `config` in the environment (R03 steps 2 and 3) runs steps 1, 5 and 6; in `prod` a release approver approves. Trigger: the last resort of `14-ingestion.md` section 19.2, only after R18 step 2's restart left the dispatcher stale.

1. The operator confirms: `docchat.ingestion.dispatcher.heartbeat_age` is still above 900 s 20 minutes after the `restart-ingestion` run of R18 step 2, and `uv run docchat-ops --env <env> ingestion status` (level `config`, granted by an approver with R03 steps 2 and 3) lists open runs.
2. `gh workflow run deploy-environment.yaml -f environment=<env> -f mode=reset-task-hub`; in `prod` approve it (section 1.5). The deploy identity stops the ingestion app, deletes through Resource Manager the host account's containers, queues and tables whose names start with `docchat<env>` (the host's own `azure-webjobs-*` containers stay) and starts the app (`83-infrastructure.md` section 10.13).
3. Follow it: `gh run watch <run ID>`; the job summary lists every deleted name.
4. The run fails with `task_hub_reset_failed`: the app stays stopped and no ingestion work runs until the mode succeeds; dispatch step 2 once more at once, and repeat until it succeeds.
5. Verify: within 15 minutes `docchat.ingestion.dispatcher.heartbeat_age` falls below 600 s, and `ingestion status` shows the open runs' items moving: the SQL ledger is untouched, running items' leases expire and are requeued, and no work is lost.
6. The approver revokes the operator's level (R03 step 8) and the operator runs `az logout`.

Undo: none; the hub's orchestration history is gone and the ledger rebuilds the work. Audit record: the workflow run with its dispatcher and approver; the Resource Manager deletes and the stop and start in the Activity Log.

### 3.36 R34 — Repair the CI runner group

Performer: the bootstrap operator (Owner, and owner of the GitHub organization). Trigger: under the private-networking option only (`ciPrivateNetworking` `true`, ADR 0122): a `dev-tests` or `load-tests` job waits more than 15 minutes for a runner, or its token step can't reach the test vault (`test_vault_unreachable`; `83-infrastructure.md` section 10.16). By default (`false`) the test jobs run on standard GitHub-hosted runners, and no runner group, CI network or private endpoint exists to repair. Prerequisites: R21's, with `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported holding the current shared allowlist, because step 4's `shared` step applies it to the registry and the test vault.

1. Confirm the CI path: `gh variable get DOCCHAT_CI_PRIVATE_NETWORKING` returns `true`. With `false` this runbook doesn't apply: a `test_vault_unreachable` comes from the job's read window, and the job log's `firewall.py` line names its code (`runner_ip_unresolved`, `runner_ip_mismatch` or `rule_not_effective`, each retried with `gh run rerun <run ID> --failed`); an address left on the vault goes to R26.
2. Read the runner: `gh api /orgs/<organization>/actions/hosted-runners --jq '.runners[] | select(.name == "docchat-ci-private") | {status, maximum_runners, runner_group_id}'` shows `Ready` and `maximum_runners` 10; a missing runner or another status is the fault.
3. Check the vault's private path: `az network private-endpoint show --name pe-kv-docchat-tst-<sharedToken>-vault --resource-group rg-docchat-shared --query provisioningState -o tsv` returns `Succeeded`, and `az network private-dns link vnet list --resource-group rg-docchat-shared --zone-name privatelink.vaultcore.azure.net --query "[].virtualNetwork.id" -o tsv` names `vnet-docchat-ci`.
4. Repair: `uv run --frozen python scripts/bootstrap/bootstrap.py --step shared`, then `--step ci-runner`; they recreate a missing endpoint, zone link, network configuration, runner group or runner and read each back. A stop code names a cause to fix first: `ci_runner_mismatch` (run the `ci-runner` step once more); `ci_region_unsupported` (change `ciNetworkLocation` by a reviewed pull request); `ci_private_networking_unavailable` (the organization's plan offers no larger runners: set `ciPrivateNetworking` to `false` by a reviewed pull request and re-run the `shared`, `assignments`, `github` and `ci-runner` steps, so the test jobs use standard runners and the vault's transient runner rule).
5. Verify: `gh run rerun <run ID> --failed`; its token step reads the vault.

Undo: none needed; the steps restore the declared state. Audit record: the bootstrap's Activity Log entries and the GitHub organization's audit log.

### 3.37 R35 — Change the API origin or add a custom domain

Performer: a maintainer; the DNS owner of the domain for part A. Trigger: a new web host name, or a new API origin (`10-web-client.md` section 2.3, item 6).

**A. Add a web custom domain**

1. Check the plan's limit: `webCustomDomains` holds at most 2 host names on `Free` and 5 on `Standard` (preflight check 14 refuses more with `custom_domains_exceed_plan`).
2. The DNS owner creates the CNAME record `<host>` → `<Static Web Apps default host name>` (`az staticwebapp show --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --query defaultHostname -o tsv`), which the `cname-delegation` validation needs.
3. A pull request adds `<host>` to `webCustomDomains` in `infra/env/<env>.json`; the what-if shows the Static Web Apps custom domain, the web registration's new redirect URI `https://<host>/` and the API's new platform CORS origin. When it is the first entry, `WEB_BASE_URL` becomes `https://<host>`, so the deployed tests and the sign-in check run on that origin.
4. Deploy with R01. Expected: `az staticwebapp hostname show --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --hostname <host> --query status -o tsv` returns `Ready`.
5. Verify CORS: `curl -s -o /dev/null -D - -X OPTIONS "<API_BASE_URL>/v1/me" -H "Origin: https://<host>" -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: authorization"` shows `Access-Control-Allow-Origin: https://<host>`; sign in at `https://<host>/` with the release sign-in check (`10-web-client.md` section 13.4).
6. A preflight that the platform answers without the origin after a correct deploy is a platform fault: revert the pull request and take the case to the product owner, who decides through a new ADR.

**B. Change the API origin**

1. The API keeps its default host name (`83-infrastructure.md` section 2.22); a custom API domain or a new API site first needs its own ADR, because the template declares no API host-name binding or certificate.
2. Deploy the API on the new origin while the old one keeps working, and check its `/healthz` answers `200`.
3. Change the `API_BASE_URL` output by pull request, then redeploy the web client (R01), so `config.json` and the content security policy change together.
4. Run the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`) and the release sign-in check.
5. Retire the old origin only after step 4 passes.

Undo: a revert pull request deployed with R01 (part A), or `API_BASE_URL` set back while the old origin still runs (part B). Audit record: the pull requests, the deploy runs and Entra's audit log for the redirect URI change (`docchat-alert-app-registrations` fires; acknowledge it with the ticket).

### 3.38 R36 — Change conversation or memory retention

Performer: a maintainer, with the product owner's approval in the ticket. Trigger: a retention decision (`13-conversations-and-memory.md` section 9.3).

1. Open a ticket with the environment, `conversationRetentionDays` and `memoryRetentionDays` (0 to 3,650; 0 keeps items) and the product owner's approval.
2. `prod`: announce the change to users 14 days before step 3, because turning retention on deletes every item whose last change is older than the period, and only a point-in-time restore inside the 30-day continuous-backup window brings such items back (7 days in `dev` and `load`).
3. A pull request sets the values in `infra/env/<env>.json`; the what-if shows the containers' `defaultTtl` (`-1` for 0, otherwise days × 86,400).
4. Deploy with R01.
5. Verify: `az cosmosdb sql container show --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <conversations|memories> --query resource.defaultTtl -o tsv` returns the expected value.
6. Items the TTL deletes inside a live container return only through a restore of the whole account into a new account, which R06 part C step 6 sends to the product owner as a new ADR; step 2's announcement exists for that reason.

Undo: a revert pull request deployed stops further deletions; items already deleted are covered by step 6. Audit record: the pull request, the deploy run and the Activity Log.

### 3.39 R37 — Retrieve an uploaded file for an audit or legal request

Performer: an operator at level `user_data`. Trigger: an audit or legal request for the file an administrator uploaded, as uploaded; readers and administrators get only PDF renditions (`82-security.md` SEC-54), so this runbook is the supported way to hand over an uploaded file.

1. Open a ticket naming the requester, the legal or audit basis, the document ID and the version number, and the approved recipient and delivery channel.
2. An administrator reads the version ID from `adminGetDocument` (`versions[].versionId`, the versions the web client's document panel lists) and records it in the ticket. A version that is `failed` with `source_missing`, or a deleted document whose blobs were purged, has no file: the command exits 2 or 4, so record that in the ticket and stop.
3. A group owner grants: `uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 1 --ticket <id> --reason "<basis>"` (or PIM); `az logout`; `az login --tenant <tenant ID>`.
4. On a machine whose public address is in the environment's operator allowlist, in a directory that only the operator can read: `uv run docchat-ops --env <env> document fetch-source --version <versionId> --out <path> --ticket <id> --reason "<basis>"`. It prints that the file is untrusted before the download. Expected: exit 0 and the version ID, media type, size and SHA-256. Exit 2: choose a new `<path>`; exit 4: check the version ID; exit 5: the stored bytes don't match the recorded SHA-256, so record that in the ticket and escalate to the security responder without handing anything over; exit 7: check the allowlist and the network.
5. Don't open the file on the operator's machine. Hand it over unopened through the channel the ticket names, with its SHA-256 and the warning that it is an untrusted upload, to be opened only in an isolated environment; then delete the local copy and record the deletion in the ticket.
6. Revoke: `uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "source fetch finished"` (or let PIM expire); `az logout`.

Undo: none needed; the command changes no stored data. Audit record: one `document.source_fetched` row per fetch (`indexId`, `documentId`, `versionNumber`, `sizeBytes`, `ticket`), the just-in-time grant in Entra ID's audit log and the ticket.

## 4. Rehearsal and record keeping

### 4.1 Runbook files

1. Each runbook of section 3 is one Markdown file in `docs/runbooks/`, named `RNN-<slug>.md` after its ID, the slug being its title in lower case with every run of characters other than letters and digits replaced by one `-` (`R01-deploy.md`, `R24-change-the-operator-allowlist.md`).
2. The file's first heading is `RNN — <title>` exactly as section 3.2 writes it, followed by the performer, trigger, prerequisites, numbered steps with their expected results, verification, undo and audit record of section 3.
3. A change to a command, workflow input, script or exit code that a runbook uses changes the runbook file in the same pull request, and this file in the same pull request when the change alters a rule stated here.

### 4.2 Rehearsals

1. Every runbook is rehearsed in `dev` at the release stage (`90-build-order.md`) and once more after any change to the commands, workflows or scripts it uses; R11 runs in `load`, its own environment.
2. R32 is rehearsed on a test document cited by a test user's turns; R33 with no ingestion run open; R06 part C with a SQL restore of `dev` to a point 1 hour back.
3. R21, R30, R31 and R34 are rehearsed as walk-throughs: the operator runs every read-only command and stops before each step that changes anything.
4. R25 and R27 apply only under content-share fallback 1 and are rehearsed when it is adopted.
5. A rehearsal that fails a step leaves the runbook open until a pull request corrects it and a new rehearsal passes.

### 4.3 Records

| Record | Where | Content | Kept |
|---|---|---|---|
| Rehearsal log | `docs/runbooks/rehearsals.md`, one table row per rehearsal | date (UTC), runbook ID, operator handle, environment, result (`passed`, `failed at step <n>`), the correlation IDs or run IDs it produced | permanently, in the repository |
| Ticket | the organisation's ticket system | the request, approvals, each command's correlation ID, run IDs, the outcome; never content, a password or a token | the organisation's ticket retention |
| Release checklist | the release ticket (section 3.3.1) | each item's evidence | as the ticket |
| Audit rows | `app.audit_events` (`80-data-model.md` section 2.21) | one row per changing command | as that section states |
| Platform records | Activity Log, Entra `AuditLogs` and the GitHub audit log, copied to `log-docchat-security` where the source allows | every Azure, Entra and GitHub change | 365 days |

## Acceptance criteria

1. `scripts/ops/src/docchat_ops/commands/` holds `local.py`, `config.py`, `override.py`, `users.py`, `jit.py`, `reindex.py`, `ingestion.py`, `chat.py`, `testusers.py`, `trace.py` and `document.py`, and `uv run docchat-ops --help` lists exactly the command groups of section 2.8. Proof: the operator CLI unit tests' catalogue test, which compares the parser's commands with section 2.8's tables.
2. `local init`, `config show`, `chat capacity-check`, `ingestion status` and `testusers check` refuse `--ticket` and `--reason` with exit 2; every other command exits 2 without either, with a ticket outside 1 to 64 characters of letters, digits, `-`, `_` and `#`, or with a reason outside 10 to 500 characters, before any sign-in. Proof: the operator CLI unit tests.
3. `--env` is accepted before the group and after the command; two different values exit 2; `--env local` is refused by every command except `local init`. Proof: the operator CLI unit tests.
4. In `prod` every changing command accepts only the typed `prod`, and exits 2 with nothing written when standard input isn't a terminal; outside `prod`, `--yes` skips the prompt. Proof: the operator CLI unit tests.
5. A service-principal caller gets exit 3 from every data-changing command in `dev` and `prod` and from `chat model-check` in every environment, and is accepted by `chat capacity-check`, `ingestion status` and `testusers check`. Proof: the operator CLI unit tests with a faked `az account show`.
6. Every run prints `correlationId: <32 hex characters>` as the first line of standard error before any network call, and every audit row it inserts carries that ID. Proof: the operator CLI unit tests.
7. Each exit code 0 to 7 is produced by at least one command under the conditions of section 2.6, and every non-zero exit writes one line naming the code's meaning, the next step and the correlation ID, with no token, password, key or content. Proof: the operator CLI unit tests, one per code.
8. Each changing command inserts the audit action and metadata of section 2.8 in the transaction of its change, validated by the metadata models of `80-data-model.md` section 2.21; an ops-config change whose metadata would exceed 4,000 characters stores `previousSha256` and `newSha256` in place of the values; the commands listed in section 2.5 item 3 insert no row. Proof: the operator CLI integration tests against the local SQL Server.
9. `--out` creates a missing directory with mode 0700, refuses a directory that holds files with exit 2, and writes every file with mode 0600; standard output never carries a sentinel string seeded into conversation text, document text, a name or an e-mail address. Proof: the operator CLI unit tests with the sentinel fixtures.
10. `config set` refuses an unknown key, an out-of-range value and a broken cross-field rule with exit 2, and a concurrent change with exit 6; the new value reaches a running API within 60 seconds. Proof: the operator CLI integration tests; the R03 rehearsal row, whose step 7 read the new value within 60 seconds.
11. `user export`, `user delete`, `user verify`, `user redact-document`, `user deletion-register` and `user audit-events` behave as section 2.8.4 states against the Cosmos DB emulator and the local SQL Server, including exit 5 on an interrupted delete that a rerun completes, and the deletion register listing exactly the `user_data.deleted` and `document.traces_redacted` rows at or after `--since`. Proof: the user-data command integration tests and the redaction integration test.
12. `jit grant` exits 2 for a self-grant and for `--hours` outside 1 to 4, and 6 for an existing member; in fallback mode it creates and `jit revoke` deletes the level's data-plane assignment. Proof: the operator CLI unit tests with a Graph and Resource Manager fake.
13. `testusers rotate` exits 2 with `load_run_in_progress` while `rg-docchat-load` carries `docchat-load-run`, rotates 25 aliases with `--set all`, and writes one `test_users.rotated` row in `dev`; `testusers check` exits 5 for a passed or near expiry and lists `warnings` with `--json`. Proof: the operator CLI unit tests; the nightly `test-credential-expiry` job's run.
14. `trace show` refuses an ID that isn't 32 lower-case hexadecimal characters with exit 2, exits 4 when no record exists, and prints from `Properties` only `docchat.error_code`, `docchat.session_id`, `docchat.turn_id` and `enduser.id`. Proof: the operator CLI unit tests with recorded Logs query responses.
15. `docs/runbooks/` holds 37 runbook files, `R01-<slug>.md` to `R37-<slug>.md`, each opening with the heading of its row in section 3.2, plus `rehearsals.md`. Proof: `ls docs/runbooks/R*.md | wc -l` returns 37 and each file's first line matches section 3.2.
16. Every runbook title named by another file of this specification (`83-infrastructure.md`, `82-security.md`, `81-config.md`, `85-observability.md`, `13-conversations-and-memory.md`, `14-ingestion.md`, `01-repository.md`, `00-overview.md`) is a row of section 3.2. Proof: a search of those files for `runbook "<title>"` whose titles all appear in section 3.2.
17. Every alert of `85-observability.md` section 6 appears in R20's table with the runbook its description names. Proof: comparing the alert names in `alerts.bicep`'s rule descriptions with R20's table.
18. Every runbook has a `passed` row in `docs/runbooks/rehearsals.md` in `dev` (R11 in `load`) dated after the last change to the commands it uses, except R25 and R27 before fallback 1 is adopted; R21, R30, R31 and R34 have a walk-through row. Proof: the rehearsal log.
19. The rehearsal of R19 raises `human-conversation-access` within its 15-minute window and writes one `conversation.read` row naming the ticket. Proof: the R19 rehearsal row with the alert's time and the audit event ID.
20. The rehearsal of R06 restores `dev`'s SQL database to a point before a `user delete`, re-applies the register, and `user verify` exits 0 for that user before the app reopens. Proof: the R06 rehearsal row with the correlation IDs of the register and verify runs.
21. The rehearsal of R32 on a test document cited by a test user's turns ends with a run reporting `traceCount` 0, and a `user read-conversation` of one of those turns shows `[removed]` in place of the document's text. Proof: the R32 rehearsal row.
22. The rehearsal of R02 deploys the previous good commit to `dev` with `rollback=true`, and `test-dev` passes on it. Proof: the R02 rehearsal row with the workflow run ID.
23. The release checklist of section 3.3.1 is completed in the release ticket of every `prod` approval. Proof: the approval comment's ticket link and the ticket's checklist.
24. Running `jit list` at the monthly access review shows the three groups of each environment empty, and every human principal in the review's query matches a ticket. Proof: the review's ticket.
25. `chat model-check` exits 2 for any `--env` other than `dev` before any sign-in; against a faked Azure OpenAI client and a faked Content Safety endpoint it makes one call per deployment and configured effort, 20 `summary`-shaped and 20 `memory`-shaped calls and the two Prompt Shields probes, exits 2 when a call answers `400`, a structured output fails its schema, a tokenizer count differs by more than 1%, a latency p95 exceeds its timeout or a probe's outcome is `failed`, records `documents_refused` and `user_prompt_refused` for a probe's `400` without failing, and inserts one `model_deployments.checked` row holding every result, whose `result` is `failed` then and `passed` on exit 0. Proof: the operator CLI unit and integration tests; the deployed model check, the deployed tokenizer check, the deployed chat-small latency test and the deployed Prompt Shields request test, run by an operator in `dev` (`84-testing.md`).
26. `ingestion drain-poison`'s dry run peeks at most 32 messages and prints "at least 32" when it saw 32; after confirmation it drains in receives of 32 until a receive returns none, prints the number drained and records it as `deletedCount`. Proof: the operator CLI unit tests with a faked queue holding 0, 31, 32 and 70 messages.
27. `document fetch-source` exits 3 without level `user_data`; with it, it prints the untrusted-file warning before the download, writes the version's source to `--out` with mode 0600 and no execute bit, refuses an existing `--out` with exit 2, exits 5 and leaves no file when the bytes differ from `document_versions.sha256`, and writes one `document.source_fetched` row holding the ticket; the rehearsal of R37 in `dev` fetches a test document's source whose SHA-256 equals its version's. Proof: the source fetch command test; the R37 rehearsal row.
