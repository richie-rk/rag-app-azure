# ADR 0109: No egress filtering in the first version

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 14-ingestion.md
- **Related ADRs:** 0002, 0005, 0010, 0044, 0049, 0084, 0098, 0103, 0104, 0150

## Context

The apps reach every Azure data and AI service through private endpoints, and their internet-bound calls (Microsoft Entra ID, Azure Monitor) use the platform's default outbound path. Filtering outbound traffic means routing all of it through the network to a NAT gateway or firewall, which needs address space, a route, an allow list for every Microsoft endpoint the apps call, and a monthly charge per environment. The first version leaves egress filtering and private monitoring ingestion out, and must keep both possible with infrastructure changes only. The content-share fallback (ADR 0103) is the one case that routes one app's traffic through the network, because a Linux app that reads a Key Vault reference from a private vault must.

Vendor facts:

- With route-all off, VNet integration sends only RFC 1918 and service-endpoint traffic into the network, and network security groups govern only that traffic; `outboundVnetRouting` has `allTraffic`, `applicationTraffic` (the same switch as the site property `vnetRouteAllEnabled`), `contentShareTraffic`, `imagePullTraffic` and `backupRestoreTraffic`; image pulls use the public route unless `imagePullTraffic` is on.
- For API versions released after 31 March 2026, subnets of new virtual networks default to `defaultOutboundAccess: false`; private subnets "aren't applicable to delegated or managed subnets used for hosting PaaS services", whose outbound connectivity the service manages.
- `Microsoft.Network/natGateways@2025-09-01` takes `sku.name` `Standard` or `StandardV2`, `idleTimeoutInMinutes`, `publicIpAddresses` and `zones`; NAT gateway Standard costs $0.045 an hour plus $0.045 per GB processed in `eastus2`.

## Decision

1. **Route-all off.** The API web app, the ingestion function app and the `load` model stub set `outboundVnetRouting: {allTraffic: false, applicationTraffic: false, imagePullTraffic: true, contentShareTraffic: false, backupRestoreTraffic: false}`.
2. **Three outbound paths.**
   - RFC 1918 traffic, which is every call to a private endpoint (SQL, Cosmos DB, both storage accounts, AI Search, Azure OpenAI, Content Safety, Document Intelligence, Key Vault, the registry and, in `load`, the stub), goes through VNet integration and the network security groups (ADR 0098).
   - Image pulls go through the network to the registry's private endpoint with the app's user-assigned identity (`imagePullTraffic: true`).
   - Everything else leaves through the platform's default outbound path with the plan's outbound addresses: Microsoft Entra ID (`login.microsoftonline.com`, tokens and signing keys) and Azure Monitor ingestion (Application Insights and the Log Analytics workspace keep public ingestion). The ingestion image carries its Functions extension bundle, so the host downloads none (ADR 0005).
3. **No egress device.** No NAT gateway, firewall, route table or private link scope for monitoring is declared, with one exception: content-share fallback 1 adds the NAT gateway `ng-docchat-<env>` with its static public IP `pip-docchat-<env>-nat` to `snet-ingest-integration` and turns `allTraffic` on for the ingestion app only (ADR 0103).
4. **Room for later.** Each environment's network reserves its fourth range: `10.61.3.0/24` in `dev`, `10.62.3.0/24` in `prod`, and `10.63.3.64` to `10.63.3.255` in `load` (after `snet-stub-integration`, `10.63.3.0/26`); the shared CI network reserves `10.60.0.96` to `10.60.0.255`. The address spaces never overlap, so a firewall subnet, an in-network runner subnet or a hub peering fits without renumbering.
5. **Services that can't call out.** Azure OpenAI, Content Safety and Document Intelligence set `restrictOutboundNetworkAccess: true` with `allowedFqdnList: []`: no feature in use makes an outbound call, and documents reach Document Intelligence as bytes, never as URLs. AI Search makes no outbound call (no indexer, skillset, vectorizer or customer-managed key, ADR 0044). SQL keeps `restrictOutboundNetworkAccess: 'Disabled'`, because no vendor documentation covers its auditing to Log Analytics with outbound restriction on; it is part of the later hardening.
6. **Inside the network, deny by default.** Every subnet's network security group admits only the app-to-private-endpoint paths on their service ports and denies the rest of the virtual network; the platform's default internet-outbound rule stays (ADR 0098).
7. **The parsing child has no sockets.** The one process that runs code fed with third-party bytes, the ingestion app's parsing child, closes its own egress: its seccomp filter makes `socket()` and `socketpair()` fail with `EPERM` for every address family, so it reaches no internet host, no private endpoint and no loopback listener, whatever the app's outbound path allows (ADR 0049). If the seccomp spike of ADR 0049 fails, the build stops at stop condition `SC-9` for the product owner, who either accepts that the child keeps the app's outbound path, or moves parsing to compute that isolates the network. The owner decides with the spike's census of loopback listeners in hand, which records every listening socket of the deployed container and what an unauthenticated local caller can do with it, the Functions host's gRPC endpoint for the language worker included (`90-build-order.md` section 2.16). With the first choice, a compromised child reaches the network and every listener of the census; through the Durable extension's listener it can list instances and read their status, which holds instance IDs and activity outputs with document titles, start, restart, rewind, terminate, suspend, resume and purge orchestration instances, raise events, signal, read and list entities, and make the app its task hub's primary; it holds only its own document; and the managed identity's token is protected only by `IDENTITY_HEADER` not being readable by a parse user (ADR 0049 decision 16 and its Consequences, `14-ingestion.md` section 15.6 item 13); this ADR's other decisions hold either way.

## Alternatives considered

- **Route-all on with a NAT gateway now.** Rejected: a NAT gateway gives the apps one static address but filters nothing, costs about $36.50 a month per environment before traffic, and no external service needs to allowlist the apps.
- **Route-all on with a firewall now.** Rejected for the first version: a firewall's hourly charge per environment, and an allow list that must track every Microsoft endpoint the apps and the Functions host reach, are a cost and an operational load this version doesn't take on.
- **Route-all on without a NAT gateway or firewall.** Rejected: routing every call through the network adds failure modes and no protection.
- **A private link scope for monitoring ingestion now.** Rejected: it belongs with egress filtering; telemetry already authenticates with Entra ID and local auth is off on the workspace and the component.

## Consequences

- A compromised app process can reach any internet host from the plan's shared outbound addresses. What narrows it: every outbound target comes from a validated setting, never from data (ADR 0150); the model has no tools and answers load no images (ADR 0084); the AI accounts can't call out; nothing the apps run is fetched at run time (ADRs 0005, 0010); and the parsing child, the likeliest process to be compromised by a document, opens no socket at all (decision 7).
- Adding egress filtering later is an infrastructure change: turn `allTraffic` on for the apps, place a NAT gateway or firewall in the reserved range with a route from the integration subnets, allow Microsoft Entra ID and Azure Monitor, and add a private link scope for monitoring. No application code changes, because every endpoint is a setting.
- Under fallback 1, all of the ingestion app's outbound traffic already leaves through `pip-docchat-<env>-nat`, which a later filter can use.

## References

- App Service VNet integration and routing: https://learn.microsoft.com/en-us/azure/app-service/overview-vnet-integration (checked 2026-10-14)
- `Microsoft.Web/sites` (`outboundVnetRouting`): https://learn.microsoft.com/en-us/azure/templates/microsoft.web/sites (checked 2026-10-14)
- Default outbound access: https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access (checked 2026-10-15)
- NAT gateway resource: https://learn.microsoft.com/en-us/azure/templates/microsoft.network/natgateways (checked 2026-10-14)
- Azure Retail Prices API: https://prices.azure.com/api/retail/prices (checked 2026-10-15)
