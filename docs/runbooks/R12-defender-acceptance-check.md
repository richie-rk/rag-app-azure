# R12 — Defender acceptance check

**Performer:** an operator; at level `config` for step 9 (R03 steps 2 and 3); the bootstrap operator for step 10's lock change.

**Trigger:** the first provision of an environment with malware scanning on (`prod` by default); any re-creation of the data storage account; the alert `uploads-scan-gate-closed`. `dev` scans only while the deployed malware-scanning test runs: the stage-2 acceptance performer sets `storage.malwareScanning` to `true` in `infra/env/dev.json` with one pull request and back to `false` with a second as soon as the suite ends (`81-config.md` section 4.2), after R23's scanner case.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); Reader on the environment's resource group and Log Analytics Reader on its workspace through `sg-docchat-operators`; for step 9 a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL).

Placeholders in angle brackets are filled in at run time. `<data storage account ID>` is `az storage account show --name <DOCCHAT_STORAGE_ACCOUNT_NAME> --resource-group rg-docchat-<env> --query id -o tsv`. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy.

KQL steps run through the Logs query API (`86-operations.md` section 1.5) against the environment's workspace, with the timespan the step names:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Confirm the setting:

   ```bash
   az rest --method GET --url "https://management.azure.com<data storage account ID>/providers/Microsoft.Security/defenderForStorageSettings/current?api-version=2025-06-01" --query "properties.malwareScanning"
   ```

   Expected: `onUpload.isEnabled: true`.

2. Wait for the first canary: the 02:40 UTC maintenance writes one daily, and the first sweep writes one when none exists; Defender can take up to 24 hours on a new account.

3. Read the canary results (timespan `P2D`):

   ```text
   az rest … AppEvents | where Name == 'scanner_canary_result' | top 5 by TimeGenerated desc | project TimeGenerated, result = tostring(Properties['result']), minutes = toint(Properties['minutes'])
   ```

   Expected: the newest row shows `clean`.

4. Defender's own record (timespan `P2D`):

   ```text
   az rest … StorageMalwareScanningResults | where TimeGenerated > ago(2d) | take 20
   ```

   Expected: the canary blob is listed.

5. An admin's upload preflight in the Documents screen succeeds (no `503 malware_scanning_not_ready`).

6. Log the acceptance in `docs/runbooks/rehearsals.md` with the canary's time; for a first provision, also in the R22 record.

7. No result after 24 hours: re-read step 1's `operationStatus` (`--query "properties.malwareScanning.operationStatus"`), check that the scanner's resource-instance rule exists:

   ```bash
   az storage account show --name <DOCCHAT_STORAGE_ACCOUNT_NAME> --resource-group rg-docchat-<env> --query networkRuleSet.resourceAccessRules
   ```

   and open a Microsoft support case; uploads stay closed.

8. Monthly cap: when the month's uploads reach `storage.malwareScanCapGbPerMonth` (timespan `P31D`):

   ```text
   az rest … AppMetrics | where TimeGenerated > startofmonth(now()) and Name == 'docchat.uploads.size' | summarize gb = sum(Sum) / 1e9
   ```

   Defender stops scanning until the month ends: new versions fail closed as `scan_unavailable`, the canary gets no clean result and `uploads-scan-gate-closed` fires. Raise the cap in `infra/env/<env>.json` by pull request and R01, then admins re-upload the versions that failed as `scan_unavailable`.

9. Uploads closed after acceptance (`503 malware_scanning_not_ready`, `uploads-scan-gate-closed`): read step 3's query, and at level `config` (R03 steps 2 and 3):

   ```bash
   uv run docchat-ops --env <env> ingestion status
   ```

   Expected: the last 5 canaries. `unavailable` points at Defender (step 7) or the monthly cap (step 8); `failed` at the scanner's error value. The gate opens by itself at the first clean canary. Revoke the level afterwards (`docchat-ops jit revoke`, R03 step 8).

10. Turning `prod` scanning off is a product owner decision recorded in an ADR. When it is made, the bootstrap operator removes the data account's `docchat-protect` lock:

    ```bash
    az lock list --resource-group rg-docchat-prod
    az lock delete --ids <lock ID>
    ```

    the pull request setting `storage.malwareScanning` to `false` deploys with R01, and in the same session:

    ```bash
    uv run --frozen python scripts/bootstrap/lock_prod.py
    ```

    exits 0.

## Verification

Step 3 shows a `clean` canary newer than the switch-on, step 4 lists the canary blob, and step 5's upload preflight succeeds; afterwards `uploads-scan-gate-closed` stays silent.

## Undo

None needed; the check changes nothing. Turning scanning off again is step 10 in `prod`, and the second pull request of the stage-2 window in `dev`.

## Audit record

The rehearsal log entry in `docs/runbooks/rehearsals.md` (and the R22 record for a first provision); the canary rows in `app.scan_canaries`.
