# ADR 0102: Zone redundancy

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-11-07 (PSRule suppressions for `dev`'s single-replica search service and single-zone Cosmos DB account, ADR 0106)
- **Decided by:** Product owner
- **Specified in:** 83-infrastructure.md, 81-config.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0034, 0053, 0095, 0096, 0097, 0101, 0106, 0126, 0142

## Context

Production runs both storage accounts on Standard ZRS, Cosmos DB with autoscale throughput and AI Search on Standard S1 with at least 2 replicas. Whether each production service survives the loss of one availability zone has to be settled before the first production provision, because the services differ in how zone redundancy can be turned on: for some it is a setting that changes online, for others it is a migration that only creation avoids.

Vendor facts (Microsoft Learn, checked 2026-10-14):

- **AI Search:** zone redundancy has no property; it is on automatically when the region has availability zones, the tier is Basic or higher and the service has at least 2 replicas. A service has 1 to 12 replicas. The SLA covers queries with 2 replicas and queries plus indexing with 3 or more. Scaling in can leave the remaining replicas in one zone; scaling out again rebalances them.
- **App Service:** zone redundancy needs Premium v2 to v4, `zoneRedundant: true` and a capacity of at least 2, which the platform enforces and bills; rules-based autoscale profiles then need minimum, default and maximum of at least 2. It can be switched on or off on a plan whose `maximumNumberOfZones` is greater than 1; otherwise the plan must be redeployed into a new resource group. There is no extra meter.
- **Azure SQL Database:** General Purpose zone redundancy exists in every availability-zone region on standard-series (Gen5) hardware, carries an extra charge, and is switched on or off online.
- **Cosmos DB:** `locations[].isZoneRedundant` is set per region and can't be turned on for a region already in the account: add a temporary region, fail over, remove the region and add it again with `isZoneRedundant: true`, then remove the temporary region. Serverless accounts can be zone-redundant only at creation and hold one region only. The availability-zone premium is waived for autoscale throughput.
- **Storage:** any change between LRS and ZRS is a conversion (`az storage account migration start`), which typically begins within 72 hours, has no completion SLA and no downtime for accounts without a hierarchical namespace; the next redundancy change must wait 24 hours.
- **List prices** (eastus2, USD, confirmed 2026-10-15): Blob Hot $0.0184 (LRS) and $0.023 (ZRS) per GB-month; AI Search S1 $0.336 per search unit-hour; App Service P1v3 $0.155 per hour.

## Decision

1. **Always on in `prod` and `load`, with no parameter.** `main.bicep` derives these values from `environmentName` alone:
   - data account `stdocchatdata<env><token>` and Functions host account `stdocchathost<env><token>`: `skuName` `Standard_ZRS` in `prod` and `load`, `Standard_LRS` in `dev`;
   - Cosmos DB account `cosmos-docchat-<env>-<token>`: `locations: [{locationName: <location>, failoverPriority: 0, isZoneRedundant: true}]` at creation in `prod` and `load` (autoscale), `isZoneRedundant: false` in `dev` (serverless);
   - AI Search `srch-docchat-<env>-<token>`: `standard` (S1) with `replicaCount` from `search.replicas` (integer 1 to 12; `dev` 1, `prod` 2, `load` 2), zone-redundant automatically in a region with availability zones. `main.bicep` fails the build when `prod` or `load` sets `search.replicas` below 2. `dev` runs `basic` with 1 replica and no zone redundancy.
2. **Two zone parameters, both `false` by default.** In `infra/env/<env>.json`, changed only by pull request:
   - `api.zoneRedundant` (bool) sets `zoneRedundant` on `asp-docchat-api-<env>`; the AVM `avm/res/web/serverfarm` 0.7.0 default of `true` for Premium SKUs is always overridden. When it is `true`, the plan's `skuCapacity` and every autoscale profile's `minimum` and `default` become at least 2. It needs a Premium v3 `api.sku` (`dev`'s B1 can't be zone-redundant).
   - `sql.zoneRedundant` (bool) sets `zoneRedundant` on the `docchat` database in `prod`; the `dev` and `load` databases are always `zoneRedundant: false`.
3. **Not zone-redundant.** The ingestion Elastic Premium plan `asp-docchat-ingest-<env>` keeps `zoneRedundant: false` in every environment: ingestion is queued and checkpointed work (ADR 0034), so a zone outage delays it without losing it. The shared registry is Premium and created in an availability-zone region, which makes it zone-redundant.
4. **Region check before provisioning.** Check 8 of the region preflight (ADR 0101) runs for `prod` and `load` always, and for `dev` when either zone parameter is `true`: `GET /subscriptions/{sub}/locations?api-version=2022-12-01` must return a non-empty `availabilityZoneMappings` for the environment's `location`, or the preflight fails with `availability_zones_unavailable` (exit 2) and nothing is provisioned. The default region `eastus2` has three zones.
5. **PSRule.** Suppression groups in `.ps-rule/docchat-suppressions.Rule.yaml`, each with `expiresOn: '2027-10-31T00:00:00Z'` and a `# Synopsis:` reason, cover `Azure.AppService.AvailabilityZone` for the API plan, `Azure.Search.IndexSLA` for the `prod` and `load` services (2 replicas), `Azure.Search.QuerySLA` and `Azure.Search.IndexSLA` for `dev`'s Basic service (1 replica), `Azure.Cosmos.AvailabilityZone` for `dev`'s serverless account and `Azure.Storage.UseReplication` for `dev`'s LRS data account (ADR 0106).
6. **Changing an environment that already exists** (the zone-redundancy runbook in 86-operations.md):
   - storage LRS to ZRS, for an account created with LRS: `az storage account migration start --account-name <account> --resource-group rg-docchat-<env> --sku Standard_ZRS --no-wait`, then daily `az storage account migration show --account-name <account> --resource-group rg-docchat-<env> --migration-name default --query properties.migrationStatus` until `Complete`; the template then declares `Standard_ZRS` for that environment (`prod` and `load` already do) and what-if shows no change;
   - Cosmos DB: add a temporary region with availability zones and Cosmos DB capacity at failover priority 1 with `isZoneRedundant=true`; wait for `provisioningState` `Succeeded` and for the private endpoint to list the new regional endpoint; fail over to it (`az cosmosdb failover-priority-change`); remove the home region; add it again with `isZoneRedundant=true` at priority 1; fail back; remove the temporary region; confirm what-if shows no change and run the deployed smoke test;
   - API plan or SQL: set `api.zoneRedundant` or `sql.zoneRedundant` to `true` in `infra/env/prod.json` by pull request and deploy; both switch online.

## Alternatives considered

- **A production parameter for every service, off by default.** Rejected by the product owner: Search has no switch, Cosmos DB needs the temporary-region procedure and storage a conversion with no completion SLA, so "switch it on later" would mean a migration rather than a redeploy.
- **Production storage and Cosmos DB without zone redundancy, to save cost.** Rejected by the product owner: Cosmos DB costs nothing extra under autoscale, ZRS adds $0.0046 per GB-month (25%) on data volumes of a few GB, and becoming zonal later is the migration above.
- **3 AI Search replicas, for the indexing SLA.** Rejected by the product owner: a third S1 replica adds $245.28 a month ($0.336 × 730 hours); 2 replicas carry the query SLA chat depends on, and ingestion retries a failed indexing batch per activity.
- **API plan and SQL zone-redundant by default in production.** Not adopted: SQL General Purpose zone redundancy is an extra charge, and both switch online on a running resource, so the default can stay off without a later migration.
- **`load` without zone redundancy, to save cost.** Rejected: `load` measures production's configuration (ADR 0126), and zone-redundant storage and Cosmos DB are part of it.

## Consequences

- A new `prod` environment is zonal from its first provision; it never needs a storage conversion or the Cosmos DB region procedure. `dev` stays single-zone, and because its Cosmos DB account is serverless, a zonal `dev` would need a new account.
- `prod` and `load` can run only in a region with availability zones; the preflight stops any other choice before provisioning.
- Cost in production: the ZRS premium on both storage accounts (under $1 a month at idle data volumes, where both production accounts cost $2.70); nothing extra for Cosmos DB under autoscale or for Search, whose 2 S1 replicas ($490.56 a month) production needs anyway. Setting `sql.zoneRedundant` adds General Purpose's zone charge; setting `api.zoneRedundant` adds no meter in `prod`, which already runs at least 2 instances.
- The indexing (read-write) SLA isn't covered with 2 replicas; ingestion's per-activity retries absorb a failed indexing batch. Scaling Search replicas in can leave both in one zone; scale out again to rebalance.
- `api.zoneRedundant` can be switched on in place only while the plan reports `maximumNumberOfZones` greater than 1; otherwise the plan becomes zone-redundant only by redeploying it into a new resource group, a procedure outside the zone-redundancy runbook.
- Obligations: the preflight unit test yields `availability_zones_unavailable` for an empty `availabilityZoneMappings` and `pass` otherwise; a `bicep build` of `prod` or `load` with `search.replicas` 1 fails; PSRule runs for `dev`, `prod` and `load`, and the suppressions of item 5 are renewed or removed before 2027-10-31; the zone-redundancy runbook is rehearsed and recorded like every runbook (ADR 0142). The Cosmos DB procedure can't be rehearsed in `dev`, whose serverless account is limited to one region.

## References

- AI Search reliability and zone redundancy: https://learn.microsoft.com/en-us/azure/reliability/reliability-ai-search (checked 2026-10-14)
- App Service zone redundancy: https://learn.microsoft.com/en-us/azure/app-service/configure-zone-redundancy (checked 2026-10-14)
- App Service reliability: https://learn.microsoft.com/en-us/azure/reliability/reliability-app-service (checked 2026-10-14)
- Azure SQL Database reliability: https://learn.microsoft.com/en-us/azure/reliability/reliability-sql-database (checked 2026-10-14)
- Cosmos DB zone redundancy on an account in use: https://learn.microsoft.com/en-us/azure/cosmos-db/enable-zone-redundancy (checked 2026-10-14)
- Cosmos DB reliability: https://learn.microsoft.com/en-us/azure/reliability/reliability-cosmos-db (checked 2026-10-14)
- Cosmos DB serverless (one region only): https://learn.microsoft.com/en-us/azure/cosmos-db/serverless (checked 2026-10-14)
- Storage redundancy conversion: https://learn.microsoft.com/en-us/azure/storage/common/redundancy-migration (checked 2026-10-14)
- Subscriptions - List Locations (`availabilityZoneMappings`, API 2022-12-01): https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/list-locations?view=rest-resources-2022-12-01 (checked 2026-10-21)
- Azure Retail Prices API: https://prices.azure.com/api/retail/prices (checked 2026-10-15)
