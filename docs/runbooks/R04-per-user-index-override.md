# R04 — Per-user index override

**Performer:** an operator at level `config` (`86-operations.md` section 2.8.3).

**Trigger:** a ticket to test an index against one user's live questions; the alert `index-override-used` (a daily list, `prod` only).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); Log Analytics Reader on the environment's workspace through `sg-docchat-operators`.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

KQL steps run through the Logs query API (`86-operations.md` section 1.5) against the environment's workspace, with the timespan `PT1H` unless the step names another:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Open a ticket with the user's object ID, the project ID, the index ID, the number of days (1 to 30) and the reason. Get the membership as R03 steps 2 and 3: PIM activation, or a group owner runs

   ```bash
   uv run docchat-ops --env <env> jit grant --level config --member <operator object ID> --hours 1 --ticket <id> --reason "<reason>"
   ```

   and the operator signs in again:

   ```bash
   az logout
   az login --tenant <tenant ID>
   ```

2. Set the override:

   ```bash
   uv run docchat-ops --env <env> override set --user <oid> --project <projectId> --index <indexId> --days <n> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0 and the expiry; the override applies to the user's next chat request. Exit 2: the index isn't mapped to the project, so an admin maps it in the Projects screen first (or `--days` is outside 1 to 30); exit 4: the user hasn't signed in yet, so no `users` row exists.

3. After the user's next turn, check that chat resolved the index through the override:

   ```text
   az rest … AppDependencies | where Name == 'docchat.access.resolve_index' and tostring(Properties['docchat.access.index_source']) == 'override' | summarize n = count()
   ```

   Expected: `n` at least 1. Then:

   ```bash
   uv run docchat-ops --env <env> override list --user <oid> --ticket <id> --reason "verify the override"
   ```

   Expected: the row with the project, the index, your object ID as `set_by`, the expiry and `expired` false.

4. To end it early:

   ```bash
   uv run docchat-ops --env <env> override clear --user <oid> --project <projectId> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0; exit 4 means no row. Otherwise the override lapses at its expiry, and chat ignores the expired row.

5. Revoke the level, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

6. On `index-override-used`: for each user, project and index in the `access` tile "Index overrides in use", run `override list --user <oid>` (with the membership of step 1) and match the row to its ticket. An override without a ticket is cleared at once with step 4 and starts R07.

## Verification

Step 3: the query returns at least one override resolution after the user's turn, and `override list` shows the row unexpired. After step 4 or the expiry, `override list` shows no row (or `expired` true) and the `access` tile "Index overrides in use" lists no new use.

## Undo

`override clear` (step 4).

## Audit record

`user_index_override.set` (`searchIndexId`, `expiresAt`, `replaced`, `ticket`) or `user_index_override.cleared` (`searchIndexId`, `ticket`); the membership change in Entra ID's audit log (`jit` commands write no audit row); the ticket.
