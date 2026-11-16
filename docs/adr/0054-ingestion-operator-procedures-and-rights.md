# ADR 0054: Ingestion operator procedures and rights

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 86-operations.md, 14-ingestion.md, 80-data-model.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0024, 0025, 0029, 0034, 0036, 0045, 0098, 0115, 0138, 0139, 0142

## Context

- Operators work outside the app, through audited scripts kept in `scripts/`. Their standing rights are Reader on the resource group, and data-plane access exists only through just-in-time group membership (ADR 0024). Durable Functions management endpoints and keys are never exposed to anyone: the ingestion app has public network access off and no HTTP trigger. The SQL ledger is the source of truth and queue messages and events are hints (ADR 0034), so most recovery is a change to ledger rows that the dispatcher then acts on.
- Ingestion still needs people for a few cases: re-indexing outside the admin screen, reading a stuck dispatcher's state, requeuing, failing or cancelling items and runs, abandoning a rebuild whose first item can't create its index, moving a physical index to renamed deployments of the same model, draining the poison queue, restarting the function app, and, as a last resort, resetting the Durable task hub. Each of these must say who acted and why, and none may give a person a standing right.
- Vendor facts (checked 2026-10-14): Storage Queue Data Message Processor (`8a0f0c08-91a1-4084-bc3d-661d67233fed`) grants `messages/read` and `messages/process/action`, which allow peek, receive and delete; sending needs `messages/add/action` or `messages/write`, which it lacks. The queue trigger moves a message to `<queue>-poison` after 5 failed dequeues. Contributor's actions (`*`, less authorisation writes) cover `Microsoft.Web/sites/restart/action`, `stop/action` and `start/action` and the Resource Manager deletes of a storage account's containers, queues and tables. The Durable task hub name allows letters and digits only.

## Decision

1. **One audited CLI.** Every procedure that reads or writes ingestion data is a command of `uv run docchat-ops --env <env> reindex|ingestion …` (`scripts/ops/src/docchat_ops/commands/reindex.py` and `ingestion.py`), on the framework of ADR 0139: the operator's own `az login` through `AzureCliCredential`; endpoints from `.azure/<env>/.env`; `--ticket` (1 to 64 characters) and `--reason` (10 to 500) on every change; typed confirmation of the environment name in `prod`; one trace ID per run; exit codes 0 success, 1 unexpected error, 2 refused precondition or validation, 3 access denied, 4 not found, 5 incomplete, 6 conflict, 7 store unreachable. The commands reuse `docchat_core.ingestion.runs` with no copied logic, write one `app.audit_events` row with actor type `operator` (the `oid` of the SQL token) in the same transaction as the change, and print IDs only. A refused precondition exits 2 and a row that changed meanwhile exits 6, with nothing written.
2. **The commands.**

   | Command | Effect | Audit action |
   |---|---|---|
   | `reindex start --index-id <uuid>` | the checks and transaction of `adminStartReingest`; prints the run ID and mode | `ingestion.reindex_started` |
   | `ingestion status` | read-only: the `app.ingestion_dispatcher_state` row, queued and running counts per priority, the oldest queued item's age, the open runs and the last 5 scanner canaries (IDs, states, counts and times) | none (no personal data) |
   | `ingestion requeue-item --item-id <uuid>` | a `running` item whose lease has expired back to `queued`; refused while the lease is valid | `ingestion.item_requeued` |
   | `ingestion fail-item --item-id <uuid>` | a `queued` item, or a `running` one whose lease has expired, to `failed` with `processing_interrupted` and the failure side effects of `record_item_outcome` | `ingestion.item_failed` |
   | `ingestion cancel-run --run-id <uuid>` | the run's `queued` items to `cancelled`; running items finish | `ingestion.run_cancelled` |
   | `ingestion abandon-rebuild --index-id <uuid>` | the `building` physical index to `retiring` with `delete_after` = now, its queued items cancelled, a delete item in a `maintenance` run, the index status recomputed | `ingestion.rebuild_abandoned` |
   | `ingestion set-deployments --physical-index-id <uuid> --query <name> --ingestion <name>` | new embedding deployment names on one physical index; refused unless the catalogue entry's model and version match | `ingestion.deployments_changed` |
   | `ingestion drain-poison` | after the operator confirms the runs' items are queued or done, receives and deletes every message of `ingestion-requests-poison` | `ingestion.poison_drained` (`deletedCount`) |

3. **Rights.** The commands run under just-in-time membership of `sg-docchat-<env>-config-operators` (level `config`: PIM for Groups activation or `docchat-ops jit grant --level config`, granted by a group owner other than the operator, for at most 4 hours; ADR 0024), from a machine whose public IP is in the environment's operator allowlist. The group's contained database user is in role `docchat_config_operator`, whose rights on ingestion tables come from the ingestion migration: `SELECT` on all eleven; `UPDATE` on `search_indexes`; `INSERT` and `UPDATE` on `physical_indexes`, `ingestion_runs` and `ingestion_run_items`; `UPDATE` on `chunk_sets` and, on `document_versions`, `UPDATE (state, failure_code, failed_at)` only (for `fail-item`; ADR 0025 rule 4a), so an operator can't change a scan result; `INSERT` on `app.audit_events`; no `DELETE` on any ingestion table and no DDL. The group also holds Storage Queue Data Message Processor on `ingestion-requests-poison` alone. No operator holds a queue-send right: a run an operator creates starts at the dispatcher's next wake, within `ingestion.active_wake_seconds` (60) while work is outstanding and within `ingestion.idle_wake_minutes` (60) when idle. The standing group `sg-docchat-operators` holds Reader on the resource group and Log Analytics Reader on the workspace only.
4. **Restart and reset run as the deploy identity.** `deploy-environment.yaml` accepts a `workflow_dispatch` with `environment` (`dev` or `prod`) and `mode`, whose only choices are `restart-ingestion` and `reset-task-hub`, so a dispatch can never deploy an image. The job runs in GitHub environment `<env>` (`prod` needs its required reviewers' approval), concurrency group `docchat-env-<env>`, as that environment's deploy identity, whose Contributor role covers every call.
   - `restart-ingestion`: record the UTC time; find the function app by tag `azd-service-name=ingestion`; `az functionapp restart` (failure: `ingestion_restart_failed`); poll the Logs query API every 30 s for at most 10 minutes for `ingestion_worker_started` with `AppVersion` equal to the API site's `docchat-release-sha` tag and `TimeGenerated` after the recorded time (none: `ingestion_start_failed`); write the app name and both times to the job summary. The image carries its extension bundle, so a restart downloads nothing.
   - `reset-task-hub`: `az functionapp stop`; list the host account's `blobServices/default/containers`, `queueServices/default/queues` and `tableServices/default/tables` through Resource Manager (API 2025-06-01) and delete each one whose name starts with the hub name `docchat<env>`, compared case-insensitively, keeping the host's `azure-webjobs-*` containers; `az functionapp start`; list every deleted name in the job summary. A failed deletion leaves the app stopped and fails with `task_hub_reset_failed`; running the mode again completes the reset.
5. **Escalation order** (runbooks R18 and R33). (a) The keep-alive's watchdog restarts a stale dispatcher on its own: last sweep older than 10 minutes while work is outstanding, or 75 minutes otherwise (ADR 0036). (b) Alert A18 (heartbeat age above 900 s while items are queued): read `docchat.ingestion.dispatcher.heartbeat_age` and `ingestion status`; still stale 20 minutes later, run `restart-ingestion`. (c) Still above 900 s 20 minutes after the restart, with open runs listed: run `reset-task-hub`; within 60 minutes the heartbeat age falls below 120 s. The ledger is untouched in every step: running items' leases expire and are requeued (ADR 0034), so no work is lost. Alert A17 (oldest queued item above 1,800 s) and A19 (any poison message, 2-hour window) lead to the same runbook.

## Alternatives considered

- **SQL by hand.** No validation, no audit row, and statements that skip the ledger's guards (fencing by claim number, the guarded Flip). Lost.
- **The Durable HTTP management API or host keys for operators.** They are never exposed, and the app accepts no inbound traffic. Lost.
- **Standing Contributor or Website Contributor for operators, to restart the app.** A standing write right on the resource group; the workflow mode gives the same restart under the environment's approval and leaves the Activity Log entry under the deploy identity. Lost.
- **Storage Queue Data Message Sender on `ingestion-requests` for operators.** A send right would let a person inject messages; the ledger already guarantees that every run is picked up at the next wake. Lost.
- **Operator data rights on the host storage account for a manual reset.** The host account never has public access, so no operator path reaches it; Resource Manager deletes by the deploy identity need none. Lost.
- **Recovery actions in the admin screens.** Admins manage content, not the pipeline's machinery, and the parallelism setting belongs to operators, outside the admin UI. Lost.

## Consequences

- Every ingestion recovery is typed, validated, attributed and audited; a failed precondition changes nothing.
- An operator needs an approver, a ticket and an allowlisted machine; a restart or a reset needs a maintainer's dispatch and, in `prod`, a reviewer's approval. With an empty operator allowlist, SQL and the data account admit no operator machine, so these commands can't run until an address is added.
- Runs an operator creates wait for the next wake, up to 60 minutes when the dispatcher is idle.
- A task-hub reset discards orchestration history: in-flight sub-orchestrations are lost, and their items come back only when their leases expire, at most the running activity's deadline plus 10 minutes later (55 minutes for an Extract); `requeue-item` and `fail-item` refuse until then.
- Obligations: an integration test proving that, as `docchat_config_operator`, every command succeeds with these grants and `DELETE` on every ingestion table is denied; the deployed restart-and-resume test in `dev`; the CLI's tests of exit codes and audit rows; runbooks R18 and R33; alerts A17, A18 and A19; the poison queue's role assignment in `main.bicep`.

## References

- Azure built-in roles for Storage (queue roles and their actions): https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage (checked 2026-10-14)
- Authorise Azure Storage data operations with Microsoft Entra ID (queue message permissions): https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory (checked 2026-10-14)
- Azure Queue storage trigger (poison queue after 5 dequeues): https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-storage-queue-trigger?pivots=programming-language-python (checked 2026-10-14)
- Durable Functions `host.json` settings (hub name rules): https://learn.microsoft.com/en-us/azure/durable-task/durable-functions/durable-functions-host-json-settings (checked 2026-10-14)
- Durable Task instance management (terminate, purge, events to missing instances): https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-instance-management (checked 2026-10-14)
- Azure built-in roles, general (Contributor, Reader): https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/general (checked 2026-10-14)
