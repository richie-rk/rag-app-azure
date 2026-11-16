# Rehearsal log

One row per rehearsal of a runbook in this folder, added by the pull request that records it (`86-operations.md` section 4; ADR 0142). Rows are appended and never edited. The release sign-in check, the Defender acceptance check (R12), the bootstrap (R21) and every first provision (R22) are logged here too; R21, R30, R31 and R34 are rehearsed as walk-throughs.

- **Date (UTC):** `YYYY-MM-DD`.
- **Runbook:** the runbook ID, `R01` to `R37`.
- **Operator:** the performer's GitHub handle.
- **Environment:** `dev`, `prod` or `load`.
- **Result:** `passed`, or `failed at step <n>`; a failed rehearsal leaves the runbook open until a corrected runbook passes a new one.
- **Correlation or run IDs:** the `docchat-ops` correlation IDs and the workflow run IDs the rehearsal produced; never content, a password or a token.

The release stage (`90-build-order.md` section 3.7) counts the catalogue's rehearsal rows from this log (`84-testing.md` sections 5.9, 5.12 and 8.4). Their rows name these runbooks and hold these IDs:

| Row | Runbook | Environment | The IDs column holds |
|---|---|---|---|
| T-549 the runbook rehearsal log | every runbook of `86-operations.md` section 3.2, each dated after the last change to the commands it uses; R25 and R27 once content-share fallback 1 is adopted; R21, R30, R31 and R34 as walk-throughs | `dev`; R11 in `load` | the IDs of that rehearsal |
| T-550 the ops-config change rehearsal | R03 | `dev` | the `config set` correlation ID and the UTC time step 7 saw the new value, within 60 s |
| T-551 the conversation-read rehearsal | R19 | `dev` | the `user read-conversation` correlation ID, the `human-conversation-access` alert's time and the `conversation.read` audit event ID |
| T-552 the restore-and-register rehearsal | R06 part C (SQL restore of `dev` to a point 1 hour back) | `dev` | the correlation IDs of the deletion-register run and of `user verify` |
| T-553 the legal-removal rehearsal | R32, on a test document cited by a test user's turns | `dev` | the removal run's correlation ID (`traceCount` 0) and the `user read-conversation` correlation ID |
| T-554 the rollback rehearsal | R02 | `dev` | the `deploy.yaml` run ID of the `rollback=true` dispatch and of its `test-dev` |
| T-455 the deployed re-pointed-tag test | R02 | `dev` | the `deploy.yaml` run ID whose `resolve` failed with `image_attestation_invalid` |
| T-464 the release-ordering rehearsal | R02 | `dev` | the run ID whose release-ordering summary shows the older run not deployed, and the rollback dispatch's run ID |
| T-465 the task hub reset rehearsal | R33 | `dev` | the `mode: reset-task-hub` run ID and the ID of the queued run that completed |
| T-466 the ingestion restart rehearsal | R33 | `dev` | the `mode: restart-ingestion` run ID and the ID of the queued run that completed |
| T-473 the Defender acceptance check | R12 | each environment with scanning on, before uploads open | the first `clean` canary's time |

| Date (UTC) | Runbook | Operator | Environment | Result | Correlation or run IDs |
|---|---|---|---|---|---|
