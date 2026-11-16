# ADR 0117: Defender for Storage enablement rights

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner
- **Specified in:** 83-infrastructure.md, 82-security.md, 86-operations.md, 90-build-order.md
- **Related ADRs:** 0038, 0039, 0098, 0106, 0107, 0111, 0114, 0115, 0141

## Context

Uploaded files are scanned by Microsoft Defender for Storage before ingestion takes them, and ingestion takes a version only after a clean result (ADRs 0038, 0039). Scanning is a resource-level setting of the data storage account, declared in Bicep and on by default in `prod`, so the deploy identity must be able to enable it. Account-level enablement needs subscription-scope `datascanners` write, and Defender assigns its scanner a data role under the caller's rights, which the deploy identity's role-assignment condition must allow. `dev` turns scanning on for the stage-0 Defender spike and for the stage-2 malware test, so the tightened conditions of both `dev` and `prod` must allow the scanner.

Vendor facts (checked 2026-10-14):

- Enabling malware scanning on one storage account needs "action set 2": `Microsoft.Storage/storageAccounts/read` and `write`; `Microsoft.Security/datascanners/read` and `write`, which "must be granted at subscription level"; `Microsoft.Security/defenderforstoragesettings/read` and `write`; `Microsoft.EventGrid/eventSubscriptions/read`, `write` and `delete`; `Microsoft.Authorization/roleAssignments/read`, `write` and `delete`. On-upload scanning needs the Microsoft.EventGrid provider registered.
- Enabling creates an Event Grid system topic in the account's resource group and a subscription-level `StorageDataScanner` resource (`Microsoft.Security/dataScanners`) with a system-assigned identity; Defender adds that scanner to the account's resource-instance rules.
- The built-in role Defender for Storage Data Scanner (`1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40`) "is used by the data scanner of Defender for Storage"; other Defender pages name Storage Blob Data Owner or Reader, so a spike confirms it.
- `Microsoft.Security/defenderForStorageSettings@2025-06-01` (GA) is an extension resource named `current` on the account, with `isEnabled`, `malwareScanning.onUpload {isEnabled, capGBPerMonth, filters}`, `blobScanResultsOptions`, `sensitiveDataDiscovery.isEnabled` and `overrideSubscriptionLevelSettings`.
- Contributor's NotActions exclude `Microsoft.Authorization/*/Write`, so Contributor alone can't create the scanner's role assignment.

## Decision

1. **The role.** DocChat Defender Scanner Enabler, GUID `2b1b37e4-43e4-4a34-8e89-05dacdf549cf`, Actions `Microsoft.Security/datascanners/read` and `Microsoft.Security/datascanners/write`, `assignableScopes` each subscription in use; created by the bootstrap's `roles` step after `az provider operation show --namespace Microsoft.Security` confirms both actions, and assigned by its `assignments` step to `id-docchat-deploy-dev`, `id-docchat-deploy-prod` and `id-docchat-deploy-load` at subscription scope (`principalType: 'ServicePrincipal'`).
2. **The providers.** The bootstrap's `providers` step registers Microsoft.Security and Microsoft.EventGrid before any provision.
3. **The rest of action set 2.** Contributor on `rg-docchat-<env>` covers the storage, Defender-settings and Event Grid actions; the conditioned Role Based Access Control Administrator covers the scanner's role assignment, because `1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40` is in the condition's set `<R>` with principal type `ServicePrincipal` (ADR 0115).
4. **The tightened pair, `dev` and `prod`.** `tighten_conditions.py --env <env>` reads the subscription's scanner principal with `az resource show --ids /subscriptions/<subscription>/providers/Microsoft.Security/datascanners/StorageDataScanner --query identity.principalId -o tsv` and adds the pair (Defender for Storage Data Scanner, that principal) to both clauses of the condition, and the principal to `allowedRoleAssigneePrincipalIds`, in `dev` as well as `prod`. In `prod` a missing scanner stops the script with `principal_ids_missing`, because `prod`'s first provision enables scanning. In `dev` a missing scanner leaves the pair out, and the script prints that it must run again (runbook R23) before `dev` scanning is turned on.
5. **The settings the deploy identity enables.** `Microsoft.Security/defenderForStorageSettings@2025-06-01` named `current` on `stdocchatdata<env><token>`, always declared: `isEnabled: storage.malwareScanning`; `malwareScanning.onUpload {isEnabled: storage.malwareScanning, capGBPerMonth: storage.malwareScanCapGbPerMonth (1,000), filters {excludeBlobsWithPrefix: ['renditions/', 'ingestion-work/'], excludeBlobsWithSuffix: ['/converted.pdf']}}`; `malwareScanning.blobScanResultsOptions: 'blobIndexTags'`; `sensitiveDataDiscovery.isEnabled: false`; `overrideSubscriptionLevelSettings: true`. `storage.malwareScanning` is `true` in `prod`, `false` in `load`, and `false` in `dev` except while the stage-2 malware test runs. The scanner's resource-instance rule (`/subscriptions/<subscription>/providers/Microsoft.Security/datascanners/StorageDataScanner`) is declared in Bicep, so a redeploy keeps it, and a diagnostic setting `scan-results` sends `ScanResults` to the workspace (ADR 0107).
6. **The stage-0 Defender spike.** Under the `dev` deploy identity, deploy the settings with scanning on, list the scanner's role assignment and network rule, upload a test blob and a second blob whose name ends with `/converted.pdf`, and read both blobs' index tags. Rules: a role other than `1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40` replaces it in `<R>` and in the tightened pair; a network rule whose `resourceId` differs from the Bicep constant (compared case-insensitively) replaces the constant; the tag key casing and time format become ingestion's constants; a tag on the second blob removes the suffix filter, so converted PDFs are scanned within the monthly cap and their tags stay unread; `dev` scanning returns to `false`. The spike runs before `dev`'s first tightening, so the subscription's scanner exists when `dev` is tightened.
7. **The upload gate.** Uploads answer `503 malware_scanning_not_ready` until the daily scanner canary reports `clean` within the last 48 hours (ADR 0039); runbook R12 is the acceptance check after a first enablement. A deployment that fails on `datascanners/write` or on the scanner's role assignment stops provisioning with the Resource Manager error, and R12 checks the deploy identity's two rights before the retry.

## Alternatives considered

- **A Security Administrator enables scanning in a post-provision step.** Rejected: a manual step per environment would let a redeploy and the declared setting disagree, and the setting belongs in the template like every other.
- **Owner or Security Admin at the subscription for the deploy identities.** Rejected: both reach far beyond enabling one account's scanner.
- **The subscription-level Defender for Storage plan.** Rejected: it needs Subscription Owner or action set 1, and it would scan the Functions host accounts and the `dev` and `load` data accounts too; only `prod`'s uploads need scanning.
- **The scanner's pair in `prod` only.** Rejected: `dev` turns scanning on for the spike and the stage-2 malware test, and after `dev`'s tightening the scanner's role assignment would be refused.

## Consequences

- Each deploy identity can create or update the subscription's data scanner, a subscription-scope right; it can't enable Defender plans (`Microsoft.Security/pricings`) or read data through it.
- Switching scanning off can make Defender delete its scanner's role assignment, which raises `docchat-alert-role-assignments`; runbook R20 routes it to R07 when the pull request that switched `dev` back explains it. In `prod` that deletion is blocked by the data account's lock, so switching `prod` scanning off follows ADR 0111's procedure.
- Cost in `prod`: $0.0134 per storage account-hour plus $0.15 per GB scanned (about $9.78 a month at idle); the 1,000 GB monthly cap stops scanning when reached, and new versions then fail closed until the cap resets or rises (R12 step 8).
- Tests (84-testing.md): deployed tests in any environment with scanning on: the scanner canary gets a scan-result tag, `StorageMalwareScanningResults` holds it, and uploads answer `503 malware_scanning_not_ready` until a clean result exists; the stage-2 malware test in `dev`; unit test: `tighten_conditions.py` renders the scanner pair for `dev` when the scanner exists and stops with `principal_ids_missing` in `prod` when it doesn't.

## References

- Defender for Storage support matrix (action set 2): https://learn.microsoft.com/en-us/azure/defender-for-cloud/support-matrix-defender-for-storage (checked 2026-10-14)
- Malware scanning: deployed resources and the scanner: https://learn.microsoft.com/en-us/azure/defender-for-cloud/introduction-malware-scanning (checked 2026-10-14)
- `defenderForStorageSettings` (API 2025-06-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.security/2025-06-01/defenderforstoragesettings (checked 2026-10-14)
- On-upload malware scanning (Event Grid prerequisite, monthly cap): https://learn.microsoft.com/en-us/azure/defender-for-cloud/on-upload-malware-scanning (checked 2026-10-14)
- Storage built-in roles (Defender for Storage Data Scanner): https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage (checked 2026-10-14)
