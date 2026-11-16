# R06 — User data export and deletion

**Performer:** an operator at level `user_data` (`13-conversations-and-memory.md` sections 10.8 to 10.11); a group owner of `sg-docchat-<env>-user-data-operators` (or PIM) for each grant; for part B step 1, the tenant administrator (Cloud Application Administrator, `86-operations.md` section 1.1) for the app-role assignments, a group owner of `sg-docchat-<env>-users` and `sg-docchat-<env>-admins` for the group memberships, or a User Administrator for an account deletion; for part C, also the bootstrap operator and a maintainer.

**Trigger:** a data-subject request (parts A and B); a point-in-time restore of the environment's SQL database or Cosmos DB database (part C).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL and Cosmos DB), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); an encrypted volume for every `--out` directory and for the deletion register.

Placeholders in angle brackets are filled in at run time; `<oid>` is the data subject's Entra object ID. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it, by reference only (identifiers, counts, codes and times, never content).

## Part A — Export

1. Open a ticket for the data-subject request with the user's object ID.

2. A group owner grants the level, or the operator activates it in PIM; then the operator signs in again so the token carries the membership:

   ```bash
   uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 4 --ticket <id> --reason "<reason>"
   az logout
   az login --tenant <tenant ID>
   ```

   Expected: exit 0 and the grant's receipt (exit 6: the operator is already a member).

3. Export the user's data into an empty directory on the encrypted volume:

   ```bash
   uv run docchat-ops --env <env> user export --user <oid> --out <directory on an encrypted volume> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0, the item counts and the manifest's SHA-256; `subject/`, `internal/` and `manifest.json` in the directory, each file with mode 0600. Exit 2: the directory already holds files, so choose an empty one; exit 4: no store holds data for that object ID, so check it and record the result in the ticket.

4. Deliver `subject/` through the organisation's data-request channel; keep `internal/` with the ticket. Delete both packages 30 days after the ticket closes, or at once when a later legal removal (R32) covers a document whose text a package holds.

5. Revoke, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

## Part B — Deletion

1. Remove the user's access first, because a user who can still sign in is provisioned anew by the next request. One of:
   - the tenant administrator (Cloud Application Administrator) removes the user's direct DocChat assignments, running the `DELETE` for each assignment whose `resourceDisplayName` starts with `DocChat`:

     ```bash
     az rest --method GET --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments
     az rest --method DELETE --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments/<assignment ID>
     ```

     and a group owner removes the user from both product groups:

     ```bash
     az ad group member remove --group sg-docchat-<env>-users --member-id <oid>
     az ad group member remove --group sg-docchat-<env>-admins --member-id <oid>
     ```

   - or a User Administrator deletes the account (Privileged Authentication Administrator when the account holds a directory role):

     ```bash
     az ad user delete --id <oid>
     ```

   Expected: the `GET` lists no assignment whose `resourceDisplayName` starts with `DocChat`. A `403` from Microsoft Graph means the person lacks the role the sub-step names: hand the sub-step to a holder; nothing else changed. Record the time of this step in the ticket.

2. Grant `user_data` as in part A step 2, and sign in again.

3. Delete the user's data; in `prod` type `prod` when asked:

   ```bash
   uv run docchat-ops --env <env> user delete --user <oid> --confirm <oid> --assignment-removed --ticket <id> --reason "<reason>"
   ```

   Expected: the counts it will delete, then exit 0. Exit 5: something remains; run it once more until it exits 0. Exit 2: `--confirm` differs from `--user`, or `--assignment-removed` is missing.

4. Revoke as part A step 5.

5. No sooner than 2 hours after step 1, when every access token issued before the removal has expired: grant once more as part A step 2, then

   ```bash
   uv run docchat-ops --env <env> user verify --user <oid> --ticket <id> --reason "verify deletion"
   ```

   Expected: exit 0, every count zero. Exit 5 means a request recreated data: repeat step 3, then this step. Revoke as part A step 5 and close the ticket.

The `user_data.deleted` row is the deletion-register entry that part C re-applies.

## Part C — Point-in-time restore and the deletion register

A restore keeps TTL enabled, so items past their retention period disappear from the restored stores too, and a restored store is used only after the deletion register has been applied to it (`80-data-model.md` section 3.13). Windows: SQL point-in-time restore for `sql.pitrDays` (7 days in `dev` and `load`, 14 in `prod`); Cosmos DB continuous backup for 7 days in `dev` and `load` and 30 days in `prod`.

1. Open a ticket with the incident, the stores to restore, the restore point `T` in UTC and the product owner's approval.

2. SQL, the bootstrap operator: restore the database beside the live one, then wait until it is online:

   ```bash
   az sql db restore --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat --dest-name docchat-restore-<yyyymmddhhmm> --time "<T as yyyy-MM-ddTHH:mm:ss>"
   az sql db show --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-restore-<yyyymmddhhmm> --query status -o tsv
   ```

   Expected: the second command returns `Online`.

3. Close the app, the bootstrap operator:

   ```bash
   az webapp stop --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>
   az functionapp stop --name <DOCCHAT_INGESTION_APP_NAME> --resource-group rg-docchat-<env>
   ```

   Expected: `api-down` fires; acknowledge it with the ticket and post a service notice.

4. The register, from the live database, by an operator at level `user_data` (granted as in part A step 2):

   ```bash
   umask 077
   uv run docchat-ops --env <env> user deletion-register --since <T as yyyy-MM-ddTHH:mm:ssZ> --ticket <id> --reason "deletion register for restore" --json > <encrypted volume>/register-<id>.json
   ```

   Expected: exit 0 and every `user_data.deleted` and `document.traces_redacted` row since `T`, oldest first. Exit 2: the time is malformed or in the future.

5. SQL switch, the bootstrap operator. In `prod`, first remove the database's lock:

   ```bash
   az lock list --resource-group rg-docchat-prod --query "[?contains(id, '/databases/docchat/')].id" -o tsv
   az lock delete --ids <lock ID>
   ```

   Then swap the names:

   ```bash
   az sql db rename --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat --new-name docchat-replaced-<yyyymmddhhmm>
   az sql db rename --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-restore-<yyyymmddhhmm> --new-name docchat
   ```

   Expected: `az sql db show --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat --query status -o tsv` returns `Online`.

6. Cosmos DB, only when the incident deleted the database `docchat` or one of its containers: the bootstrap operator restores it into the same account with Azure CLI 2.58.0 or later, the database before its containers and at most three restores at once, never with `--disable-ttl`:

   ```bash
   az cosmosdb sql database restore --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --name docchat --restore-timestamp <T as yyyy-MM-ddTHH:mm:ssZ>
   az cosmosdb sql container restore --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <conversations|memories|usage> --restore-timestamp <T as yyyy-MM-ddTHH:mm:ssZ>
   az cosmosdb sql container show --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <container> --query name -o tsv
   ```

   Expected: the last command returns the container's name for each restored container. Items changed or deleted inside a live container are recovered only by restoring the whole account into a new account, which no template connects to the app: that case goes to the product owner as a new ADR and this runbook stops here, with the app reopened by step 9.

7. Re-apply the register, the operator at level `user_data`, for each row of step 4:
   - `user_data.deleted`: confirm with part B step 1's Graph `GET` that the user holds no DocChat assignment, then, until it exits 0:

     ```bash
     uv run docchat-ops --env <env> user delete --user <subject_object_id> --confirm <subject_object_id> --assignment-removed --ticket <id> --reason "deletion register after restore"
     ```

   - `document.traces_redacted`: until it exits 0:

     ```bash
     uv run docchat-ops --env <env> user redact-document --document-id <target_id> --ticket <id> --reason "deletion register after restore"
     ```

     When the restored database lists that document, an admin deletes it in the Documents screen after step 9.

8. Verify, for each user (when the row is younger than 2 hours, wait until it is 2 hours old) and each document:

   ```bash
   uv run docchat-ops --env <env> user verify --user <oid> --ticket <id> --reason "verify deletion after restore"
   uv run docchat-ops --env <env> user redact-document --document-id <target_id> --ticket <id> --reason "verify redaction after restore"
   ```

   Expected: each `verify` exits 0, and each further `redact-document` exits 0 reporting `traceCount` 0. Revoke the level as part A step 5.

9. Reopen, the bootstrap operator:

   ```bash
   az webapp start --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>
   az functionapp start --name <DOCCHAT_INGESTION_APP_NAME> --resource-group rg-docchat-<env>
   ```

   A maintainer then dispatches the deploy of the release the environment serves, approved in `prod` (`86-operations.md` section 1.5, "Approving a waiting deployment"):

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false
   ```

   Its provision re-applies the database's and containers' declared settings and diagnostic settings, and its migrations bring a restored schema to `head` (calls that need a newer column fail until they finish). Expected: the run succeeds and `api-down` resolves. In `prod` the bootstrap operator then restores the lock:

   ```bash
   uv run --frozen python scripts/bootstrap/lock_prod.py
   ```

   Expected: exit 0.

10. After a SQL restore the ingestion ledger is at `T` while the search indexes and blobs are current: admins re-ingest every index that changed after `T` (R05).

11. The bootstrap operator deletes the replaced database once the ticket closes, at most 7 days later:

    ```bash
    az sql db delete --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --name docchat-replaced-<yyyymmddhhmm> --yes
    ```

## Verification

Part A: the recipient confirms delivery through the ticket, and `jit list` shows the operator no longer in the `user_data` group. Part B: step 5's `user verify` exits 0. Part C: the deploy run's tests pass and every step 8 check exited 0.

## Undo

Parts A and B: none; a deletion can't be undone, and an export is deleted with its ticket (part A step 4). Part C: until step 11, steps 3 and 5 with the two database names swapped back.

## Audit record

Part A: one `user_data.exported` row (`ticket`, `counts`, `manifestSha256`). Part B: one `user_data.deleted` row (`ticket`, `counts`), the deletion-register entry; `user verify` writes none. Part C: one `user_data.deleted` row per re-applied user and one `document.traces_redacted` row per document; the restore, rename, lock, stop and start events in the Activity Log. Every part: the just-in-time grants and revocations in Entra ID's audit log (and `docchat-alert-jit-grant`), the `human-conversation-access` alert raised by the operator's Cosmos DB requests, and the ticket.
