# R20 — Alert response index and runbook maps

**Performer:** the on-call operator.

**Trigger:** any alert. Every alert's description names its runbook (`85-observability.md` section 6); this runbook is the index.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

## Steps

1. Read the alert: its description starts with the alert ID and the runbook title (`85-observability.md` section 6.1).
2. Find the alert in the table below, take its first action, and follow the runbook it names.
3. For a security alert explained by a pipeline run or a ticket, write the run ID or the ticket in the acknowledgement.

## Alerts

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

## Infrastructure procedures and their runbooks

The procedures of `83-infrastructure.md`:

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
| Repair the CI runner group (private-networking option only) | R34 |
| Turn `prod` scanning off | R12 step 10 |

## Procedures of the other files and their runbooks

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

## Verification

None of its own: the runbook each row names states its verification.

## Undo

None; this index changes nothing.

## Audit record

The alert's acknowledgement, naming the run ID or ticket for a security alert, and the records of the runbook followed.
