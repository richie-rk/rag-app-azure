# ADR 0103: Content share spike and fallback 1

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 90-build-order.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0005, 0053, 0098, 0109, 0113, 0115, 0116, 0123, 0125, 0141

## Context

Ingestion runs on a Linux Elastic Premium plan with a custom container, Durable Functions on the Azure Storage provider with identity-based connections, public network access off and runtime scale monitoring on (ADR 0005). Microsoft's documentation requires an Azure Files content share for most Elastic Premium apps, and a share's connection string carries a storage account key. Every other connection in this product uses a managed identity, and key access is off on every resource, so a share would be the only key in the system. Whether the ingestion app runs and scales without a share can only be proven on the platform, so stage 0 of the build order runs a spike, and the fallback is chosen before the spike runs so that a failure doesn't stop the build for a design decision. Whichever way the spike goes, the result stays inside the templates.

Vendor facts (Microsoft Learn, checked 2026-10-14):

- `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` is required for Consumption and Elastic Premium and doesn't support managed identity ("Azure Files doesn't currently support using managed identity"); it may be a Key Vault reference. `WEBSITE_CONTENTSHARE` is required for Consumption and Premium. `WEBSITE_SKIP_CONTENTSHARE_VALIDATION=1` skips validation, after which an invalid share or connection gives HTTP 500.
- Container deployments on Premium set `linuxFxVersion` and `WEBSITES_ENABLE_APP_SERVICE_STORAGE=false` "since your app content is provided in the container itself", and Microsoft's container template omits the content-share settings. A share on network-restricted storage needs an explicit `WEBSITE_CONTENTSHARE`, a `fileServices/shares` resource and content-share routing through the network (`outboundVnetRouting.contentShareTraffic`), without which scaling stays within one scale unit of about 1 to 20 instances.
- Running Premium without Azure Files is documented for zip packages (`WEBSITE_RUN_FROM_PACKAGE` on a private blob), with the warning that scaling "could be limited"; `WEBSITE_RUN_FROM_PACKAGE` must not be used with containers. Scale-out beyond the always-ready instances is best effort.
- A Linux app that reads Key Vault references from a vault behind a private endpoint must route all outbound traffic through the network (`outboundVnetRouting.allTraffic`).
- Functions on Container Apps autoscales Durable Functions only with the MSSQL or Durable Task Scheduler storage providers and needs no content share.
- `Microsoft.Network/natGateways@2025-09-01`: `sku.name` `Standard` or `StandardV2`, `idleTimeoutInMinutes`, `publicIpAddresses`, `zones`. List prices (eastus2, confirmed 2026-10-15): NAT gateway $0.045 per hour plus $0.045 per GB processed; Standard static public IPv4 $0.005 per hour; private endpoint $0.01 per hour; private DNS zone $0.50 per month.

## Decision

1. **The spike (stage 0).** Stage 0 deploys the `dev` ingestion app exactly as ADR 0005 specifies, with no Azure Files share, no `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` and no `WEBSITE_CONTENTSHARE`, running the stage-0 spike build of the ingestion image (module `docchat_ingestion.spike`: a queue-triggered starter and a Durable orchestration that fans out 200 activities of 30 seconds each, with `maxConcurrentActivityFunctions` 1). The spike also deploys the image with `azd deploy ingestion --from-package <digest>` (ADR 0123) and creates the ingestion identity's SQL contained user from its client ID (ADR 0125).
2. **Pass criteria, all seven required:** (1) provisioning succeeds with no content-share validation error; (2) Application Insights receives the spike worker's start-up event (`ingestion_worker_started` in the real image) within 10 minutes of the deploy; (3) a message on `ingestion-requests` starts the orchestration within 2 minutes; (4) during the fan-out the app runs on 2 instances (two distinct `cloud_RoleInstance` values) within 10 minutes, and returns to 1 within 70 minutes of the last activity; (5) the orchestration completes; (6) 24 hours pass with no host error that mentions Azure Files, the content share or `AzureWebJobsStorage`; (7) the image pull succeeds with `acrUserManagedIdentityID` set to the identity's client ID.
3. **Decision rule.**
   - (1) to (6) pass: no share is created, and the spike's results are recorded in this ADR.
   - Any of (1) to (6) fails, scale-out (4) included: fallback 1 (item 4) applies at once, without a new decision, and criteria (1) to (6) run again against it.
   - (4) still fails with fallback 1 deployed: the build stops, and fallback 2 (Functions on Container Apps with the MSSQL Durable provider) goes to the product owner as a new ADR.
   - (7) fails with an identity error: both apps set `acrUserManagedIdentityID` to the identity's resource ID, and the result is recorded in this ADR.
4. **Fallback 1, chosen now: keep Elastic Premium and add a content share.** The exact changes (runbook R27):
   - Host account `stdocchathost<env><token>`: `allowSharedKeyAccess: true`, the single exception to "no keys"; a `file` private endpoint in `snet-private-endpoints` with the private DNS zone `privatelink.file.core.windows.net` linked to the environment's network; the share `func-docchat-ingest-<env>` (`fileServices/default/shares`, quota 100 GiB).
   - Key Vault `kv-docchat-<env>-<token>`: the secret `ingestion-content-share-connection` with the value `DefaultEndpointsProtocol=https;AccountName=<host account>;AccountKey=<listKeys().keys[0].value>;EndpointSuffix=core.windows.net`, written by Bicep from `listKeys()`; the ingestion identity `id-docchat-ingest-<env>` gets Key Vault Secrets User on that secret only.
   - Function app: `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` = `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/ingestion-content-share-connection)`, `WEBSITE_CONTENTSHARE` = `func-docchat-ingest-<env>`, `WEBSITE_SKIP_CONTENTSHARE_VALIDATION` = `1`, `keyVaultReferenceIdentity` = the ingestion identity's resource ID, `outboundVnetRouting.contentShareTraffic: true` and `outboundVnetRouting.allTraffic: true`.
   - Outbound path: because all of the app's outbound traffic then enters the network, its internet-bound calls get an explicit egress that the template declares: a NAT gateway `ng-docchat-<env>` (`Microsoft.Network/natGateways@2025-09-01`, `sku.name: 'Standard'`, `idleTimeoutInMinutes: 4`, no zone) with one Standard static public IP `pip-docchat-<env>-nat` (`Microsoft.Network/publicIPAddresses@2025-09-01`, `sku {name: 'Standard', tier: 'Regional'}`, `publicIPAllocationMethod: 'Static'`, `publicIPAddressVersion: 'IPv4'`), associated with `snet-ingest-integration` only. The subnet's network security group keeps the platform's default internet-outbound rule, so Microsoft Entra ID and Azure Monitor ingestion stay reachable through the NAT gateway's address. The API subnet is unchanged.
   - Policy: before the deploy, the bootstrap operator adds the host account to the `notScopes` of the Deny assignment `docchat-deny-storage-shared-key` on `rg-docchat-<env>` (policy exemptions have no GA API, ADR 0116).
   - Rotation (runbook R25): every 90 days and at once on suspected exposure, in a window with no ingestion work running: `az storage account keys renew --key key1`, a `full` deploy writes the secret from the new key, the ingestion app restarts through the environment's deploy identity (`deploy-environment.yaml`, `mode: restart-ingestion`), and the next ingestion run must complete.
5. **Fallback 2 isn't chosen in advance.** It changes the hosting resource, the Durable storage provider and the ingestion scaling model (ADR 0053), while fallback 1 confines the change to one storage account, one secret and the ingestion subnet's outbound path.

## Alternatives considered

- **Fallback 2 chosen now.** Rejected: moving the task hub to SQL and the app to Container Apps reworks ingestion's runtime, scaling and operations for a risk the spike may not confirm.
- **Waiting for the spike result before choosing a fallback.** Rejected: a failed spike would stop stage 0 for a design decision; choosing now turns any failure of (1) to (6) into a known change set.
- **A share whose connection string is a plain app setting.** Rejected: the key would sit in site configuration readable by anyone with configuration read rights; every secret this product holds lives in Key Vault behind a Key Vault reference.
- **Content-share routing without routing all traffic.** Rejected: a Linux app can't resolve a Key Vault reference from a vault behind a private endpoint unless all outbound traffic is routed through the network.
- **Running from a blob package (`WEBSITE_RUN_FROM_PACKAGE`).** Rejected: the ingestion app is a custom container, and Microsoft says not to combine that setting with containers.

## Consequences

- If the spike passes, the "no keys" rule holds with no exception, and the share resources never exist.
- Under fallback 1, one storage key exists, on the Functions host account only (never on the data account), read by Bicep into one Key Vault secret; the shared-key Deny policy exempts only that account. Writing the secret calls `Microsoft.Storage/storageAccounts/listKeys/action` on every provision of that environment, which the security alert `docchat-alert-key-access` reports (ADR 0115), so while fallback 1 is active the security action group gets one alert per provision; runbook R20 routes a security alert that a pipeline run explains to R07 (incident response), and an unexplained one to R31 (suspected pipeline compromise).
- Under fallback 1, all of the ingestion app's outbound traffic, Microsoft Entra ID and Azure Monitor included, leaves through one static address, which a later egress filter can use (ADR 0109). Each environment's idle cost grows by about $44.30 a month (NAT gateway $32.85, public IP $3.65, `file` private endpoint $7.30, DNS zone $0.50), plus $0.045 per GB through the NAT gateway and the share's used capacity.
- Key rotation needs a window with no running ingestion work, because the host can't reach its share between renewing the key and restarting the app.
- Obligations: stage 0's acceptance (90-build-order.md) runs the seven criteria and records the results here; after fallback 1, PSRule and what-if pass, the deployed `smoke` (`dev`) or `prod_smoke` (`prod`) passes, an ingestion run completes, and `az network vnet subnet show --name snet-ingest-integration --query natGateway.id` names `ng-docchat-<env>` (runbook R27, step 4).

## References

- Elastic Premium plan, instances and scaling: https://learn.microsoft.com/en-us/azure/azure-functions/functions-premium-plan (checked 2026-10-14)
- Functions app settings (content share settings): https://learn.microsoft.com/en-us/azure/azure-functions/functions-app-settings (checked 2026-10-14)
- Infrastructure as code for Premium plans: https://learn.microsoft.com/en-us/azure/azure-functions/functions-infrastructure-as-code?pivots=premium-plan (checked 2026-10-14)
- Storage considerations for Functions (running without Azure Files): https://learn.microsoft.com/en-us/azure/azure-functions/storage-considerations (checked 2026-10-14)
- Custom containers on Functions: https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-custom-container (checked 2026-10-14)
- Key Vault references in App Service and Functions: https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references (checked 2026-10-14)
- Functions on Container Apps: https://learn.microsoft.com/en-us/azure/container-apps/functions-overview (checked 2026-10-14)
- NAT gateway resource (API 2025-09-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.network/natgateways (checked 2026-10-14)
- Azure Retail Prices API: https://prices.azure.com/api/retail/prices (checked 2026-10-15)
