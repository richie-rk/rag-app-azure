# R10 — Zone-redundancy conversions

**Performer:** a maintainer, in a change window, with the bootstrap operator for the Azure CLI conversions (Owner; operators hold only Reader).

**Trigger:** a decision to make a resource zone-redundant.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; a ticket recording the decision. Which settings are derived from the environment name and which are parameters is in `83-infrastructure.md` section 2.20: the storage SKUs and Cosmos DB's `isZoneRedundant` are derived, `api.zoneRedundant` and `sql.zoneRedundant` (read only in `prod`) are the only zone parameters, and the ingestion plan is never zone-redundant (ADR 0102).

Placeholders in angle brackets are filled in at run time; a value from `.azure/<env>/.env` is written by its output name.

## Steps

### Storage, LRS to ZRS

1. Read the account (the bootstrap operator):

   ```bash
   az storage account show --name <account> --resource-group rg-docchat-<env> --query "{sku: sku.name, kind: kind}"
   ```

   Expected: `Standard_LRS` and `StorageV2`.

2. Start the conversion:

   ```bash
   az storage account migration start --account-name <account> --resource-group rg-docchat-<env> --sku Standard_ZRS --no-wait
   ```

3. Daily, until the status is `Complete` (the conversion usually starts within 72 hours and has no completion time guarantee):

   ```bash
   az storage account migration show --account-name <account> --resource-group rg-docchat-<env> --migration-name default --query properties.migrationStatus
   ```

4. Align the template: `main.bicep` derives the SKU from the environment name (`Standard_ZRS` in `prod` and `load`, `Standard_LRS` in `dev`), so `prod` and `load` already declare it; for `dev`, the derivation changes by pull request. Expected: what-if then shows no change.

### Cosmos DB, the home region through a temporary region

1. Choose a temporary region with availability zones and Cosmos DB capacity, and add it zone-redundant:

   ```bash
   az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<home> failoverPriority=0 isZoneRedundant=false --locations regionName=<temporary> failoverPriority=1 isZoneRedundant=true
   ```

2. Wait for the account, then confirm the private endpoint lists the new regional endpoint:

   ```bash
   az cosmosdb show --name <account> --resource-group rg-docchat-<env> --query provisioningState
   az network private-endpoint show --name <Cosmos DB endpoint> --resource-group rg-docchat-<env> --query "customDnsConfigs[].fqdn"
   ```

   Expected: `Succeeded`, and the temporary region's endpoint among the names.

3. Fail over:

   ```bash
   az cosmosdb failover-priority-change --name <account> --resource-group rg-docchat-<env> --failover-policies <temporary>=0 <home>=1
   ```

4. Remove the home region:

   ```bash
   az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<temporary> failoverPriority=0 isZoneRedundant=true
   ```

5. Add the home region zone-redundant:

   ```bash
   az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<temporary> failoverPriority=0 isZoneRedundant=true --locations regionName=<home> failoverPriority=1 isZoneRedundant=true
   ```

6. Fail back:

   ```bash
   az cosmosdb failover-priority-change --name <account> --resource-group rg-docchat-<env> --failover-policies <home>=0 <temporary>=1
   ```

7. Remove the temporary region:

   ```bash
   az cosmosdb update --name <account> --resource-group rg-docchat-<env> --locations regionName=<home> failoverPriority=0 isZoneRedundant=true
   ```

8. Confirm the declaration (`isZoneRedundant: true`) and a what-if with no change; run the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`):

   ```bash
   gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke
   gh workflow run deployed-tests.yaml -f environment=prod -f suite=prod_smoke -f api_base_url=<API_BASE_URL> -f web_base_url=<WEB_BASE_URL> -f web_client_id=<WEB_CLIENT_ID> -f api_scope=<API_SCOPE>
   ```

   Expected: the run passes.

### App Service plan or SQL database

1. Plan only:

   ```bash
   az appservice plan show --name asp-docchat-api-<env> --resource-group rg-docchat-<env> --query properties.maximumNumberOfZones
   ```

   Greater than 1: step 2. Exactly 1: the plan sits on a scale unit without zones, can't switch in place, and a deploy with `api.zoneRedundant: true` would fail; such a plan becomes zone-redundant only by redeployment into a new resource group, which the `rg-docchat-<env>` layout doesn't provide, so `api.zoneRedundant` stays `false`, the reading goes into the runbook record, and the product owner decides through a new ADR (ADR 0102). SQL needs no check.

2. Set `api.zoneRedundant` or `sql.zoneRedundant` (read only in `prod`) to `true` in `infra/env/prod.json` by pull request and deploy with R01. Expected: both switch on in place, and with `api.zoneRedundant` the plan's instances and every autoscale minimum become at least 2.

## Verification

Storage step 3 reads `Complete` and step 4's what-if shows no change; Cosmos DB step 8; for the plan or database, `az appservice plan show --name asp-docchat-api-prod --resource-group rg-docchat-prod --query "{zoneRedundant: properties.zoneRedundant, capacity: sku.capacity}"` or `az sql db show --name docchat --server <DOCCHAT_SQL_SERVER_NAME> --resource-group rg-docchat-prod --query zoneRedundant` shows the new value after R01.

## Undo

The same procedures in reverse, by the same performers.

## Audit record

The Activity Log entries of each conversion and the pull requests.
