# ADR 0095: Environments, subscription and resource groups

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-10-30 (the CI network only under the private-networking option, ADR 0122)
- **Decided by:** Product owner and architect
- **Specified in:** 00-overview.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 86-operations.md, CONTEXT.md
- **Related ADRs:** 0016, 0019, 0096, 0101, 0112, 0114, 0115, 0116, 0117, 0118, 0122, 0126

## Context

Every environment is built from one set of Bicep templates with different parameters, deployed by azd from GitHub Actions under one federated deploy identity per environment, which holds Contributor on its own resource group. The product needs at least a development and a production environment, and a short-lived environment with production SKUs in which the load test proves the scaling targets. Every non-production environment has a test client and test users for automated tokens; production has neither (ADR 0019). No environment may differ from another through settings made by hand: each is described entirely by the shared templates and its reviewed environment file, so a web client can never reach another environment's endpoints.

Contributor reaches further than the deploy identity's role-assignment condition: it can add a federated credential to any user-assigned identity in its scope and then sign in as that identity, outside the GitHub environment that should guard it. The pipeline identities, and the security alerts that watch the pipeline, must therefore live where no pipeline identity can write (ADR 0115). A one-time bootstrap run by a tenant administrator creates the resource groups and these identities (ADR 0114).

Vendor facts:

- Contributor (`b24988ac-6180-42a0-ab88-20f7382dd24c`) has Actions `*`; its NotActions include `Microsoft.Authorization/*/Write` and `Microsoft.Authorization/*/Delete`, so it creates no role assignment, role definition, policy assignment or lock. Nothing under `Microsoft.ManagedIdentity` is excluded, and Contributor or Owner is enough to create a user-assigned identity and its federated credentials.
- A user-assigned identity holds at most 20 federated credentials, each issuer and subject pair unique; identities created in Malaysia South can't hold federated credentials.
- With `AZURE_RESOURCE_GROUP` set and `targetScope = 'resourceGroup'` in `main.bicep`, azd deploys into an existing resource group and needs rights on that group only, not on the subscription.
- A template deployment registers its resources' providers only when the caller holds `/register/action` at the subscription; providers of supporting resources (security, monitoring, Event Grid for malware scanning) must be registered beforehand.
- A complete-mode deployment of an empty template deletes every resource in the group, Cognitive Services accounts, Key Vaults and Cosmos DB accounts included, and never deletes role assignments, policy assignments or locks. A deleted Key Vault keeps its name for its soft-delete period (7 days in `load`), a deleted Cognitive Services account for 48 hours.
- At eastus2 list prices an idle `load` environment costs about $3.32 an hour (about $2,427 a month if left running), against about $426 a month for idle `dev` and $1,433 for idle `prod` (checked 2026-10-15).

## Decision

1. **Three environments.** `dev` (long-lived, development SKUs), `prod` (long-lived, production SKUs, the only production environment) and `load` (short-lived: created for one load-test run and torn down after it, with production SKUs for every component the load test measures, the model stub in place of Azure OpenAI (ADR 0112), and non-production identity rules, so it has a test client and test users like `dev`). The azd environment name equals the environment name, and `main.bicep` declares `@allowed(['dev', 'prod', 'load']) param environmentName string`. Every environment deploys the same `infra/main.bicep` with its own environment file (ADR 0096):

   | Component | `dev` | `prod` | `load` |
   |---|---|---|---|
   | API plan | B1, 1 instance | P1v3, autoscale 2 to 6 | P1v3, autoscale 1 to 6 |
   | Ingestion plan | EP1, always ready 1, burst 2 | EP2, always ready 1, burst 4 | EP2, always ready 2, burst 4 |
   | SQL | `GP_S_Gen5_2` serverless, auto-pause after 15 minutes | `GP_Gen5_2` | `GP_Gen5_2` |
   | Cosmos DB | serverless | autoscale, zone-redundant | autoscale, zone-redundant |
   | AI Search | basic, 1 replica | standard, 2 replicas | standard, 2 replicas |
   | Azure OpenAI | Global Standard deployments | the same, larger capacities | none (model stub) |

2. **One subscription by default.** The three environments, the shared resources and the pipeline identities live in one subscription. Each environment's subscription ID is the field `subscriptionId` of its environment file, so `prod` moves to its own subscription by changing that value and re-running the bootstrap for it.
3. **Five resource groups, created by the bootstrap and never by the pipeline** (`bootstrap.py --step resource-groups`, ADR 0114). Each environment group is in its environment's region; `rg-docchat-identity` and `rg-docchat-shared` are in the default region `eastus2`.

   | Resource group | Holds | Pipeline rights in it |
   |---|---|---|
   | `rg-docchat-identity` | the five pipeline identities `id-docchat-deploy-dev`, `id-docchat-deploy-prod`, `id-docchat-deploy-load`, `id-docchat-pr` and `id-docchat-citest` with their federated credentials; the security workspace `log-docchat-security` (365 days); the action group `ag-docchat-security`; the security alert rules | none: no pipeline identity holds any role there |
   | `rg-docchat-shared` | the container registry, the test-credential vault and, under the private-networking option of ADR 0122 only, the CI network `vnet-docchat-ci` with the vault's private endpoint, its DNS zone and the GitHub network settings | each deploy identity: deployments only (custom role DocChat Shared Deployment Operator) plus its registry rights (ADR 0118) |
   | `rg-docchat-dev`, `rg-docchat-prod` | every resource of the environment, its workload identities included | that environment's deploy identity: Contributor plus Role Based Access Control Administrator under a condition (ADR 0115); in `dev` also `id-docchat-citest`: Log Analytics Reader on `log-docchat-dev` only (ADR 0120) |
   | `rg-docchat-load` | every resource of the current load run | the `load` deploy identity: as above; `id-docchat-citest`: Log Analytics Reader and DocChat Load Test Operator only |

4. **Resource-group scope only.** Each environment deploys with `targetScope = 'resourceGroup'` into `rg-docchat-<env>`, named by the GitHub environment variable `AZURE_RESOURCE_GROUP`. At the subscription a deploy identity holds only the read-only DocChat Preflight Reader and the scanner-enablement role (ADR 0117), so no pipeline creates, deletes or re-scopes a resource group.
5. **The identity group is out of the pipeline's reach.** No pipeline job can add a federated credential to a pipeline identity, change the subject an identity trusts, delete a security alert or purge the security workspace; every change there is a bootstrap step run by the tenant administrator (Owner). The workload identities `id-docchat-api-<env>` and `id-docchat-ingest-<env>` stay in their environment group, where the Deny policy on federated credentials (ADR 0116) and the alert `docchat-alert-federated-credentials` guard them. The preflight refuses an identity group in `malaysiasouth` (`fic_region_unsupported`, ADR 0101).
6. **Names.** Cloud Adoption Framework abbreviation, then `docchat`, then the environment, then the 6-character `resourceToken` where a name must be globally unique: `take(uniqueString(subscription().id, environmentName), 6)` in `dev` and `prod`; in `load`, `take(uniqueString(subscription().id, environmentName, resourceTokenSalt), 6)` with `resourceTokenSalt = 'run-<GitHub run ID>'`, so every run gets new globally unique names and no soft-deleted vault or account of an earlier run blocks it, with no purge right at the subscription. Shared resources use `sharedToken = take(uniqueString(subscription().id, 'shared'), 6)`. 83-infrastructure.md checks every pattern against its resource's length and character rules.
7. **The `load` group is kept and emptied.** `rg-docchat-load` exists permanently and holds at most one run. Before provisioning, a run tags it `docchat-load-run=<run ID>` and `docchat-load-created=<UTC timestamp>`. Teardown (`scripts/deploy/teardown_load.py`, as the `load` deploy identity) deletes the run's app registrations and their service principals and purges both from Microsoft Graph's deleted items, removes the run identities' AcrPull assignments on the registry, then deploys `infra/teardown/empty.bicep` in complete mode, up to 3 more times 5 minutes apart while resources remain, so the group's role and policy assignments survive between runs. A nightly guard tears down a `load` environment older than 6 hours unless its run is still queued, waiting or in progress and less than 24 hours old (ADR 0126).

## Alternatives considered

- **One subscription per environment as the default.** Item 2 supports it, but most adopters of a template start from one subscription, and one subscription keeps the Azure OpenAI quota the preflight checks, the budget alerts, the subscription-scope Activity Log alerts, the custom role definitions and the provider registrations in one place. Separate subscriptions would need each of those once per subscription.
- **Pipeline identities in their environment's resource group.** Rejected: Contributor there would let a compromised job add a federated credential for a subject of its choosing, a branch other than `main` or a pull request, to its own deploy identity, and sign in as it later without the `prod` approval; it could also delete the alerts that would report it.
- **The pipeline creates resource groups through a subscription-scope deployment.** Rejected: azd would need subscription-scope deployment rights, and Contributor at the subscription would reach the identity group, the shared group and every other environment.
- **Delete and re-create `rg-docchat-load` for every run.** Rejected: its role and policy assignments would need an Owner on every run, because no pipeline identity writes `Microsoft.Authorization` objects outside its conditioned delegation, and deleting a group needs rights above it.
- **A long-lived `load` environment.** Rejected: idle, it costs about $3.32 an hour, and per-run names keep each run independent of an earlier run's soft-deleted resources.
- **Load tests in `dev` or `prod`.** Rejected: `dev`'s SKUs (B1, EP1, basic search, serverless Cosmos DB) can't show that the production sizing meets the scaling targets, and `prod` must never hold a test client, test users or synthetic load beside real users' data.

## Consequences

- Easier: an environment is described completely by the shared templates and its environment file, so environments differ only where a reviewed file says so; `prod` can move to its own subscription by one value.
- Harder: every change in `rg-docchat-identity` (a new pipeline identity, a changed federated credential, an alert rule) needs the bootstrap operator, not only a pull request.
- `dev` and `prod` draw on one subscription's regional Azure OpenAI quota; the preflight's quota check sums the capacity of every deployment and fails with the shortfall in units (ADR 0101).
- `load` costs money only while a run exists; the nightly guard limits a forgotten environment to about 6 hours, about $20.
- Runbooks: R21 (bootstrap), R22 (first provision), R30 (decommission: remove `prod`'s locks, delete and purge the app registrations, delete the resource group, purge soft-deleted Cognitive Services accounts and, outside `prod`, the Key Vault, delete the environment's federated credentials, subscription-scope assignments and GitHub environments).
- Tests: `bicep build-params` succeeds with `AZURE_ENV_NAME` set to `dev`, `prod` and `load`; before `rg-docchat-load` holds any resource of a run it carries that run's two tags; teardown leaves the group empty and the run's registrations permanently deleted; `prod_smoke` finds no test client registration in `prod`; in `dev`, the deploy identity's attempt to add a federated credential to `id-docchat-api-dev` is denied by Azure Policy.
- Risk: a subscription-wide incident (exhausted quota, a misapplied policy, a spending limit) reaches every environment at once; the `subscriptionId` field is the way to separate `prod`.

## References

- Resource-group-scoped deployments with azd: https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/resource-group-scoped-deployments (checked 2026-10-14)
- Privileged built-in roles (Contributor's NotActions): https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged (checked 2026-10-14)
- Federated credentials on a user-assigned identity (limits; Contributor or Owner creates them): https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust-user-assigned-managed-identity (checked 2026-10-14)
- Federated identity credential considerations (Malaysia South): https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations (checked 2026-10-14)
- Resource providers and their registration: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-providers-and-types (checked 2026-10-14)
- Complete-mode deletion by resource type: https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-complete-mode-deletion (checked 2026-10-15)
- Cloud Adoption Framework resource abbreviations: https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/azure-best-practices/resource-abbreviations (checked 2026-10-21)
- Azure Retail Prices REST API: https://prices.azure.com/api/retail/prices (checked 2026-10-15)
