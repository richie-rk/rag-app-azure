# R33 — Reset the Durable task hub

**Performer:** a maintainer dispatches the reset; an operator holding level `config` in the environment (R03 steps 2 and 3) runs steps 1, 5 and 6; in `prod` a release approver approves the run.

**Trigger:** the last resort of `14-ingestion.md` section 19.2, only after R18 step 2's `restart-ingestion` left the dispatcher stale: a dispatcher that doesn't recover.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); `gh` signed in with the right to dispatch workflows.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. The operator confirms the reset is needed: on the `ingestion` tile "Dispatcher heartbeat age and poison messages", `docchat.ingestion.dispatcher.heartbeat_age` is still above 900 s 20 minutes after the `restart-ingestion` run of R18 step 2, and, at level `config` (granted by an approver with R03 steps 2 and 3):

   ```bash
   uv run docchat-ops --env <env> ingestion status
   ```

   Expected: the dispatcher's state row with a stale heartbeat, and the open runs listed. With no open run, stop here: there is no stuck work to recover.

2. The maintainer dispatches the reset; in `prod` the release approver approves it (`86-operations.md` section 1.5):

   ```bash
   gh workflow run deploy-environment.yaml -f environment=<env> -f mode=reset-task-hub
   ```

   The environment's deploy identity stops the ingestion app, deletes through Resource Manager every container, queue and table of the host account whose name starts with `docchat<env>` (the host's own `azure-webjobs-*` containers stay), and starts the app (`83-infrastructure.md` section 10.13).

3. Follow it:

   ```bash
   gh run watch $(gh run list --workflow deploy-environment.yaml --limit 1 --json databaseId -q '.[0].databaseId')
   ```

   Expected: the run succeeds and its job summary lists every deleted container, queue and table.

4. The run fails with `task_hub_reset_failed`: the app stays stopped and no ingestion work runs until the mode succeeds. Dispatch step 2 once more at once, and repeat until it succeeds; each run deletes what the previous one left.

5. The operator verifies: within 15 minutes `docchat.ingestion.dispatcher.heartbeat_age` falls below 600 s on the same tile, and `ingestion status` shows the open runs' items moving. The SQL ledger is untouched: running items' leases expire and are requeued, and no work is lost.

6. The approver revokes the operator's level (R03 step 8), or PIM expires, and the operator signs out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

## Verification

Step 5: the heartbeat age is below 600 s within 15 minutes of the run, the open runs' items move to finished states, and `ingestion-dispatcher-stale` resolves.

## Undo

None: the hub's orchestration history is gone, and the dispatcher's first sweep rebuilds the work from the SQL ledger.

## Audit record

The workflow run with its dispatcher and approver, and its job summary of deleted names; the Resource Manager deletes and the app's stop and start in the Activity Log, under the deploy identity; the ticket.
