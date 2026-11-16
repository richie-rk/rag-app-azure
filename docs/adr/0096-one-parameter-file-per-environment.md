# ADR 0096: One parameter file per environment

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 86-operations.md, CONTEXT.md
- **Related ADRs:** 0008, 0011, 0086, 0095, 0099, 0101, 0102, 0106, 0138

## Context

`dev`, `prod` and `load` run the same Bicep templates with different values (ADR 0095). Those values must be visible, typed and reviewed in pull requests, because an environment configured by hand drifts from the others, and a web client or app pointed at the wrong environment's endpoints fails in ways no test sees. azd's JSON parameter file receives per-environment values only through `${VARIABLE}` substitution from the azd environment, which lives outside the repository, unreviewed and untyped, so the product needs a parameter mechanism of its own.

Some values must still stay out of the repository: the operators' IP addresses, the alert mailboxes and the image digests that the `preprovision` hook resolves. Values that differ per environment and belong to other components (conversation and memory retention, group data access, alert thresholds) live in the same environment file as every other per-environment value, so one file describes one environment.

Vendor facts:

- azd selects the provisioning file by module name (`infra.module`, default `main`) and prefers `<module>.bicepparam` to `<module>.bicep` when both exist; it never selects a parameter file by environment name (azd source, checked 2026-10-21).
- `readEnvironmentVariable(name, [default])` in a `.bicepparam` file is resolved at compile time, so one file serves azd, `az deployment group what-if` and PSRule expansion.
- A Bicep object type accepts properties it doesn't declare and reports them only as warning BCP089; `@sealed()` turns that warning into an error, and the deployment engine then also rejects extra properties. Missing required properties and wrong types are compile errors (checked 2026-10-21).
- `loadJsonContent()` reads a JSON file of at most 1,048,576 characters when the Bicep file compiles; its path can't contain variables. Since Bicep CLI 0.36, a variable can declare a type (`var <name> <type> = <value>`), and the compiler checks the assigned value against it (checked 2026-10-21).

## Decision

1. **The files.**

   ```text
   infra/main.bicepparam          the one parameter file azd reads (using 'main.bicep')
   infra/types.bicep              exported, sealed user-defined types environmentConfig, openAiDeployment, embeddingModel
   infra/env/dev.json             per-environment values, typed by environmentConfig, committed and reviewed
   infra/env/prod.json
   infra/env/load.json
   infra/config/app-settings.json behavioural app settings with their owners' defaults (keys api, ingestion, modelStub)
   infra/config/regions.json      region facts with no GA API (Prompt Shields, semantic ranker, Linux Premium maximum, GitHub's Azure private networking regions)
   infra/shared.bicepparam        parameters of infra/shared.bicep, deployed by the bootstrap only
   ```

   The repository holds no JSON parameter file for `main.bicep`.
2. **`main.bicepparam` reads only what must stay out of the repository**, each from an environment variable at compile time: `environmentName = readEnvironmentVariable('AZURE_ENV_NAME')`; `apiImage`, `ingestionImage` from `DOCCHAT_API_IMAGE_CURRENT`, `DOCCHAT_INGESTION_IMAGE_CURRENT`; `modelStubImage` from `DOCCHAT_MODEL_STUB_IMAGE_CURRENT` (default empty); `operatorIpAllowlist` from `DOCCHAT_OPERATOR_IP_ALLOWLIST` (default empty); `resourceTokenSalt` from `DOCCHAT_RESOURCE_TOKEN_SALT` (default empty); `releaseSha` from `DOCCHAT_RELEASE_SHA` (default `local`); `opsAlertEmails` from `DOCCHAT_OPS_ALERT_EMAILS` (default empty; the preflight refuses an empty list in `prod`, `alert_receivers_missing`; security alerts go to the bootstrap's `ag-docchat-security` action group, whose `securityAlertEmails` the bootstrap requires, ADR 0114); and `budgetStartDate` from `DOCCHAT_BUDGET_START_DATE` with no default, so a provision without the `preprovision` hook fails at compile time. List values are comma-separated and split in the file. GitHub environment variables supply them in CI and the azd environment locally.
3. **Selection.** `main.bicep` loads all three files with literal paths into one typed variable, `var envConfigs {dev: environmentConfig, prod: environmentConfig, load: environmentConfig} = {dev: loadJsonContent('env/dev.json'), prod: loadJsonContent('env/prod.json'), load: loadJsonContent('env/load.json')}`, and passes `envConfigs[environmentName]` to every module as a parameter of type `environmentConfig`; it also loads `config/app-settings.json`. `environmentConfig` and each nested object type carry `@sealed()`. Every build therefore checks all three files, whichever environment it targets, and a missing, extra or mistyped field in any of them fails it.
4. **The fields.** Every per-environment value is a field of `environmentConfig`, whichever component owns it, with its type and allowed values: subscription and tenant (written by the bootstrap), `location` and `staticWebAppLocation`, `network.addressSpace`, `webCustomDomains`, the SKU, instance and capacity fields of each resource (`web`, `api`, `ingestion`, `sql`, `cosmos`, `search`), `openAi.enabled` and `modelStub.enabled` (exactly one true), `openAi.deployments` and `openAi.embeddingModels` (ADR 0099), the storage scanning fields, Key Vault protection, monitoring retention and daily cap, `telemetrySamplingRatio`, budget, the bootstrap-written `groups` object IDs, the conversation and memory fields `conversationRetentionDays`, `memoryRetentionDays` (0 to 3,650 days, 0 keeps everything) and `cosmosGroupDataAccess`, and the alert fields `alerts.enabled`, `alerts.ttftP95Ms`, `alerts.openAiFirstChunkP95Ms` (10,000 ms) and `alerts.openAiCallMeanMs` (8,000 ms), which `main.bicep` passes to `infra/modules/monitoring/alerts.bicep`. No component keeps a parameter file of its own. 83-infrastructure.md lists every field with its type, range and three values.
5. **Derived values are never fields.** `main.bicep` computes from the environment name alone the storage SKU (`Standard_LRS` in `dev`, `Standard_ZRS` in `prod` and `load`), Cosmos DB `isZoneRedundant` and the AI Search minimum of 2 replicas outside `dev` (ADR 0102); from SKU maps it computes the API worker count (`B1` 1, `P0v3` 1, `P1v3` 2, `P2v3` 4), the ingestion worker process count (`EP1` 1, `EP2` 2, `EP3` 4), the Durable activity and orchestrator concurrency (`EP1` 2, `EP2` 4, `EP3` 8) and the search index limit (`basic` 15, `standard` 50). App settings come only from resource outputs, environment-file fields, SKU maps and `config/app-settings.json`; none is set by hand, in the portal or by a script.
6. **What may be committed.** The bootstrap writes the tenant-specific IDs (subscription, tenant, group object IDs) into the environment files and the operator commits them, because object and resource IDs aren't secrets. IP addresses and personal email addresses never go in a committed file. Every field changes through a reviewed pull request, and a `prod` change deploys only after the `prod` approval.
7. **One file serves every tool.** azd provision, the pull-request what-if (`az deployment group what-if` under the pull-request identity) and the PSRule expansion for each environment all compile the same `main.bicepparam` with `AZURE_ENV_NAME` set.

## Alternatives considered

- **A JSON parameter file (`main.parameters.json`) with azd substitution.** Rejected: every value that differs per environment would be an azd or GitHub variable, outside review and without types; a JSON parameter file can't select per-environment values itself.
- **One `.bicepparam` file per environment** (`dev.bicepparam`, `prod.bicepparam`). Rejected: azd selects `<module>.bicepparam` by module name, so it would read none of them without a different `infra.module` or `infra.path` per environment, which `azure.yaml` can't express per environment.
- **Every per-environment value as a GitHub environment variable.** Rejected: invisible in pull requests, untyped and editable without review; only the values that must stay out of the repository are variables.
- **Conditions on `environmentName` inside each module.** Rejected: values would scatter across modules, and a reviewer couldn't see one environment's configuration in one place; only the derived values of item 5 depend on the name.
- **Unsealed types and an untyped `envConfigs`.** Rejected: an extra or misspelt field would compile with a warning and be ignored, which is how an intended setting silently fails to apply, and an untyped variable would leave the two files a build doesn't select unchecked.

## Consequences

- Easier: one file shows an environment's whole configuration; CI rejects a malformed file before any deployment; environments differ only where a reviewed file says so, and with the deploy-time web configuration of ADR 0086 no environment is configured by hand.
- Harder: a new per-environment value needs a field in `environmentConfig` with its type and range, and a value in all three files, in the same pull request as its use; tenant-specific IDs make the committed files specific to one tenant, so a new adopter runs the bootstrap's `config` step and commits its diff.
- 01-repository.md's tree shows `infra/main.bicepparam`, `infra/types.bicep`, `infra/env/` and `infra/config/`.
- Tests (CI): `bicep build` of `main.bicep` and `shared.bicep` and `bicep build-params` of `main.bicepparam` with `AZURE_ENV_NAME` set to `dev`, `prod` and `load`; an environment file with a missing, extra or mistyped field fails `bicep build-params`; each environment file holds `alerts.openAiFirstChunkP95Ms` 10,000 and `alerts.openAiCallMeanMs` 8,000 and the compiled template passes the four alert inputs to `alerts.bicep`; the preflight passes for `dev` and `prod` with the shipped files. Deployed (`dev`): both apps' settings equal 83-infrastructure.md's list exactly.
- Runbooks change values through pull requests to `infra/env/<env>.json` (R09 model versions, R10 zone redundancy, R29 budget) and through GitHub variables for addresses and mailboxes (R24).
- Risk: `loadJsonContent` embeds all three files in every compiled template, so the `dev` deployment's template also carries `prod`'s non-secret IDs and SKUs; none of them is a secret.

## References

- Bicep parameters file functions (`readEnvironmentVariable` at compile time): https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/bicep-functions-parameters-file (checked 2026-10-14)
- User-defined types and `@sealed()`: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/user-defined-data-types (checked 2026-10-21)
- azd `azure.yaml` schema (`infra.module`, default `main`): https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/azd-schema (checked 2026-10-21)
- azd Bicep provider (`<module>.bicepparam` preferred): https://github.com/Azure/azure-dev/blob/main/cli/azd/pkg/infra/provisioning/bicep/bicep_provider.go (checked 2026-10-21)
- What-if validation levels: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-what-if (checked 2026-10-14)
- PSRule for Azure with Bicep and `.bicepparam` files: https://azure.github.io/PSRule.Rules.Azure/using-bicep/ (checked 2026-10-14)
- Bicep file functions (`loadJsonContent`): https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/bicep-functions-files (checked 2026-10-21)
- Typed variables: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/variables (checked 2026-10-21)
