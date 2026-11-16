# ADR 0068: Reasoning-model calls on Chat Completions

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 12-chat-pipeline.md, 13-conversations-and-memory.md, 14-ingestion.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0059, 0060, 0063, 0070, 0099, 0100, 0101, 0112, 0113, 0130, 0140, 0147

## Context

Every Azure OpenAI deployment defaults to `GlobalStandard` (ADR 0099), where current GA models are offered: `gpt-5.6-sol` for chat and `gpt-5.6-terra` for chat small and the table summary, version `2026-07-09`, retiring 2028-01-11 (ADR 0100). Both are reasoning models, so the call shape differs from plain chat models: no sampling parameters, a completion cap that also covers hidden reasoning, and an effort setting that trades latency and cost for reasoning depth.

A request never names a model: the API picks the deployment from its settings. A model, effort or parameter that a deployment refuses must therefore fail at start-up or at the stage gate, never on a user's turn, and every call must go through one client created once per process, because the chat path is async end to end and must not build clients per request.

Vendor facts (Microsoft Learn and PyPI, checked 2026-10-14 unless dated otherwise):

- The Azure OpenAI v1 API is GA: base URL `https://<resource>.openai.azure.com/openai/v1/` with the standard OpenAI client and `base_url`, no api-version needed; Chat Completions is `POST {endpoint}/openai/v1/chat/completions`. Microsoft recommends the Responses API for new apps; Chat Completions stays supported.
- Entra ID: a bearer-token provider for scope `https://ai.azure.com/.default` passed as `api_key` (openai 1.106.0 or later); a custom subdomain is required; the caller needs a role holding the `Microsoft.CognitiveServices/accounts/OpenAI/*` data actions: Cognitive Services OpenAI User (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) holds only those, and Cognitive Services User (`a97b65f3-24c7-4388-baec-2e87135dc908`) holds `Microsoft.CognitiveServices/*`, which also covers Content Safety's `text:shieldPrompt` (both checked 2026-10-25).
- GPT-5-series reasoning models on Chat Completions take `max_completion_tokens` (reasoning plus visible tokens) and don't support `temperature`, `top_p` or the penalty parameters; `max_tokens` is deprecated.
- `reasoning_effort` values are model-dependent (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`); gpt-5.6 defaults to `medium`; `minimal` doesn't work with gpt-5.1 or later; reasoning tokens are billed as output and reported in `usage.completion_tokens_details.reasoning_tokens`; Microsoft advises reserving at least 25,000 tokens while a workload is new; developer and system messages are equivalent and shouldn't both be sent. The same page lists gpt-5.6 versions as 2026-06-25 while the model and retirement pages say 2026-07-09, which is why versions are resolved through the Models API (ADR 0101).
- `stream_options.include_usage` adds a final usage chunk, which may be missing when a stream is interrupted (ADR 0060); `safety_identifier` (at most 64 characters) replaces the deprecated `user` field.
- Prompt caching (checked 2026-10-18): caching needs a prompt of at least 1,024 tokens whose first 1,024 tokens are identical; on GPT-5.6 and later, cache writes can be charged and are reported in `prompt_tokens_details.cache_write_tokens`; the default mode `implicit` places a breakpoint on the latest message; `prompt_cache_options.mode` `explicit` with no breakpoints uses no caching and incurs no cache-write charges on Standard pay-as-you-go deployments; models before GPT-5.6 answer `400` to `prompt_cache_options`.
- `tiktoken` 0.14.0 maps the `gpt-5` prefixes to `o200k_base` and the text-embedding-3 models to `cl100k_base`, and has no GPT-6 entry (`encoding_for_model` raises `KeyError`).
- `openai` 3.31.0 is the latest release; the package's client retries twice by default and allows 600 s per request.
- The Responses API stores responses server-side by default. This product keeps conversation content only in its own Cosmos DB store, under its own access rules and retention.

## Decision

**1. One client per process, on the v1 endpoint with Entra tokens.** `docchat_core.clients.openai` builds one `openai.AsyncOpenAI` per process at start-up and closes it at shutdown; no other module builds a model client (ADR 0113).

| Profile | `max_retries` | `timeout` | `http_client` |
|---|---|---|---|
| API | 0 (retries are per call, ADR 0070) | `httpx2.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)`; connect from `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` (10, 1 to 30), read from `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS` (60, 10 to 120) | `httpx2.AsyncClient(limits=httpx2.Limits(max_connections=200, max_keepalive_connections=50))` |
| Ingestion | 3, each honouring `Retry-After` up to 120 s | `httpx2.Timeout(60.0, connect=5.0)` | not set |

Both profiles use `base_url = "<DOCCHAT_OPENAI_ENDPOINT>/openai/v1/"` (the account's custom-subdomain endpoint) and `api_key` = an async bearer-token provider over the process's one credential (`ManagedIdentityCredential` in Azure), scope `https://ai.azure.com/.default`. Package `openai` 3.31.0 (PyPI, 2026-10-14); `httpx2`, its transport, is imported only in this module (ADR 0130). No key is ever sent.

**2. Chat Completions only.** Every text call is `chat.completions.create` and every vector call `embeddings.create`; the Responses API is never called. Messages use only the `system`, `user` and `assistant` roles, never `developer`.

**3. Explicit effort and cap on every call; no sampling parameters.**

| Call | Deployment setting | `max_completion_tokens` | `reasoning_effort` |
|---|---|---|---|
| Answer | `DOCCHAT_OPENAI_CHAT_DEPLOYMENT` | `chat.answer_max_completion_tokens`: 12,000; 2,000 to 40,000 and at most `chat.context_token_budget` − 8,000 | `DOCCHAT_OPENAI_CHAT_REASONING_EFFORT`: `low` |
| Rewrite, search passage, follow-ups | `DOCCHAT_OPENAI_SMALL_DEPLOYMENT` | `chat.small_call_max_completion_tokens`: 800; 200 to 8,000 | `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT`: `none` |
| Rolling summary | `DOCCHAT_OPENAI_SMALL_DEPLOYMENT` | `summary.max_completion_tokens`: 2,000; 500 to 16,000 and at least `summary.max_summary_tokens` + 200 | `summary.reasoning_effort`: `low`; one of `none`, `low`, `medium` |
| Memory extraction | `DOCCHAT_OPENAI_SMALL_DEPLOYMENT` | `memory.max_completion_tokens`: 1,200; 300 to 8,000 | `memory.reasoning_effort`: `low`; one of `none`, `low`, `medium` |
| Table summary (ingestion) | `DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT` | 2,000 | `low` |

- The answer call is the only streamed call: `stream=True`, `stream_options={"include_usage": True}`. Every other call is non-streamed and reads `usage` from the response.
- No call sends `temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `logprobs`, `top_logprobs`, `n`, `stop`, `tools`, `tool_choice`, `functions`, `max_tokens`, `user` or `verbosity`. The answer, summary and table-summary calls send no `response_format`; the rewrite, search-passage, follow-ups and memory calls use strict JSON-schema structured outputs. A parameter the pinned `openai` package doesn't name travels in `extra_body` under its JSON name.
- Every call made for a user (answer, rewrite, search passage, follow-ups, summary, memory) sends `safety_identifier` = the first 32 hexadecimal characters of SHA-256 over the UTF-8 string `docchat:{tid}:{oid}`; the table-summary call, made for no user, sends none.
- Prompt caching per deployment: `DOCCHAT_OPENAI_CHAT_PROMPT_CACHE` and `DOCCHAT_OPENAI_SMALL_PROMPT_CACHE`, each `disabled` (default: `extra_body={"prompt_cache_options": {"mode": "explicit"}}` with no breakpoints) or `automatic` (no cache parameter; required for models before GPT-5.6). The per-call marker of ADR 0063 makes every prompt unique after the static rules, which are shorter than the 1,024-token minimum, so implicit caching would only add write charges.

**4. Model capability table.** `docchat_core.chat.models.MODEL_CAPABILITIES`, keyed by model name:

| Model | Streaming Chat Completions | Strict structured outputs | `prompt_cache_options` | Accepted `reasoning_effort` |
|---|---|---|---|---|
| `gpt-5.6-sol` | yes | yes | yes | `none`, `low`, `medium`, `high` |
| `gpt-5.6-terra` | yes | yes | yes | `none`, `low`, `medium`, `high` |

Start-up fails when `DOCCHAT_OPENAI_CHAT_MODEL` or `DOCCHAT_OPENAI_SMALL_MODEL` names a model absent from the table, when a `*_REASONING_EFFORT` setting holds a value outside its model's row, or when a `*_PROMPT_CACHE` setting is `disabled` for a model without `prompt_cache_options`; the ops-config models refuse `summary.reasoning_effort` and `memory.reasoning_effort` values outside the chat-small model's row. The model names and versions (`DOCCHAT_OPENAI_CHAT_MODEL` `gpt-5.6-sol`, `DOCCHAT_OPENAI_SMALL_MODEL` `gpt-5.6-terra`, both `*_MODEL_VERSION` `2026-07-09`) come from the Bicep deployment parameters; no request field names a model.

**5. Deployed model check** (stage 3 acceptance, stage 4 acceptance and after any model change), in `dev` only: `docchat-ops --env dev chat model-check`, run by a member of `sg-docchat-dev-config-operators` from an allowlisted address with the operator's `AzureCliCredential` under that group's Cognitive Services User at `rg-docchat-dev`, which the bootstrap assigns and which permits both the Azure OpenAI calls and the Content Safety calls below; any other `--env` exits 2, `prod` runs the same pinned model names and versions, and every result is printed and written to the audit row `model_deployments.checked`. It makes one call per deployment and per configured effort (the answer's, the small calls', and the summary and memory efforts), each with its prompt-cache setting, plus strict structured output on chat small for the `standalone_query`, `search_passage` and `followup_questions` schemas and the memory-extraction schema; any `400` fails it. The same run makes 20 `summary`-shaped and 20 `memory`-shaped chat-small calls with `summary.reasoning_effort` and `memory.reasoning_effort` and fails when either p95 exceeds `summary.timeout_seconds` or `memory.timeout_seconds`, and sends the two Prompt Shields probes of ADR 0062 to the `dev` Content Safety account (`{"userPrompt": "Works on supplier contract renewals", "documents": []}`, and an empty `userPrompt` with an indirect-attack document), recording each status code and `attackDetected`. A value the check refuses is removed from the table in code, after which the settings and ops-config models reject it. If a deployment refuses `none`, `none` leaves that model's row, `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT` becomes `low`, `chat.small_call_max_completion_tokens` is raised to 6,000 and the capacity check of ADR 0147 runs again (the chat-small charge per turn becomes 59,390 tokens, needing at least 713 chat-small units in `prod`).

**6. Encodings are settings, checked against reported usage.** `DOCCHAT_OPENAI_CHAT_ENCODING`, `DOCCHAT_OPENAI_SMALL_ENCODING` and `DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING` (each `o200k_base` by default; `o200k_base` or `cl100k_base`) come from each deployment's Bicep `encoding` field, and each embedding model's encoding travels in `DOCCHAT_OPENAI_EMBEDDING_MODELS` (`cl100k_base`). They are never derived from model names. Encodings load from the cache baked into the image (`TIKTOKEN_CACHE_DIR`); start-up fails when one can't load. The tokenizer check, run with the model check, makes one non-streamed call per chat and chat-small deployment with a constant probe prompt of about 2,000 tokens and `max_completion_tokens = 16`; when the local count differs from `usage.prompt_tokens` by more than 1%, the check fails and the operator sets the encoding the model's documentation names.

## Alternatives considered

- **The Responses API**, Microsoft's recommendation for new apps. Rejected: it stores responses server-side by default, which would keep conversation content outside the product's own store, access rules and retention. Chat Completions on the v1 API covers every need here: streaming, strict structured outputs, reasoning effort and usage.
- **The api-version-dated endpoint with the `AzureOpenAI` client.** Rejected: the v1 API is GA, needs no api-version to manage and works with the standard client.
- **API keys.** Rejected: local authentication is off on the account (`disableLocalAuth: true`), and Entra tokens from the workload identity need no stored secret.
- **The service's default effort (no `reasoning_effort`).** Rejected: gpt-5.6 defaults to `medium`, which delays the first visible token and spends reasoning tokens on every call. An explicit effort per call makes latency and cost a reviewed setting.
- **Encodings from `tiktoken.encoding_for_model`.** Rejected: it raises `KeyError` for names it doesn't know, and a silent fallback could miscount the token budget. Settings checked against `usage.prompt_tokens` catch a wrong encoding at the stage gate.
- **Checking capabilities on first use.** Rejected: a model, effort or parameter a deployment refuses must fail at start-up or at the stage gate, never on a user's turn.
- **A non-reasoning chat model, to keep sampling parameters.** Rejected: the gpt-4.1 family is Deprecated for new subscriptions and retires on 2027-04-14.
- **Cognitive Services OpenAI User for the `dev` model check, with a second role for the Prompt Shields probes.** Rejected: no built-in role holds the Content Safety data actions alone, and Azure AI Developer, the other built-in role that holds them, also grants Machine Learning workspace writes and `Microsoft.Resources/deployments/*`. Cognitive Services User covers both data planes in one assignment; its key-listing action yields no usable key, because both accounts have local authentication off.

## Consequences

- Easier: one client and one table hold every model assumption, and the settings models reject a value before the app serves traffic.
- A model change is a code release (its capability row) plus a Bicep change, then the model check, the tokenizer check, the capacity check (ADR 0147) and a price in `pricing.model_rates`, in that order (the model-change runbook in 86-operations.md).
- The model stub used in `load`, local runs and CI answers `400 unsupported_parameter` to `temperature`, `top_p`, `max_tokens` and the penalty parameters, and `400 unsupported_value` to any effort other than `none`, `low`, `medium` and `high`, so a stray parameter or effort fails tests.
- Tests: unit, start-up refuses `DOCCHAT_OPENAI_SMALL_MODEL` naming a model absent from the table and an effort outside the model's row, and the ops-config model refuses `memory.reasoning_effort = none` once `none` leaves the chat-small row; operator checks in `dev`, recorded by the stage's performer from the `chat model-check` run, the model check returns no `400`, the tokenizer check is within 1% for each deployment it calls, each chat-small latency p95 is within its timeout, and the two Prompt Shields probes pass or record the `400` outcomes of ADR 0062.
- Not using the Responses API gives up its server-side conversation state and the features only it offers; the product needs neither.
- Risk: the model and tokenizer checks call the chat and chat-small deployments. `ingest-summary` runs the chat-small model and version by default, so they cover it; a model set on `ingest-summary` alone would reach no deployed check.
- Risk: the table-summary call sends no cache parameter, so implicit caching applies on `ingest-summary`; for table prompts of at least 1,024 tokens, GPT-5.6 can charge a cache write on each call with few reads to recover it, and the ingestion run counters record prompt and completion tokens only.
- Risk: a deployment that refuses `none` raises the chat-small charge, which `prod` capacity must then cover (713 units at the 6,000-token cap).

## References

- v1 API lifecycle (GA; Responses recommended, Chat Completions supported): https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle (checked 2026-10-14)
- Entra ID with the v1 API (`https://ai.azure.com/.default`; SDK default retries): https://learn.microsoft.com/en-us/azure/foundry/openai/supported-languages (checked 2026-10-14)
- Reasoning models (`reasoning_effort`, `max_completion_tokens`, 25,000-token reserve): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning (checked 2026-10-14)
- GPT-5 series on Chat Completions (no sampling parameters): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/chatgpt (checked 2026-10-14)
- Chat Completions reference (`stream_options`, `safety_identifier`): https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat (checked 2026-10-14)
- Prompt caching (`prompt_cache_options`, `cache_write_tokens`, 1,024-token minimum): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching (checked 2026-10-18)
- Models and retirement dates: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure and https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule (checked 2026-10-14)
- `openai` package: https://pypi.org/project/openai/ (3.31.0) and https://pypi.org/project/openai/3.30.0/ (client defaults) (checked 2026-10-14)
- `tiktoken` 0.14.0: https://pypi.org/project/tiktoken/0.14.0/ (checked 2026-10-14)
- Built-in roles (Cognitive Services User, Cognitive Services OpenAI User, Azure AI Developer): https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/ai-machine-learning (checked 2026-10-25)
- Content Safety access with Microsoft Entra ID (Cognitive Services User): https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview (checked 2026-10-25)
