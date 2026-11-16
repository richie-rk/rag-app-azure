# ADR 0067: Embedding deployments outside content filtering

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 12-chat-pipeline.md, 14-ingestion.md, 82-security.md, 83-infrastructure.md, 84-testing.md
- **Related ADRs:** 0061, 0062, 0066, 0099, 0100, 0106

## Context

The product's platform-filter rule says that Azure OpenAI deployments run under a content filter policy declared in Bicep, with Prompt Shields for jailbreak attacks in the user's question. Besides the three chat-model deployments of ADR 0066, the template deploys two embedding deployments per embedding model, from the array `openAi.embeddingModels`: `embed-query-<key>`, which the API calls for query vectors, and `embed-ingest-<key>`, which ingestion calls for chunk vectors (ingestion has its own deployment so a bulk upload can't starve chat). The default entry, key `te3-large-v1`, gives `embed-query-te3-large-v1` and `embed-ingest-te3-large-v1`: `text-embedding-3-large` version 1, `GlobalStandard`, `NoAutoUpgrade`, 30 and 60 capacity units in `dev`, 100 and 250 in `prod` (ADR 0100).

The rule exists so that the user's question and every text a model writes pass a filter. The question this ADR settles is whether a policy on an embedding deployment adds anything to that.

Vendor facts (Microsoft Learn):

- "The content filtering system doesn't apply to prompts and completions processed by the Embedding models or the audio models such as Whisper" (checked 2026-10-14 and 2026-10-18).
- The same page says that default safety settings apply to all models except Whisper; the part a policy configures, the filtering system, is the part that doesn't run on embedding prompts. An attached policy is therefore accepted and has no effect.

What an embedding call carries and returns:

- The API's query-embedding call embeds the search query (the question as written, or its standalone form from the `chat-small` rewrite) and, when retrieval confidence is low, the search passage that `chat-small` wrote (ADR 0061).
- Ingestion's call embeds each chunk's `chunkText`, at most 8,000 `cl100k_base` tokens per input.
- The output is a list of floats of the physical index's dimensions (1,024 by default). It is used only as a search vector: it never reaches a user and never enters a prompt (the vector field isn't retrievable).

## Decision

1. **No policy on embedding deployments.** In `infra/modules/ai/openai-deployments.bicep`, the deployment objects built from `openAi.embeddingModels` (`embed-query-<key>` and `embed-ingest-<key>`) omit `raiPolicyName`; they carry `versionUpgradeOption: 'NoAutoUpgrade'`. No other setting refers to a filter for them.
2. **A CI rule holds the line both ways.** PSRule rule `DocChat.OpenAI.DeploymentRules` requires that every deployment whose model name doesn't start with `text-embedding` names a `raiPolicyName`, and that every embedding deployment has `versionUpgradeOption: 'NoAutoUpgrade'` and no `raiPolicyName`. CI runs PSRule over the templates on every pull request.
3. **Where the rule is met.** The platform-filter rule is met on `chat`, `chat-small` and `ingest-summary` (ADR 0066), each under a blocking policy: every question that reaches the answer model is judged by `chat`'s jailbreak shield and harm filters as the latest user message of the answer call; when the session has an earlier completed turn or a summary, the question is judged again by `chat-small`'s filters inside the question-rewrite call, and memory extraction, when it runs, sends it to `chat-small` too; every text a model writes comes from one of those three deployments. `82-security.md` states it as: "every Azure OpenAI deployment whose model generates text names a content filter policy declared in Bicep; no embedding deployment names one, because content filtering doesn't apply to embedding models".
4. **Untrusted text is handled where it can act.** Chunk text reaches a model only after Prompt Shields screens it as a retrieved source (ADR 0062); the search query and the search passage reach the embedding deployment only to become vectors.

## Alternatives considered

- **Attach `docchat-text-guard` to the embedding deployments, so that every deployment names a policy.** Rejected: the service accepts the attachment but filters nothing, so the template would show a protection that doesn't exist, and a reviewer would read the attachment as coverage.
- **Analyse every text with Content Safety text analysis before embedding it.** Rejected: an embedding produces a vector that reaches no user and no prompt; the question is already judged on the chat deployments; chunk text is screened by Prompt Shields at retrieval, where it can affect an answer. A call per chunk during ingestion would add cost, latency and a failure point with no output to protect.

## Consequences

- Easier: an embedding deployment object needs only its model, version, type, capacity and `NoAutoUpgrade`.
- `83-infrastructure.md` lists the embedding deployments without a policy and gives the reason; `82-security.md` carries the testable statement above; `84-testing.md` includes the PSRule run, so a policy on an embedding deployment, or a text deployment without one, fails the build.
- The rule keys on the model-name prefix `text-embedding`. Adding an embedding model whose name doesn't start with it to `openAi.embeddingModels` needs a rule change in the same pull request, or CI fails that deployment for lacking a policy.
- If Microsoft extends content filtering to embedding models, the template picks it up only through a reviewed change that attaches a policy and changes the rule.
- No unfiltered model text reaches a user or a prompt through an embedding call, so users carry no risk from this exception.

## References

- Content filtering doesn't apply to embedding models: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/content-filter (checked 2026-10-14)
- The same statement on the Foundry (classic) page, with the default safety settings: https://learn.microsoft.com/en-us/azure/foundry-classic/foundry-models/concepts/content-filter (checked 2026-10-14 and 2026-10-18)
- Deployments Bicep reference (`raiPolicyName`, `versionUpgradeOption`): https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/accounts/deployments (checked 2026-10-14)
- Embeddings (`text-embedding-3-large`, `dimensions`, 8,192 tokens per input): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings (checked 2026-10-14)
- PSRule for Azure rule set: https://github.com/Azure/PSRule.Rules.Azure/tree/main/docs/en/rules (checked 2026-10-14)
