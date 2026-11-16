# ADR 0118: Shared registry rights and concurrency

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-11-06 (no job sweeps the registry: no job holds the shared allowlist, so leftover runner rules go to runbook R26, ADR 0119)
- **Decided by:** Product owner
- **Specified in:** 83-infrastructure.md, 82-security.md, 86-operations.md
- **Related ADRs:** 0098, 0104, 0114, 0115, 0116, 0119, 0124, 0127, 0136

## Context

Each commit's images are built once into one shared Premium registry and deployed by digest to every environment (ADR 0124). Three needs follow. The registry's IP rules are settings of the registry resource itself, so the `dev` build can push through the registry's firewall only with a right that edits the registry. Each environment's app identities need AcrPull on a registry in another resource group, which the templates create through a nested deployment there. And concurrent deploys must never cancel each other: GitHub cancels a pending run in a concurrency group when a newer one queues, unless told otherwise, so a later `dev` run could silently cancel a pending `prod` deploy. The rights that serve these needs must not let any pipeline delete or open the registry `prod` pulls from.

Vendor facts:

- Registry permissions: `Microsoft.ContainerRegistry/registries/write` "Creates or updates a container registry" and carries the network rules (there is no separate network-rule action); `registries/privateEndpointConnectionsApproval/action` auto-approves a private endpoint connection; Container Registry Contributor and Data Access Configuration Administrator manages network rules but can also delete the registry; AcrPull and AcrPush hold no control-plane rights (checked 2026-10-14).
- Deploying a Bicep module scoped to another resource group starts a nested deployment there, so the deploying identity needs deployment rights at that scope (checked 2026-10-14).
- In ABAC repository-permissions mode, AcrPull, AcrPush and AcrDelete aren't honoured, and Microsoft says that mode will become the default (checked 2026-10-14).
- GitHub concurrency: a group allows one running job and, with the default `queue: single`, one pending job, which a newly queued run cancels; `queue: max` keeps up to 100 pending runs in order and can't be combined with `cancel-in-progress: true` (checked 2026-10-14).

## Decision

1. **The registry.** `crdocchat<sharedToken>`, Premium, `Microsoft.ContainerRegistry/registries@2025-11-01` (local module), in `rg-docchat-shared`, deployed by the bootstrap only: `adminUserEnabled: false`, `anonymousPullEnabled: false`, `publicNetworkAccess: 'Enabled'` with `networkRuleSet {defaultAction: 'Deny', ipRules: <sharedOperatorIpAllowlist>}`, `networkRuleBypassOptions: 'None'`, `roleAssignmentMode: 'LegacyRegistryPermissions'` (AcrPull and AcrPush honoured, so the conditioned AcrPull delegation keeps working when the ABAC mode becomes the platform default), ARM-audience tokens on; repositories `docchat/api`, `docchat/ingestion` and `docchat/model-stub`; diagnostic setting `registry-audit` to `log-docchat-security`.
2. **Rights on the shared registry and group:**

   | Right | Holder | Scope |
   |---|---|---|
   | Role Based Access Control Administrator with an AcrPull-only condition (`7f951dda-4ed3-4680-a7ca-43fe172d538d`, principal type `ServicePrincipal`) | each deploy identity | registry |
   | DocChat Registry Endpoint Approver (`aa667e85-6da6-4ceb-838f-505789b07fc5`: `registries/read`, `registries/privateEndpointConnectionsApproval/action`) | each deploy identity | registry |
   | DocChat Shared Deployment Operator (`fefff72b-0dcb-4495-8fc3-c93f2aeef356`: `subscriptions/resourceGroups/read`; `deployments/read`, `write`, `validate/action`, `whatIf/action`, `cancel/action`; `deployments/operations/read`; `deployments/operationstatuses/read`) | each deploy identity | `rg-docchat-shared` |
   | AcrPush (`8311e382-0749-4cb8-b61a-304f252e45ec`) | `dev` deploy identity | registry |
   | DocChat Registry Firewall Editor (`1c6b44a0-732f-4fb9-b65e-520d77014b79`: `registries/read`, `registries/write`) | `dev` deploy identity | registry |
   | Reader and DocChat Template Validator | pull-request identity | `rg-docchat-shared` |

   Each environment's `main.bicep` creates its app identities' AcrPull assignments through a module scoped to `rg-docchat-shared`, which the deployments role permits and the AcrPull-only condition bounds; after tightening, only the API and ingestion identities of `dev` and `prod` may receive it (ADR 0115). Each environment's registry private endpoint is approved automatically through the approval action.
3. **Guards on `registries/write`.** No identity holds `registries/delete`. Deny assignments at `rg-docchat-shared` refuse a local admin user, anonymous pull, repository-scoped tokens and unrestricted network access (`docchat-deny-acr-admin`, `-anonymous`, `-tokens`, `-open-network`, ADR 0116). The alert `docchat-alert-registry-writes` (severity 2) reports every successful `registries/write` to `ag-docchat-security` (ADR 0115).
4. **Only `dev` jobs touch the registry.** The `publish` job (push, build-provenance attestation, attestation check), the `resolve` job (tag lookup and attestation check for a dispatch, rollbacks included) and `nightly.yaml`'s `trivy-rescan` run in GitHub environment `dev` as the `dev` deploy identity. Each adds a transient IP rule for its runner with `firewall.py open --target registry` and removes it in an `if: always()` step with `firewall.py close --target registry` (ADR 0119). None sweeps the registry first: no job holds the shared allowlist, so a job can't tell a crashed runner's address from an operator's, and leftover runner rules are removed only through runbook R26. `prod` and `load` jobs never contact the registry: they deploy the digest references that `publish` or `resolve` produced and verified, and the apps pull through their private endpoints with their own identities.
5. **Concurrency.** The groups `docchat-registry` (`publish`, `resolve`, `trivy-rescan`), `docchat-env-dev`, `docchat-env-prod` and `docchat-env-load` each set `cancel-in-progress: false` and `queue: max`, so up to 100 queued runs wait in order and none is cancelled; jobs that edit one resource's IP rules never overlap.
6. **Release ordering in `prod`.** Before provisioning, the `prod` job reads the API site's tag `docchat-release-sha`; when the run's commit is an ancestor of the deployed commit (`git merge-base --is-ancestor`), the job writes "a newer release is already in prod" to its summary and ends without deploying, so approving an old queued run can't roll `prod` back. Deliberate rollbacks are `deploy.yaml` dispatch runs with `rollback: true`, which skip the check (ADR 0127).

## Alternatives considered

- **Container Registry Contributor and Data Access Configuration Administrator for the `dev` identity.** Rejected: it can delete the registry `prod` pulls from.
- **The `prod` job opening the registry firewall too.** Rejected: `prod` would need the firewall role and would share a concurrency group with `dev`, so a `dev` run could delay a `prod` deploy; the verified digests make the contact unnecessary.
- **GitHub's default concurrency.** Rejected: a newly queued `dev` run would cancel a pending `prod` deploy without notice.
- **A registry with public access off and pushes from a runner inside a network.** Not adopted now: it needs a larger runner joined to a network that reaches the registry's private endpoint for every `dev` build, the pattern ADR 0104 records for SQL.
- **The ABAC repository-permissions mode.** Not adopted: AcrPull and AcrPush aren't honoured there, and the conditioned AcrPull delegation depends on them.

## Consequences

- The `dev` deploy identity can change any registry property except what the Deny assignments refuse, and every change alerts; it can't delete the registry.
- Each `dev` publish job's two firewall edits raise one `docchat-alert-registry-writes` notification per window; runbook R20 routes a pipeline-explained one to R07.
- A pending `prod` deploy waits behind earlier `prod` runs and is never cancelled; at most 100 runs wait in one group.
- A rule a crashed job leaves on the registry lives until runbook R26 or the bootstrap's next `shared` step, which rewrites the registry's IP rules; images are never deleted automatically, and runbook R28 prunes, at most quarterly, images older than 400 days that no `deploy-prod-*` tag references.
- Tests (84-testing.md): every `uses:` is pinned and every job signs in as one identity; a registry pull from a runner without a rule fails; the API and ingestion images run from digests equal to the release manifest, each with a valid attestation for `deploy.yaml` on `main`; a dispatch for a commit whose tag was re-pointed to an unattested image fails in `resolve` with `image_attestation_invalid`.

## References

- Container Registry permissions: https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/containers (checked 2026-10-14)
- Container Registry built-in roles: https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-built-in-roles-overview (checked 2026-10-14)
- ABAC repository permissions: https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions (checked 2026-10-14)
- Bicep modules at another scope: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-to-resource-group (checked 2026-10-14)
- GitHub workflow concurrency: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency (checked 2026-10-14)
- Registry resource (API 2025-11-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.containerregistry/registries (checked 2026-10-14)
