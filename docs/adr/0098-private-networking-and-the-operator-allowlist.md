# ADR 0098: Private networking and the operator allowlist

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-10-30 (the test vault's default runner rule and the optional CI network, ADR 0122); amended 2026-11-07 (the private-endpoint group denies outbound traffic to the network too, for PSRule's `Azure.NSG.LateralTraversal`, ADR 0106)
- **Decided by:** Product owner and architect
- **Specified in:** 00-overview.md, 81-config.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 86-operations.md, CONTEXT.md
- **Related ADRs:** 0017, 0024, 0095, 0101, 0103, 0104, 0106, 0109, 0118, 0119, 0122

## Context

SQL and Cosmos DB hold users, projects, conversations and memory, so the product owner requires them to be reachable only from the environment's virtual network and from a named list of operator IP addresses. The same rule applies to every other resource that holds or processes the documents: the data storage account, AI Search, Azure OpenAI, Content Safety, Document Intelligence, Key Vault and the container registry. The trust boundaries are: the internet reaches the static web client (files only) and the API, the only public entry point, guarded by platform authentication and in-app token validation; everything else sits behind private endpoints plus that allowlist; the ingestion app accepts no inbound traffic. Network isolation is the second wall behind managed identities and disabled local authentication (ADR 0106). Egress filtering and private ingestion of monitoring data are later hardening (ADR 0109).

Vendor facts (checked 2026-10-14 and 2026-10-15):

- With route-all off, VNet integration sends only RFC 1918 and service-endpoint traffic into the network, and network security groups govern only that traffic; image pulls use the public route unless `outboundVnetRouting.imagePullTraffic` is on (the property that took over from `vnetImagePullEnabled` in API 2024-11-01). A plan uses one subnet address per instance, doubled during scale, and plans can't share a subnet.
- Linux Elastic Premium needs at least a /28 and recommends a /26; a /24 gives 251 usable addresses, about 125 instances, and one app is capped at 100 instances. Scaling from a network-secured queue needs `functionsRuntimeScaleMonitoringEnabled: true`.
- An app's `publicNetworkAccess` is evaluated before its access rules and covers the main and SCM sites; the SCM site has its own rules and unmatched-rule action; private-endpoint traffic isn't subject to the rules.
- Storage IP rules take single public IPv4 addresses only (no RFC 1918 ranges, no /31 or /32), have no effect on clients in the account's own region, and apply only with `defaultAction: 'Deny'`; trusted-service access takes the highest precedence over other restrictions; resource-instance rules stay in effect after public access is set to `Disabled`, and a template that sets `Disabled` must also set `defaultAction: 'Deny'`.
- Cosmos DB `ipRules` refuse private ranges; AI Search's `networkRuleSet` takes IP rules only and applies only while public access is enabled; the AI services accounts take `networkAcls` IP rules and `restrictOutboundNetworkAccess` with `allowedFqdnList`.
- A change to the SQL firewall can take up to five minutes to take effect.
- New subnets created with network API versions released after 31 March 2026 default to `defaultOutboundAccess: false`; delegated subnets keep the outbound path their service manages.
- A private endpoint costs $0.01 an hour and a private DNS zone $0.50 a month at eastus2 list prices.

## Decision

1. **One network per environment.** `vnet-docchat-<env>` (AVM `network/virtual-network` 0.10.2), Azure-provided DNS, no peering, no DDoS Network Protection plan; subnets derived with `cidrSubnet()` from the environment-file field `network.addressSpace`:

   | Environment | Address space | `snet-api-integration` | `snet-ingest-integration` | `snet-private-endpoints` | Fourth /24 |
   |---|---|---|---|---|---|
   | `dev` | 10.61.0.0/22 | 10.61.0.0/24 | 10.61.1.0/24 | 10.61.2.0/24 | 10.61.3.0/24 reserved |
   | `prod` | 10.62.0.0/22 | 10.62.0.0/24 | 10.62.1.0/24 | 10.62.2.0/24 | 10.62.3.0/24 reserved |
   | `load` | 10.63.0.0/22 | 10.63.0.0/24 | 10.63.1.0/24 | 10.63.2.0/24 | `snet-stub-integration` 10.63.3.0/26; 10.63.3.64 to 10.63.3.255 reserved |

   The integration subnets and the stub subnet are delegated to `Microsoft.Web/serverFarms`, one plan each. `snet-private-endpoints` has no delegation, `privateEndpointNetworkPolicies: 'Enabled'` and `defaultOutboundAccess: false`. No two ranges overlap, so the networks can later be peered to a hub without renumbering, and the reserved ranges hold a later NAT gateway, firewall or in-network runner subnet.
2. **Network security groups**, one per subnet (`Microsoft.Network/networkSecurityGroups@2025-09-01`), deny by default inside the network; the platform's default rules (load-balancer probes, outbound internet) stay:

   | Group (subnet) | Rules |
   |---|---|
   | `nsg-docchat-<env>-pe` | inbound 100 `allow-apps-https`: TCP 443 from the integration subnets (and the stub subnet in `load`); inbound 110 `allow-apps-sql`: TCP 1433 and 11000–11999 from the API and ingestion subnets; outbound 4000 `deny-vnet-outbound` (private endpoints originate nothing, so it changes no path); inbound 4000 `deny-vnet-inbound` |
   | `nsg-docchat-<env>-api`, `nsg-docchat-<env>-ingest` | outbound 100 `allow-private-endpoints`: TCP 443, 1433 and 11000–11999 to `snet-private-endpoints`; outbound 4000 `deny-vnet-outbound`; inbound 4000 `deny-vnet-inbound` |
   | `nsg-docchat-load-stub` | outbound 100 `allow-private-endpoints-https`: TCP 443 to `snet-private-endpoints`; outbound 4000 `deny-vnet-outbound` |

3. **Private endpoints and zones.** Every endpoint (AVM `network/private-endpoint` 0.12.1) sits in `snet-private-endpoints`, is named `pe-<target resource name>-<group ID>`, is approved automatically and has a DNS zone group named `default`. The zones (AVM `network/private-dns-zone` 0.8.1) live in the environment's resource group, linked only to its network (`link-vnet-docchat-<env>`, `registrationEnabled: false`):

   | Target | Group ID | Private DNS zone |
   |---|---|---|
   | SQL server | `sqlServer` | `privatelink.database.windows.net` |
   | Cosmos DB account | `Sql` | `privatelink.documents.azure.com` |
   | Data storage | `blob`, `queue` | `privatelink.blob.core.windows.net`, `privatelink.queue.core.windows.net` |
   | Functions host storage | `blob`, `queue`, `table` | the blob and queue zones, `privatelink.table.core.windows.net` |
   | AI Search | `searchService` | `privatelink.search.windows.net` |
   | Azure OpenAI | `account` | `privatelink.openai.azure.com` |
   | Content Safety, Document Intelligence | `account` each | `privatelink.cognitiveservices.azure.com` |
   | Key Vault | `vault` | `privatelink.vaultcore.azure.net` |
   | Shared registry | `registry` | `privatelink.azurecr.io` (the regional data-endpoint records go in the same zone) |
   | Model stub site (`load`) | `sites` | `privatelink.azurewebsites.net` |

   That is 13 endpoints and 10 zones in every environment: `load` has no Azure OpenAI endpoint or zone and adds the stub's. Every service is called on its public host name (a custom subdomain for each AI service), which the linked zones resolve to a private address inside the network. The registry endpoint is approved automatically because each deploy identity holds the approval action on the registry (ADR 0118).
4. **Public access and the operator allowlist.** The allowlist is the Bicep parameter `operatorIpAllowlist`, read by `main.bicepparam` from the GitHub environment variable `DOCCHAT_OPERATOR_IP_ALLOWLIST` (comma-separated single public IPv4 addresses, at most 20; set per GitHub environment and in the local azd environment; never in the repository). The shared registry and the shared test vault use `sharedOperatorIpAllowlist` from `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST`, applied by the bootstrap's `shared` step. Every resource sets `bypass: 'None'` or its equivalent: no trusted-service exception and no `AllowAllWindowsAzureIps` rule anywhere.

   | Resource | Allowlist non-empty | Allowlist empty |
   |---|---|---|
   | SQL server | public access enabled; one firewall rule `operator-<n>` per address (start = end); no other standing rule | enabled with no standing rule, so only a transient runner rule can open it (ADR 0104) |
   | Cosmos DB | enabled, `ipRules` = allowlist | `publicNetworkAccess: 'Disabled'` |
   | Data storage | enabled, `defaultAction: 'Deny'`, `ipRules` = allowlist, plus Defender's resource-instance rule | disabled, `defaultAction: 'Deny'`; the resource-instance rule stays declared and effective |
   | Functions host storage | disabled | disabled |
   | AI Search | enabled, `ipRules` = allowlist | disabled |
   | Azure OpenAI, Content Safety, Document Intelligence | enabled, `defaultAction: 'Deny'`, `ipRules` = allowlist | disabled |
   | Key Vault | enabled, `defaultAction: 'Deny'`, `ipRules` = allowlist | disabled |
   | Shared registry | enabled, `defaultAction: 'Deny'`, `ipRules` = shared allowlist | enabled, `defaultAction: 'Deny'`, no rule |
   | Shared test vault | enabled, `defaultAction: 'Deny'`, `ipRules` = shared allowlist | by default enabled, `defaultAction: 'Deny'`, no rule, so only a test job's transient runner rule opens it; under the private-networking option disabled, reachable only through its private endpoint in the CI network (ADR 0122) |

5. **Transient runner rules are the only other opening.** Inside one GitHub Actions job, the job's own identity adds and then removes one rule for the runner's public address on three resources only: the environment's SQL server (rule `ci-runner-<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>`, for the provision job's migrations and grants, in `dev` for the deployed infrastructure suite's `infra-checks` job and, in `load`, for the `seed-config`, `stream-reset`, `s4-set1-config`, `s4-set2-config` and `s4-reset` jobs, under that environment's deploy identity) the shared registry (an IP rule, for the `publish` and `resolve` jobs and the nightly `trivy-rescan` job, under the `dev` deploy identity only) and, by default, the shared test vault (an IP rule for each read window of a `dev-tests` or `load-tests` job, under `id-docchat-citest`; ADR 0122). The removal runs in a step with `if: always()` (in the azd `postprovision` hook and the infrastructure suite's tests, a `finally` block), and the Activity Log records both writes (ADR 0119). No other resource ever takes a runner rule.
6. **The apps.** The API web app: `publicNetworkAccess: 'Enabled'`, the only internet-facing endpoint, behind platform authentication and in-app token validation (ADR 0017); `ipSecurityRestrictionsDefaultAction: 'Allow'` with no rules; `scmIpSecurityRestrictionsDefaultAction: 'Deny'` with no rules and `scmIpSecurityRestrictionsUseMain: false`, so the deployment (SCM) site accepts no traffic; `virtualNetworkSubnetId` = `snet-api-integration`. The ingestion function app: `publicNetworkAccess: 'Disabled'`, with `ipSecurityRestrictionsDefaultAction` and `scmIpSecurityRestrictionsDefaultAction` both `'Deny'`, so it accepts no inbound traffic of any kind; `virtualNetworkSubnetId` = `snet-ingest-integration`; `functionsRuntimeScaleMonitoringEnabled: true`, so the platform scales it from the private queue. Both set `outboundVnetRouting {allTraffic: false, applicationTraffic: false, imagePullTraffic: true, contentShareTraffic: false, backupRestoreTraffic: false}` and pull their image through the registry's private endpoint with `acrUseManagedIdentityCreds: true` and `acrUserManagedIdentityID` = their user-assigned identity's client ID. The model stub app (`load`) has public access disabled and is reached through its `sites` endpoint. The Static Web App stays public (static files only); Log Analytics and Application Insights keep public ingestion and query.
7. **Outbound.** Private-endpoint calls (RFC 1918) go through VNet integration; everything else (Microsoft Entra ID token and signing-key endpoints, the managed-identity endpoint, Azure Monitor ingestion) leaves through the platform's default outbound path with the plan's outbound addresses. No NAT gateway or firewall exists, except the NAT gateway that content-share fallback 1 adds to `snet-ingest-integration` (ADR 0103). Azure OpenAI, Content Safety and Document Intelligence set `restrictOutboundNetworkAccess: true` with an empty `allowedFqdnList`, so they make no outbound calls.
8. **Allowlist validation.** The preflight (ADR 0101) fails with `allowlist_invalid` unless every entry is a dotted-quad IPv4 address with no prefix length, outside 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 127.0.0.0/8 and 169.254.0.0/16, and the list holds at most 20 entries.
9. **The shared CI network, under the private-networking option only.** Declared only when the bootstrap key `ciPrivateNetworking` is `true` (ADR 0122), `vnet-docchat-ci` in `rg-docchat-shared` uses 10.60.0.0/24, overlaps no environment and is never peered: `snet-ci-runners` 10.60.0.0/26, delegated to `GitHub.Network/networkSettings` with `defaultOutboundAccess: true` set explicitly; `snet-ci-private-endpoints` 10.60.0.64/27, holding only the test vault's private endpoint, whose zone `privatelink.vaultcore.azure.net` links only to this network; 10.60.0.96 to 10.60.0.255 reserved. Its groups admit only TCP 443 from the runner subnet to the endpoint subnet (ADR 0122).

## Alternatives considered

- **Public endpoints guarded only by Entra authentication.** Rejected: the data stores hold every user's conversations and every project's documents, so a leaked token or a misassigned role must not be usable from anywhere on the internet.
- **Service endpoints with subnet rules in place of private endpoints.** Rejected: the traffic still targets each service's public endpoint, AI Search's network rule set takes IP rules only, and only private endpoints with linked zones give every service a private address inside the network.
- **SQL, the registry and the test vault closed too when the allowlist is empty.** Rejected here: the pipeline must reach SQL for migrations and grants, the registry for image pushes and, by default, the test vault for the test passwords from GitHub-hosted runners; ADR 0104 records the in-network alternative for organisations that forbid any public SQL endpoint, and ADR 0122 the private-networking option that closes the vault.
- **The trusted-service exception.** Rejected: on storage it takes precedence over every other rule, and on SQL `AllowAllWindowsAzureIps` admits traffic from any Azure customer; Defender's scanner needs only its resource-instance rule.
- **One allowlist for every environment.** Rejected: an address added for `dev` work would also open `prod`'s data resources; each environment's list belongs to its own GitHub environment.
- **/26 integration subnets.** Rejected: 59 usable addresses can't leave the 100 free addresses the ingestion plan needs while scale operations double address use.
- **Route-all on in the first version.** Rejected: without a NAT gateway or firewall to filter it, routing every call through the network adds cost and failure modes and no protection; egress filtering is later hardening (ADR 0109), and the reserved ranges hold it.

## Consequences

- Easier: a compromised component reaches only the private endpoints, on their service ports; no resource holding data answers from the internet except to allowlisted operator addresses, SQL or the registry to one runner for the length of one job, and by default the test vault to one runner for one read window.
- Harder: operators work from an allowlisted address. With an empty allowlist, Cosmos DB, storage, AI Search, the AI services and Key Vault have no public path, so portal data views fail and data-plane work happens from an allowlisted machine or through the just-in-time path (ADR 0024). Storage IP rules don't admit clients in the account's own region, so an operator machine in the environment's Azure region can't be allowlisted.
- Cost per environment: 13 endpoints × $0.01 per hour × 730 hours ≈ $94.90 a month, plus 10 zones × $0.50 = $5.00 a month.
- SQL firewall changes take up to five minutes to apply, so the runner rule's probe waits up to 300 seconds (ADR 0119).
- Tests (deployed, `dev`): from the runner without a firewall rule, Cosmos DB, both storage accounts, AI Search, Azure OpenAI, Content Safety, Document Intelligence and the environment Key Vault refuse connections, and SQL refuses until the runner rule exists; from the API container every private-endpoint host name resolves to an address in `snet-private-endpoints`; the API's SCM host refuses every request; the ingestion app's default host name refuses every request; a message on `ingestion-requests` starts the dispatcher within 2 minutes with the app's public access disabled; after a deploy job the Activity Log shows the runner rule's creation and deletion and no `ci-runner-*` rule remains; a registry pull from the runner without a rule fails. Unit: the preflight's allowlist check refuses private, shared-address, loopback and link-local entries, prefixes and a 21st entry.
- Runbook R24 changes an allowlist: set `DOCCHAT_OPERATOR_IP_ALLOWLIST` on the GitHub environment and run a `full` deploy, or set the shared list and re-run the bootstrap's `shared` step; it then checks one `operator-<n>` SQL rule per address and the Cosmos DB `ipRules`.
- Risk: the Static Web App, Log Analytics and Application Insights stay public endpoints until private monitoring is added; outbound calls leave from the plan's shared outbound addresses, so no third party can allowlist this product alone.

## References

- VNet integration and routing: https://learn.microsoft.com/en-us/azure/app-service/overview-vnet-integration (checked 2026-10-14)
- Routing options (`outboundVnetRouting`): https://learn.microsoft.com/en-us/azure/app-service/configure-vnet-integration-routing (checked 2026-10-14)
- Functions networking options (subnet sizes, runtime scale monitoring): https://learn.microsoft.com/en-us/azure/azure-functions/functions-networking-options (checked 2026-10-14)
- App Service access restrictions: https://learn.microsoft.com/en-us/azure/app-service/overview-access-restrictions (checked 2026-10-14)
- Private endpoint DNS zone values: https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns (checked 2026-10-14)
- Storage network security (IP rules): https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security (checked 2026-10-14)
- Storage network security limitations (precedence; `Disabled` with `Deny`): https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-limitations (checked 2026-10-14)
- Cosmos DB account properties (2026-03-15): https://learn.microsoft.com/en-us/azure/templates/microsoft.documentdb/2026-03-15/databaseaccounts (checked 2026-10-14)
- Cognitive Services account properties: https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/accounts (checked 2026-10-14)
- SQL Database firewall (up to five minutes to take effect): https://learn.microsoft.com/en-us/azure/azure-sql/database/firewall-configure (checked 2026-10-15)
- Default outbound access: https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access (checked 2026-10-15)
- Azure Retail Prices REST API: https://prices.azure.com/api/retail/prices (checked 2026-10-15)
