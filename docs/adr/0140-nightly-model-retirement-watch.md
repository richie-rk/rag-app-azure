# ADR 0140: Nightly model-retirement watch

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 84-testing.md, 83-infrastructure.md, 86-operations.md
- **Related ADRs:** 0009, 0067, 0099, 0100, 0101, 0045, 0141

## Context

Every model name and version is a pinned parameter, each Azure OpenAI deployment (chat, chat small, table summary, and the query and ingestion embedding deployments) has its own deployment type defaulting to `GlobalStandard`, embedding deployments use `versionUpgradeOption: NoAutoUpgrade`, and the region preflight checks each pin against its type through the Models API, failing when a version is deprecated for the subscription, isn't offered for that type in the region, or retires within 6 months (ADR 0099, ADR 0100, ADR 0101). A preflight runs only when someone provisions, yet a pin can go bad with no change in this repository: under Microsoft's lifecycle policy a GA version is deprecated for new subscriptions 12 months after launch and retires 18 months after it, so a new environment, a new subscription or a region's changed offering can lose a pinned version between two releases. At retirement a chat-model deployment is upgraded without review (`OnceCurrentVersionExpired`), and an embedding deployment (`NoAutoUpgrade`) stops working, which takes every index built with it out of service until a rebuild. Maintainers need to know that a pinned dependency is ageing out months before it breaks.

Vendor facts: the Models API (`GET /subscriptions/{sub}/providers/Microsoft.CognitiveServices/locations/{location}/models?api-version=2024-10-01`) returns each model's `lifecycleStatus`, `deprecation.inference` and each SKU's `deprecationDate`; a deprecated version can still be deployed by a subscription that has deployed it, never by a new one; GA retirement notices come at least 60 days ahead. Today's pins retire on 2028-01-11 (`gpt-5.6-sol` and `gpt-5.6-terra` 2026-07-09) and 2028-02-09 (`text-embedding-3-large` 1).

## Decision

1. **What is watched.** Each pinned deployment of the two long-lived environments: `chat`, `chatSmall` and `tableSummary` in `openAi.deployments`, and the `embed-query-<key>` and `embed-ingest-<key>` pair of each `openAi.embeddingModels` entry, each with its model, version and its own deployment type, as written in `infra/env/dev.json` and `infra/env/prod.json` at `main`.
2. **The job.** `nightly.yaml` (daily at 02:30 UTC) job `model-watch`, in GitHub environment `dev` as the `dev` deploy identity, `permissions {contents: read, id-token: write, issues: write}`, runs the preflight in its model-only mode for both files: `uv run --frozen python scripts/preflight/preflight.py --env dev --check models` and the same with `--env prod`. The identity reads model and account metadata in every subscription in use through DocChat Preflight Reader (`63c4387a-e306-4ef7-9bfa-2e921c67f02b`), so the `prod` file is checked against the `prod` account without any `prod` data access.
3. **Fail and warn rules.** A pinned version fails the job when it isn't offered for its deployment type in the region (`model_version_not_offered`, `model_sku_not_offered`), is retired (`model_retired`), is deprecated while the environment's account holds no deployment of it (`model_deprecated`), or retires within 183 days (`model_retires_soon`, from the SKU's `deprecationDate` or `deprecation.inference`). It adds a warning when it retires within 274 days. The rules are those of the provisioning preflight, so a nightly pass means the next provision passes the model check.
4. **Findings become issues.** On failure the job creates or updates one issue per finding titled `Model retirement: <deployment> <model> <version>` (`gh issue create`, or `gh issue comment` on the open issue), with the check's code and dates.
5. **The fix: runbook R09.** Choose the replacement version and its deployment type (`GlobalStandard` unless the product owner decides otherwise) from the region's offering; edit the deployment's parameter object in `infra/env/dev.json`; run `--check models` locally; open a pull request; after the `dev` deploy, run the chat deployment smoke and tokenizer check and set the new model's price before traffic reaches it; repeat for `prod` with approval. Embedding deployments are never changed in place: a new deployment is added for the new version, every logical index that uses the old one is rebuilt into a new physical index, and the old deployment is removed only when no physical index references it. The issue closes when the nightly watch passes. A `model_retires_soon` finding is resolved within its 183-day window.

## Alternatives considered

- **Checking only at provision time.** Rejected: an environment that isn't redeployed for months learns about a retirement when its embedding deployment stops working.
- **Letting the service auto-upgrade every deployment.** Rejected for embeddings, whose vectors would stop matching the indexes built with the old version; for chat models, an unreviewed upgrade changes answers, prices and tokenizer behaviour without a test run, so the watch makes upgrades planned work instead.
- **Relying on Microsoft's retirement emails.** Rejected: they reach the subscription's contacts, not the repository's maintainers, and say nothing about this template's own pins or a new subscription's deprecation state.
- **A scheduled alert in Azure Monitor.** Rejected: model availability isn't a metric or log signal; the preflight already holds the exact rule, and one script keeps provision-time and nightly checks identical.
- **Checking `prod` from a `prod` identity.** Rejected: a nightly job would hold a `prod` deploy identity's token for a read that DocChat Preflight Reader grants to the `dev` identity without any write or data access.

## Consequences

- A pin that will block the next provision is known at least 183 days before it does, and a pin within 274 days of retirement is visible as a warning.
- Today's pins start warning in April 2027 (the gpt-5.6 versions) and May 2027 (`text-embedding-3-large` 1), and failing in July and August 2027; from 2027-07-09 a new `gpt-5.6` deployment in an account that has never held it fails `model_deprecated`, so a new environment created after that date needs a newer chat pin; the embedding replacement, which needs a rebuild of every index, is planned before its August 2027 failure date.
- The watch reads metadata only; it changes nothing, and a failure blocks no merge or release by itself, while the next provision's preflight does.
- Obligations: R09 for every finding; `86-operations.md` holds the responses; the job's issue titles are stable, so one finding keeps one issue.
- Tests (84-testing.md): the nightly model watch passes for the `dev` and `prod` configurations; the preflight's unit tests on recorded Models API responses (`model_retires_soon` fails at 182 days and passes at 184; `--check models` warns at 270 days but not at 280; a version the account already holds passes `model_deprecated`).

## References

- Model retirements, lifecycle states and deprecation for new subscriptions: https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements (checked 2026-10-14)
- Model retirement schedule: https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule (checked 2026-10-14)
- Deployment types: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types (checked 2026-10-14)
