# R23 — Re-tighten role-assignment conditions

**Performer:** the bootstrap operator.

**Trigger:** a workload identity of `dev` or `prod` re-created; `dev` scanning about to turn on after a tightening that ran before the subscription's Defender scanner existed. `load` is never tightened.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

Placeholders in angle brackets are filled in at run time. `<current release commit>` is the commit the environment serves:

```bash
az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv
```

## Steps

1. Confirm the trigger: the identity's principal ID (and the same for `id-docchat-ingest-<env>`) differs from the principal ID in the deploy identity's condition:

   ```bash
   az identity show --name id-docchat-api-<env> --resource-group rg-docchat-<env> --query principalId -o tsv
   az identity show --name id-docchat-ingest-<env> --resource-group rg-docchat-<env> --query principalId -o tsv
   az role assignment list --assignee <deploy identity principal ID> --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --query "[?condition!=null].condition" -o tsv
   ```

   A provision that re-created an identity stops at that identity's role assignments, because the condition still names the old principal; that failure is the expected signal. Scanner case (`dev`): this read returns a principal the condition doesn't name; run step 2 before the pull request that sets `storage.malwareScanning` to `true` in `infra/env/dev.json` merges:

   ```bash
   az resource show --ids /subscriptions/<subscription ID>/providers/Microsoft.Security/datascanners/StorageDataScanner --query identity.principalId -o tsv
   ```

2. Tighten:

   ```bash
   uv run --frozen python scripts/bootstrap/tighten_conditions.py --env <env>
   ```

   Expected: exit 0; commit the configuration diff it prints (`principal_ids_missing`: finish the provision first; `condition_update_failed`: run it once more).

3. Provision, approved in `prod`:

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false
   ```

4. Verify:

   ```bash
   az role assignment list --all --assignee <new principal ID> --query "[].roleDefinitionName" -o tsv
   ```

   Expected: the run succeeds and the list shows the identity's roles of `83-infrastructure.md` section 6.5 or 6.6.

## Verification

Step 4.

## Undo

None; the old principal is gone.

## Audit record

The condition update in the Activity Log; `docchat-alert-role-assignments` fires and its acknowledgement names the ticket.
