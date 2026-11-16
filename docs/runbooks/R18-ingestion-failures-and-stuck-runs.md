# R18 — Ingestion failures and stuck runs

**Performer:** an operator at level `config` for the `ingestion` and `reindex` commands (`14-ingestion.md` section 19); a maintainer for the workflow modes of `deploy-environment.yaml`; in `prod` a release approver approves a workflow mode.

**Trigger:** the alerts `ingestion-item-failures`, `ingestion-queue-age`, `ingestion-dispatcher-stale`, `ingestion-poison-messages`, `malware-detected`, `ingestion-throttling`, `ingestion-run-failed` and `rerender-failed`; uploads paused by the scan gate; a planned raise of `ingestion.max_parallel_items`.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL and the data account), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`). For the commands at level `config`, the membership of R03 steps 2 and 3 (PIM activation, or a group owner's `docchat-ops jit grant --level config`, then `az logout` and `az login --tenant <tenant ID>`).

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it. Workbook tiles are named as `85-observability.md` section 5.5 names them.

## Steps

1. `ingestion-item-failures`, `ingestion-run-failed`: read the `ingestion` tiles "Item outcomes by code" and "Latest failed items", and apply the remedy of `14-ingestion.md` section 17.2 for each outcome code (a re-upload, a method parameter, a scan failure). DOCX and PPTX items failed with `convert_timeout`, `convert_memory_exceeded` or `convert_failed`:
   - read the `ingestion` tile "Office conversions": the outcomes of `docchat.ingestion.convert.duration` by content type;
   - a few failures on large or damaged files: the index's admin splits the file, or saves it as PDF in Word or PowerPoint, and uploads it as a new version (the version's `failureMessage` says which);
   - failures of most conversions since a release: the converter of that image is at fault, so roll back with R02 and open an issue naming the release; a re-ingest of the failed documents (R05) after the fix converts them, because no converted PDF was stored for a failed version.

2. `ingestion-queue-age`, `ingestion-dispatcher-stale`: read the `ingestion` tile "Dispatcher heartbeat age and poison messages", then, at level `config`:

   ```bash
   uv run docchat-ops --env <env> ingestion status
   ```

   Expected: the dispatcher's state row, the queued and running counts per priority, the oldest queued item's age, the open runs and the last 5 scanner canaries. The keep-alive restarts a stale dispatcher by itself (last sweep older than 10 minutes while work is outstanding, 75 minutes otherwise). When the heartbeat stays stale beyond those limits for more than 10 minutes, the function app itself is unhealthy, and a maintainer restarts it (approved in `prod`, `86-operations.md` section 1.5):

   ```bash
   gh workflow run deploy-environment.yaml -f environment=<env> -f mode=restart-ingestion
   gh run watch $(gh run list --workflow deploy-environment.yaml --limit 1 --json databaseId -q '.[0].databaseId')
   ```

   Expected: the job summary names the app, the restart time and the time of the `ingestion_worker_started` event that followed it. `ingestion_restart_failed`: nothing changed; dispatch the mode once more. `ingestion_start_failed`: no worker started on the release within 10 minutes; follow R07. Items whose leases expired during the restart are requeued at the dispatcher's next sweep.

3. To act on one item or one run at once, at level `config`:
   - a `running` item with an expired lease:

     ```bash
     uv run docchat-ops --env <env> ingestion requeue-item --item-id <uuid> --ticket <id> --reason "<reason>"
     ```

     Expected: exit 0, the item `queued`; exit 2 means the lease is still valid or the item isn't `running`.
   - an item that must stop:

     ```bash
     uv run docchat-ops --env <env> ingestion fail-item --item-id <uuid> --ticket <id> --reason "<reason>"
     ```

     Expected: exit 0, the item `failed` with `processing_interrupted`; exit 2 means a valid lease or a finished item.
   - a run to stop (its `running` items finish):

     ```bash
     uv run docchat-ops --env <env> ingestion cancel-run --run-id <uuid> --ticket <id> --reason "<reason>"
     ```

     Expected: exit 0 and the cancelled item count; exit 2 means the run had finished, exit 4 an unknown run.
   - a rebuild to give up:

     ```bash
     uv run docchat-ops --env <env> ingestion abandon-rebuild --index-id <uuid> --ticket <id> --reason "<reason>"
     ```

     Expected: exit 0, the building physical index `retiring`, its queued items cancelled and a deletion item queued; exit 2 means no building physical index.
   - a physical index moved to a renamed deployment pair of the same model and version:

     ```bash
     uv run docchat-ops --env <env> ingestion set-deployments --physical-index-id <uuid> --query <name> --ingestion <name> --ticket <id> --reason "<reason>"
     ```

     Expected: exit 0; exit 2 means no `DOCCHAT_OPENAI_EMBEDDING_MODELS` entry with those names has the index's model and model version.

4. `ingestion-poison-messages`: confirm with `ingestion status` that the runs the messages name have their items queued or finished, then drain the poison queue; in `prod` type `prod` when asked:

   ```bash
   uv run docchat-ops --env <env> ingestion drain-poison --ticket <id> --reason "<reason>"
   ```

   Expected: the dry run's count and run IDs, then, after confirmation, `deletedCount`. Exit 5: a receive or delete failed part-way; run it once more to drain the rest.

5. `malware-detected`: the source blob was deleted at detection and stays soft-deleted for 14 days; the `document.malware_detected` audit row names the version and, as its subject, the uploader; no ingestion output exists for that version. Then R07 step 6.

6. `ingestion-throttling`: compare the ingestion deployments' tokens per minute with the `openai` tile "429s by deployment, chat path and ingestion"; lower `ingestion.max_parallel_items` with R03, or raise the deployment's capacity with R09.

7. Uploads paused by the scan gate: R12 step 9.

8. Raising `ingestion.max_parallel_items` to `p`: check the ingestion embedding and table-summary deployments' tokens per minute (a document can draw `p` × 2 embedding requests of up to 60,000 tokens and `p` × 4 summary calls at once), Document Intelligence's 15 analyze transactions per second, and that `ingestion.maxBurst` allows `ceil(p / 2)` EP2 instances. In `prod` above 2, first raise `ingestion.alwaysReady` in `infra/env/prod.json` to `ceil(p / 2)` by pull request and R01; then set the value with R03.

9. A failed `rerender` item (`rerender-failed` for `render_failed` and `render_too_large`; for every code, the tile "Latest failed items", operation `rerender`; `14-ingestion.md` section 6.15): its chunk set stays served with the renditions of the earlier `RENDITION_RULES_VERSION`, every page of them verified under those rules, and its citations answer as before.
   - `storage_unavailable`, `activity_timeout` or `processing_interrupted`: nothing to do; sweep duty 12 queues the chunk set again 24 hours after the failure. A second failure with the same code goes to the storage or dispatcher steps of this runbook.
   - `render_failed` or `render_too_large`: the deployed rules refuse a page of this PDF, and sweep duty 12 doesn't queue it again while the item's row exists (180 days). Read the pull request that raised `RENDITION_RULES_VERSION`, whose description names what the change leaves out. When the change closed a way for a rendition to act on a reader's viewer, ask the index's admin, through the ticket, to delete the document or upload a changed file as a new version, within 2 working days; otherwise the earlier renditions stay until the admin replaces the document.
   - `source_changed` or `source_missing`: the stored PDF doesn't match the chunk set or is missing, and the renditions stored under the lower rules version stay served; ask the index's admin, through the ticket, to delete the document and upload the file again.
   - Any other code: apply the remedy of `14-ingestion.md` section 17.2 for it.

10. Last resort, when the dispatcher stays stale 20 minutes after step 2's restart with open runs listed: R33.

11. Revoke the level, or let PIM expire; then sign out:

    ```bash
    uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
    az logout
    ```

## Verification

The alert that triggered the runbook resolves (the state alerts `ingestion-queue-age`, `ingestion-dispatcher-stale`, `ingestion-poison-messages` and `ingestion-throttling` on their own; the event alerts after their mute); `ingestion status` shows the dispatcher's heartbeat fresh, the oldest queued item younger than 30 minutes and the open runs' items moving; the `ingestion` tile "Dispatcher heartbeat age and poison messages" shows the heartbeat age below 900 s and no poison message.

## Undo

Per command, none: each records its change. A raised parallelism goes back with R03; a raised `ingestion.alwaysReady` with a pull request and R01.

## Audit record

The `ingestion.*` rows of `86-operations.md` section 2.8.6 (`ingestion.item_requeued`, `ingestion.item_failed`, `ingestion.run_cancelled`, `ingestion.rebuild_abandoned`, `ingestion.deployments_changed`, `ingestion.poison_drained`), each with its ticket; the `deploy-environment.yaml` runs of step 2 with their dispatcher and approver; the restart in the Activity Log; the ticket.
