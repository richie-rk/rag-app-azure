# R25 — Rotate the host-account key (content-share fallback 1 only)

**Performer:** the bootstrap operator; a maintainer for the deploy.

**Trigger:** every 90 days under fallback 1, and at once on suspected exposure; in a change window with no ingestion work running, because the host can't reach its content share between steps 2 and 4. `83-infrastructure.md` section 2.7 cites this runbook by its short title "Rotate the host-account key".

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

Placeholders in angle brackets are filled in at run time. `<current release commit>` is the commit the environment serves:

```bash
az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv
```

KQL steps run through the Logs query API (`86-operations.md` section 1.5) against the environment's workspace, with the KQL and the timespan the step names:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Confirm fallback 1 applies:

   ```bash
   az storage account show --name <host account> --resource-group rg-docchat-<env> --query allowSharedKeyAccess
   ```

   Expected: `true`. Confirm nothing runs with this query (timespan `PT15M`), which returns 0:

   ```kusto
   AppMetrics | where Name == 'docchat.ingestion.items.running' | top 1 by TimeGenerated desc | project running = Sum
   ```

2. Renew the key:

   ```bash
   az storage account keys renew --account-name <host account> --resource-group rg-docchat-<env> --key key1
   ```

   `docchat-alert-key-access` fires; acknowledge it with the ticket.

3. Write the secret `ingestion-content-share-connection` from the new key with a full deploy, approved in `prod`:

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false
   ```

4. Restart the ingestion app so the platform reads the Key Vault reference once more, approved in `prod`:

   ```bash
   gh workflow run deploy-environment.yaml -f environment=<env> -f mode=restart-ingestion
   ```

   The job waits for the new worker's `ingestion_worker_started`.

5. Verify: within 10 minutes this query (timespan `PT15M`) returns a row; no host record mentions Azure Files or the content share; the next ingestion run completes.

   ```kusto
   AppTraces | where AppRoleName == 'docchat-ingestion' | summarize n = count()
   ```

6. Record the rotation date in the ticket that tracks the key.

## Verification

Step 5.

## Undo

None; the old key stops working when renewed.

## Audit record

`regenerateKey` and the provision's `listKeys` in the Activity Log and `log-docchat-security`.
