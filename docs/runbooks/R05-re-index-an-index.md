# R05 — Re-index an index

**Performer:** an admin of the environment (normal path, the admin Indexes screen); an operator at level `config` (automation path, `86-operations.md` section 2.8.6).

**Trigger:** a method or parameter change; a new embedding model or version (R09); a new method version (`14-ingestion.md` section 9.5).

**Prerequisites:** normal path: membership of `sg-docchat-<env>-admins`, signed in to the web client. Automation path: the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`).

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. Room on the search service: a rebuild into a new physical index holds two physical indexes for one logical index (Basic allows 15 indexes, S1 50; `DOCCHAT_SEARCH_MAX_INDEXES` in `.azure/<env>/.env`). The start refuses when there is no room: `409 search_index_capacity` in the admin screen, exit 2 from the command. Answer it by deleting unused indexes (`adminDeleteIndex`, the admin Indexes screen) or by raising `search.sku` in `infra/env/<env>.json` through a pull request and R01.

2. Normal path: in the admin Indexes screen change the method or its parameters, then select Start re-ingest (`adminStartReingest`). Expected: the index shows its new run and, for a rebuild, its `rebuildProgress`.

3. Automation path: get the membership as R03 steps 2 and 3 (PIM activation, or a group owner's `docchat-ops jit grant --level config`, then `az logout` and `az login --tenant <tenant ID>`), then, in `prod` typing `prod` when asked:

   ```bash
   uv run docchat-ops --env <env> reindex start --index-id <uuid> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0, the run ID and the mode `same_index` or `new_physical_index`; the run starts at the dispatcher's next wake. Exit 2: the index's status doesn't allow a re-ingest, or the search service has no room (step 1); exit 4: unknown index; exit 6: someone changed the index meanwhile, so read it and rerun.

4. A faster rebuild: raise `ingestion.max_parallel_items` with R03 after R18 step 8's checks, and set the earlier value back with R03 when the run finishes.

5. Watch `rebuildProgress` on the index in the admin Indexes screen and the `ingestion` workbook (tiles "Queue age, queued and running items" and "Item outcomes by code"); at level `config`:

   ```bash
   uv run docchat-ops --env <env> ingestion status
   ```

   Expected: the run open with its items moving. Ingestion alerts follow R18.

6. To give up a rebuild while it runs:

   ```bash
   uv run docchat-ops --env <env> ingestion abandon-rebuild --index-id <uuid> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0, the building physical index `retiring`, its queued items cancelled and a deletion item queued; exit 2 means no building physical index.

7. Revoke the level, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

## Verification

The run ends `completed` (the admin Indexes screen, or `ingestion status` no longer listing it as open), the index's status is `ready`, and for a rebuild the new physical index is active and the earlier one leaves service through the `delete_physical_index` item the repoint added.

## Undo

Step 6 while the rebuild runs; a finished rebuild is undone by another re-index with the earlier method or parameters.

## Audit record

`index.reingest_started` (admin) or `ingestion.reindex_started` (`runId`, `mode`, `itemCount`, `ticket`; operator); `ingestion.rebuild_abandoned` for step 6; the membership change in Entra ID's audit log (`jit` commands write no audit row); the ticket.
