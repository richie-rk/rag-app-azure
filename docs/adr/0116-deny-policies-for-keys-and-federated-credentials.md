# ADR 0116: Deny policies for keys and federated credentials

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner
- **Specified in:** 82-security.md, 83-infrastructure.md, 84-testing.md
- **Related ADRs:** 0095, 0103, 0106, 0111, 0113, 0114, 0115, 0118, 0122

## Context

Nothing in this product authenticates with a key: every resource turns local and key authentication off in Bicep (ADR 0106). A template setting is only as strong as the identity that deploys it, though, and Contributor can turn key access back on, call `listKeys`, or add a federated credential to an app identity and sign in as it, all outside the just-in-time route. The `dev` deploy identity also holds `registries/write` on the shared registry (ADR 0118), which reaches every registry setting. Azure Policy Deny assignments made by the bootstrap close these paths: a policy assignment is a `Microsoft.Authorization` object, so no pipeline identity can change or delete one.

Vendor facts:

- Built-in definitions with an Audit, Deny or Disabled `effect` parameter (checked 2026-10-15): Storage accounts should prevent shared key access `8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54` (2.0.0); Storage account public access should be disallowed `4fa4b6c0-31ca-4c0d-b10d-24b96f62a751` (3.1.1); Cosmos DB database accounts should have local authentication methods disabled `5450f5bd-9c72-4390-a9c4-a7aba4edfdd2` (1.2.0); Azure Cosmos DB accounts should not allow traffic from all Azure data centers `12339a85-a25c-4f17-9f82-4766f13f5c4c` (1.0.0); Azure AI Services resources should have key access disabled `71ef260a-8f18-47b7-abcb-62d0673d94dc` (1.1.0); Azure AI Search services should have local authentication methods disabled `6300012e-e9a4-4649-b41f-a85f5c43be91` (1.0.1); Azure Key Vault should use RBAC permission model `12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5` (1.0.1); Key vaults should have deletion protection enabled `0b60c0b2-2dc2-4e1c-b5c9-abbed971de53` (2.1.0); Azure SQL Database should have Microsoft Entra-only authentication enabled `b3a22bc9-66de-45fb-98fa-00f5df42f41a` (1.0.0) and the same enabled during creation `abda6d70-9778-44e7-84a8-06713e6db027` (1.2.0); the container registry definitions for local admin `dc921057-6b28-4fbe-9b83-f7bec05db6c2` (1.0.1), anonymous authentication `9f2dea28-e834-476c-99c5-3507b4728395` (1.0.0), repository-scoped access tokens `ff05e24e-195c-447e-b322-5e90c9f9f366` (1.0.0) and unrestricted network access `d0793b48-0edc-4296-a390-4c75d1bdfd71` (2.0.0).
- Microsoft documents a Deny policy for federated credentials: `{"if": {"field": "type", "equals": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"}, "then": {"effect": "deny"}}` (checked 2026-10-21).
- `Microsoft.Authorization/policyAssignments` and `policyDefinitions` are GA at 2026-07-01; `policyExemptions` has only preview versions, the latest 2026-01-01-preview (checked 2026-10-15).

## Decision

1. **Assignments** (`infra/bootstrap/policies.bicep`, `Microsoft.Authorization/policyAssignments@2026-07-01`, `enforcementMode: 'Default'`, parameter `effect: 'Deny'`, each with a `nonComplianceMessages` entry naming its rule and this ADR):

   | Assignment | Definition | Scopes |
   |---|---|---|
   | `docchat-deny-storage-shared-key` | `8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54` | each environment group (under content-share fallback 1, the host account in `notScopes`) |
   | `docchat-deny-storage-public-access` | `4fa4b6c0-31ca-4c0d-b10d-24b96f62a751` (3.1.1) | each environment group and `rg-docchat-shared` |
   | `docchat-deny-cosmos-local-auth` | `5450f5bd-9c72-4390-a9c4-a7aba4edfdd2` | each environment group |
   | `docchat-deny-cosmos-azure-datacenters` | `12339a85-a25c-4f17-9f82-4766f13f5c4c` | each environment group |
   | `docchat-deny-ai-key-access` | `71ef260a-8f18-47b7-abcb-62d0673d94dc` | each environment group |
   | `docchat-deny-search-local-auth` | `6300012e-e9a4-4649-b41f-a85f5c43be91` | each environment group |
   | `docchat-deny-keyvault-access-policies` | `12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5` | each environment group and `rg-docchat-shared` |
   | `docchat-deny-keyvault-purge` | `0b60c0b2-2dc2-4e1c-b5c9-abbed971de53` | `rg-docchat-prod`, `rg-docchat-shared` |
   | `docchat-deny-sql-local-auth` | `b3a22bc9-66de-45fb-98fa-00f5df42f41a` | each environment group |
   | `docchat-deny-sql-local-auth-create` | `abda6d70-9778-44e7-84a8-06713e6db027` | each environment group |
   | `docchat-deny-federated-credentials` | custom `docchat-deny-federated-credentials` | each environment group |
   | `docchat-deny-acr-admin`, `docchat-deny-acr-anonymous`, `docchat-deny-acr-tokens`, `docchat-deny-acr-open-network` | `dc921057-…`, `9f2dea28-…`, `ff05e24e-…`, `d0793b48-…` | `rg-docchat-shared` |
   | `docchat-deny-store-deletion` | custom `docchat-deny-store-deletion` (`denyAction`, ADR 0111) | `rg-docchat-prod` |

   "Each environment group" is `rg-docchat-dev`, `rg-docchat-prod` and `rg-docchat-load`.
2. **The federated-credential definition.** `docchat-deny-federated-credentials` (`Microsoft.Authorization/policyDefinitions@2026-07-01`, subscription scope, `mode: 'All'`): `{"if": {"field": "type", "equals": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"}, "then": {"effect": "deny"}}`. Assigned to the environment groups only, it stops anyone with Contributor there from adding a credential to `id-docchat-api-<env>`, `id-docchat-ingest-<env>` or `id-docchat-stub-load`, while the pipeline identities in `rg-docchat-identity` keep theirs.
3. **Why anonymous blob access is in the list.** A compromised deploy identity could set `allowBlobPublicAccess: true` on the data account and open `documents` or `renditions` to anonymous reads, a path to every document that needs no data role and no tightened assignment.
4. **The refusal.** Before assigning anything, the bootstrap's `policies` step reads every built-in definition with `az policy definition show --name <ID>` and stops with `policy_definition_missing` (exit 2) when one is absent or its `effect` parameter doesn't allow `Deny`; no assignment is made.
5. **No exemptions.** Policy exemptions have no GA API version, so the one exception, the host account under content-share fallback 1, is a `notScopes` entry on `docchat-deny-storage-shared-key`, set by the bootstrap operator before the fallback deploys (ADR 0103).
6. **Out of the pipeline's reach.** Contributor's NotActions exclude `Microsoft.Authorization/*/Write` and `*/Delete`, and Role Based Access Control Administrator holds only role-assignment write and delete, so no pipeline identity can change, delete or exempt an assignment; `docchat-alert-governance` reports every policy assignment write or delete and every exemption write (ADR 0115).

## Alternatives considered

- **Audit in place of Deny.** Rejected: an audit reports a re-enabled key after it exists; Deny refuses the request.
- **Assignments made by `main.bicep`.** Rejected: the identity they bound could remove them.
- **A policy exemption for the content-share fallback.** Rejected: exemptions exist only in preview API versions; `notScopes` is GA and names the one account.
- **Denying federated credentials at the subscription.** Rejected: the pipeline identities in `rg-docchat-identity` need theirs; the environment groups hold only app identities.
- **Custom definitions for every rule.** Rejected: the built-in definitions are maintained by Microsoft and carry their versions; a custom definition is used only where no built-in exists.

## Consequences

- While the assignments exist, no deployment, pipeline or person can turn key access back on, open blobs anonymously, give Cosmos DB "all Azure data centers" access, switch Key Vault to access policies or add a federated credential to an app identity: Resource Manager answers `RequestDisallowedByPolicy`. Removing an assignment first needs Owner or Resource Policy Contributor and raises `docchat-alert-governance`.
- Under content-share fallback 1, the host account alone is outside the shared-key denial (ADR 0103).
- A changed or retired built-in definition stops the next bootstrap run at `policy_definition_missing`, so no policy that can't deny is ever assigned.
- Tests (84-testing.md, deployed `dev`): an attempt under the deploy identity to set `disableLocalAuth: false` on the Cosmos DB account, `allowSharedKeyAccess: true` or `allowBlobPublicAccess: true` on the data account, or to add a federated credential to `id-docchat-api-dev` is denied by Azure Policy; unit test: the `policies` step stops with `policy_definition_missing` for a definition whose `effect` parameter allows only `Audit`.

## References

- Built-in policy definitions for Storage: https://learn.microsoft.com/en-us/azure/storage/common/policy-reference (checked 2026-10-15)
- Built-in policy definitions for Cosmos DB, AI services, AI Search, Container Registry, Key Vault and Azure SQL: https://learn.microsoft.com/en-us/azure/cosmos-db/policy-reference, https://learn.microsoft.com/en-us/azure/ai-services/policy-reference, https://learn.microsoft.com/en-us/azure/search/policy-reference, https://learn.microsoft.com/en-us/azure/container-registry/policy-reference, https://learn.microsoft.com/en-us/azure/key-vault/policy-reference, https://learn.microsoft.com/en-us/azure/azure-sql/database/policy-reference (checked 2026-10-15)
- Federated credentials and Azure Policy: https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations (checked 2026-10-21)
- Policy assignments and definitions (API 2026-07-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.authorization/policyassignments (checked 2026-10-15)
