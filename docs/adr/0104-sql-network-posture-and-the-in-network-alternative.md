# ADR 0104: SQL network posture and the in-network alternative

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-10-30 (the test vault as a third runner-rule target and the CI network as an option, ADR 0122)
- **Decided by:** Product owner and architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0024, 0027, 0098, 0103, 0105, 0116, 0119, 0122, 0124, 0125

## Context

The product owner requires SQL and Cosmos DB to sit behind network isolation, reachable only from the environment's virtual network and from a named operator IP allowlist. Migrations and SQL grants run in the pipeline under the deploy identity after every provision (ADR 0027), from a GitHub-hosted runner whose address is neither in the network nor in the allowlist. The product owner decided that a transient runner rule, added and removed by the deploy identity inside one job and recorded in the Activity Log, counts as part of the operator allowlist. SQL accepts only Microsoft Entra authentication (ADR 0125), so the network posture is a second layer behind authentication, never the only one. Some organisations forbid any public SQL endpoint, so the design also records how this product runs without one.

Vendor facts (checked 2026-10-14 unless stated):

- `Microsoft.Sql/servers` (API 2025-01-01): `publicNetworkAccess` `Disabled`, `Enabled` or `SecuredByPerimeter`; `minimalTlsVersion` up to `1.3`; `restrictOutboundNetworkAccess`. "There might be up to a five-minute delay for changes to the Azure SQL Database firewall configuration to take effect."
- GitHub larger runners exist only on the GitHub Team and Enterprise Cloud plans and are billed per minute for public and private repositories ($0.006 per minute for 2-core Linux). Azure private networking for GitHub-hosted runners works only with larger runners: the subnet is delegated to `GitHub.Network/networkSettings` (resource `GitHub.Network/networkSettings@2024-04-02`, GA), the runners are created in the subnet's region, including `eastus2`, and "use whatever outbound control your network is using".
- A Bicep deployment script (`Microsoft.Resources/deploymentScripts@2023-08-01`) runs privately in a container instance on a subnet delegated to `Microsoft.ContainerInstance/containerGroups`, but its storage account must have `allowSharedKeyAccess` set to `true` ("The only way to mount a storage account in Azure Container Instance (ACI) is via an access key"), and its container image comes from the Microsoft Container Registry (checked 2026-10-21).

## Decision

1. **A public endpoint limited to the allowlist.** The SQL server `sql-docchat-<env>-<token>` has `publicNetworkAccess: 'Enabled'` in every environment, `minimalTlsVersion: '1.3'` (ADR 0105), `connectionPolicy: 'Default'` and no `AllowAllWindowsAzureIps` rule. Its only standing firewall rules are `operator-<n>` (n from 1; start = end = one address of `operatorIpAllowlist`, read from the GitHub environment variable `DOCCHAT_OPERATOR_IP_ALLOWLIST`: at most 20 single public IPv4 addresses, validated by preflight check 9 with `allowlist_invalid`). With an empty allowlist the server has no standing rule at all, so only the private endpoint and a live runner rule reach it.
2. **The private endpoint.** `pe-sql-docchat-<env>-<token>-sqlServer` (group `sqlServer`) in `snet-private-endpoints`, with the zone `privatelink.database.windows.net` linked to the environment's network only; both apps reach SQL through VNet integration and this endpoint (ADR 0098).
3. **The transient runner rule.** The provision job of every environment (the azd `postprovision` hook that runs migrations and grants) adds one rule named `ci-runner-<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>` under that environment's deploy identity (Contributor: `Microsoft.Sql/servers/firewallRules/write` and `delete`) and removes it in the same job; the Activity Log records both. `scripts/deploy/firewall.py` (`sweep`, `open`, `close`, each `--target sql|registry|vault --env <env>`, no other target) does the work:
   - `sweep` runs first and removes every SQL rule named `ci-runner-*`; the concurrency group `docchat-env-<env>` (`cancel-in-progress: false`, `queue: max`) serialises every job that reaches the server, so no other job's live rule exists at that moment.
   - `open` reads the runner's public address from `https://api.ipify.org?format=text`, cross-checks it with `https://ifconfig.me/ip` (HTTPS, 5-second timeouts; a failure, a disagreement or a non-public IPv4 address exits 2 with `runner_ip_unresolved` or `runner_ip_mismatch` and adds nothing), creates the rule with `az sql server firewall-rule create`, then tries a TDS login with `Encrypt=strict` every 15 seconds for at most 300 seconds (error 40615 means not yet) and exits 3 with `rule_not_effective`, removing the rule, if it never succeeds.
   - `close` runs in a step with `if: always()` and removes exactly the rule `open` created; a failure exits 4 with `rule_close_failed`, and the next `sweep` removes the rule.
   - Outside GitHub Actions (`GITHUB_ACTIONS` unset) `open` adds nothing and exits 2 with `not_in_allowlist` unless the caller's address is already in the allowlist: operators work from allowlisted machines.
   - SQL, the `dev` registry and, by default, the test-credential vault are the only resources that ever take a runner rule (ADR 0119, ADR 0122).
4. **The in-network alternative, recorded and not built.** An organisation that forbids any public SQL endpoint runs the provision job on a GitHub-hosted Ubuntu 24.04 larger runner joined through Azure private networking to a subnet carved from the environment's reserved range (`10.61.3.0/24` in `dev`, `10.62.3.0/24` in `prod`, `10.63.3.64` to `10.63.3.255` in `load`) and delegated to `GitHub.Network/networkSettings`, with its network settings resource, network configuration and runner group created by the bootstrap exactly as for the CI network of ADR 0122's private-networking option. The subnet keeps `defaultOutboundAccess: true`, as the CI runner subnet does, so the job still reaches GitHub, Microsoft Entra ID and Azure Resource Manager; its network security group allows outbound TCP 1433 and 11000 to 11999 to `snet-private-endpoints` and denies the rest of the network, and `nsg-docchat-<env>-pe` gains the matching inbound rule from that subnet (ADR 0098). The job then sets the server's `publicNetworkAccess` to `'Disabled'`, skips `firewall.py` for SQL, and reaches SQL through the private endpoint, whose zone resolves inside that network.

## Alternatives considered

- **The in-network runner as the default.** Not adopted: the product owner chose the transient runner rule. Operators reach SQL from the allowlist, so the public endpoint exists whenever the allowlist holds an address, and an in-network runner would remove only the runner rule while adding a subnet, network settings and a runner group per environment and billed runner minutes for every deploy.
- **A deployment script inside the network.** Rejected: its storage account must allow shared-key access, a second key exception beside the content-share fallback (ADR 0103) that the shared-key Deny policy forbids (ADR 0116); its container pulls an Azure CLI image from the Microsoft Container Registry, so the ODBC driver and the migration code would be fetched at run time outside the pinned, scanned images (ADR 0124).
- **A self-hosted runner inside the network.** Rejected for the reasons of ADR 0122: an ephemeral runner registers with a stored credential holding repository Administration write, and a persistent one carries state between jobs.
- **A standing rule for GitHub's runner address ranges.** Rejected: those ranges are shared by every GitHub customer, so the rule would admit them all permanently.
- **The "allow Azure services" exception.** Rejected: it admits connections from any Azure customer's resources, and neither SQL nor Cosmos DB takes it.

## Consequences

- Outside a provision job, only the private endpoint and at most 20 allowlisted addresses reach the server, and inside one job a single runner address does for a few minutes. Every path still needs an Entra token for a contained user with a role, over TLS 1.3.
- A rule a crashed runner leaves behind lives until the next job that reaches SQL sweeps it, or until runbook R26 (`firewall.py sweep --target sql --env <env>` from an allowlisted machine) removes it; R26 also lists who wrote each rule from the Activity Log and sends any caller other than a pipeline identity to R31.
- Two third-party address echo services sit in the deploy path; a wrong answer can only open a rule for a few minutes to an address that still needs Entra credentials and a role, and a disagreement fails the job before any rule exists.
- Changing the allowlist is runbook R24: set `DOCCHAT_OPERATOR_IP_ALLOWLIST` on the GitHub environment and run a `full` deploy; the preflight refuses a malformed list.
- Tests: unit tests prove that `firewall.py` accepts only the `sql`, `registry` and `vault` targets, that `sweep` removes only `ci-runner-*` SQL rules, that `close` removes only the rule `open` created, and that `open` refuses private, CGNAT, loopback and link-local addresses and disagreeing answers; deployed `dev` tests prove that SQL refuses the runner until `open` adds its rule, and that after a deploy job the Activity Log shows the rule's create and delete under the deploy identity and no `ci-runner-*` rule remains.
- Adopting the alternative later costs per environment a subnet from the reserved range, the GitHub network objects and runner minutes at $0.006 per minute, and needs GitHub Team or Enterprise Cloud, which ADR 0122 needs only under its private-networking option. With public access disabled, operators' allowlisted machines can't reach SQL either, so just-in-time SQL work (ADR 0024) and the operator CLI need an in-network path that the adopting organisation provides.

## References

- Azure SQL server resource (API 2025-01-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.sql/2025-01-01/servers (checked 2026-10-14)
- Azure SQL Database firewall rules and their delay: https://learn.microsoft.com/en-us/azure/azure-sql/database/firewall-configure (checked 2026-10-14)
- Larger runners: https://docs.github.com/en/actions/concepts/runners/larger-runners (checked 2026-10-14)
- Actions runner pricing: https://docs.github.com/en/billing/reference/actions-runner-pricing (checked 2026-10-14)
- Azure private networking for GitHub-hosted runners: https://docs.github.com/en/organizations/managing-organization-settings/about-azure-private-networking-for-github-hosted-runners-in-your-organization (checked 2026-10-14)
- Workflow concurrency: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency (checked 2026-10-14)
- Deployment scripts over a private endpoint: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deployment-script-vnet-private-endpoint (checked 2026-10-21)
- Deployment script storage account requirements: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deployment-script-develop (checked 2026-10-21)
