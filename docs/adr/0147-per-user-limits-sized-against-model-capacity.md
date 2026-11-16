# ADR 0147: Per-user limits sized against model capacity

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 12-chat-pipeline.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 86-operations.md, 90-build-order.md
- **Related ADRs:** 0027, 0028, 0058, 0059, 0061, 0063, 0065, 0068, 0074, 0075, 0099, 0100, 0112, 0139

## Context

- The product owner asked for per-user rate limits and token quotas, enforced before any model call, with the Azure OpenAI tokens-per-minute limits as the service-level backstop. ADR 0058 sets the algorithms (a two-bucket window, stream leases, day and month quotas); this record decides how large the per-user values may be.
- Every chat turn spends capacity that the whole organisation shares, so one user's limits must leave room for everyone else. Ingestion has its own deployments (ADR 0100), so chat capacity serves chat users only.
- Vendor facts (checked 2026-10-14): the tokens-per-minute limiter estimates each request on arrival from its prompt size plus `max_completion_tokens`, not from billed tokens; requests per minute are enforced over 1-second or 10-second windows; a deployment's `sku.capacity` is in capacity units, 1,000 tokens per minute each for the models chosen; Global Standard quota is pooled per subscription per model, 1,000 units in Tier 1.
- So a user at the rate limit `L` reserves up to `L` times the per-request charge of every turn, whatever the answers' real length, and a per-user limit means something only against the capacity of the deployments it shares. In `prod` the chat deployment (`gpt-5.6-sol`) has 500 units and chat small (`gpt-5.6-terra`) 530; `dev` has 30 units each, serving developers only; `load` has no Azure OpenAI account (ADR 0112).

## Decision

1. **The invariant, in `prod`.** Both shares stay at or below 25%:
   - chat share = `ratelimit.chat_requests_per_minute` × `chat.context_token_budget` ÷ (chat capacity × 1,000);
   - chat-small share = `ratelimit.chat_requests_per_minute` × `S` ÷ (chat-small capacity × 1,000).
2. **`S`, the largest chat-small charge one turn and its after-response work can cause**, sums five calls, each counted as its input allowance plus its `max_completion_tokens`:
   - standalone-query call (`rewrite`) = 300 + `summary.max_summary_tokens` + 610 × `chat.rewrite_history_turns` + 4,000 + `chat.small_call_max_completion_tokens`;
   - search passage = 4,300 + `chat.small_call_max_completion_tokens`;
   - follow-ups = 5,100 + (500 + `followups.source_excerpt_tokens`) × `followups.source_count` + `chat.small_call_max_completion_tokens`;
   - memory = 6,500 + `memory.max_completion_tokens` (the memory call's input allowance, 13-conversations-and-memory.md);
   - summary = `summary.max_input_tokens` + 350 + `summary.max_completion_tokens`.

   The allowances count 300 tokens for each call's rules and block lines, 4,000 for the question (one per code point, the question limit), 610 for each earlier exchange in the standalone-query call (two 300-token cuts and their labels), 800 for the follow-ups' answer excerpt and 500 for each follow-ups source's title, section and block lines.
3. **Defaults.** `ratelimit.chat_requests_per_minute` 3 (range 1 to 1,200); `ratelimit.max_concurrent_streams` 2 (1 to 200); `chat.context_token_budget` 40,000. With the seeded values (`summary.max_summary_tokens` 800, `chat.rewrite_history_turns` 4, `chat.small_call_max_completion_tokens` 800, `followups.source_excerpt_tokens` 300, `followups.source_count` 3, `memory.max_completion_tokens` 1,200, `summary.max_input_tokens` 12,000, `summary.max_completion_tokens` 2,000), `S` = 8,340 + 5,100 + 8,300 + 7,700 + 14,350 = 43,790 tokens. The shares are 3 × 40,000 = 120,000 ÷ 500,000 = 24% on chat and 3 × 43,790 = 131,370 ÷ 530,000 = 24.8% on chat small.
4. **One function.** `docchat_core.chat.capacity.capacity_shares(ops, chat_capacity_units, small_capacity_units)` computes both shares and backs three checks:
   - a unit test of the seeded defaults against the capacities in `infra/env/prod.json` (0.24 and 0.248);
   - the read-only command `docchat-ops chat capacity-check [--env <env>] [--set <key>=<json>]`: capacities from the azd outputs `DOCCHAT_OPENAI_CHAT_CAPACITY` and `DOCCHAT_OPENAI_SMALL_CAPACITY` (written from the same `openAiDeployment` objects that create the deployments), values from `app.ops_config`; `--set` evaluates a proposed value without writing it; no `--ticket` or `--reason`; a service-principal caller is accepted. It prints both shares and exits 0 when they pass, 2 when either exceeds 25% in `prod`, 4 in `prod` when a capacity output is empty or absent (which counts as absent) and 7 when the store can't be reached; in `dev` it exits 0 whatever the shares, and in `load` it exits 0;
   - the deployment: from build stage 3 on, the migrations-and-grants step runs the command as the deploy identity right after migrations and grants, inside the same SQL access window, and copies its output to the job summary. In `prod`, exit 2 fails the step with `chat_capacity_exceeded` and any other non-zero exit with `chat_capacity_check_failed`, so no `prod` release deploys while the invariant is unproven; in `dev` it only reports; `load` skips it.
5. **Changing a value.** In `prod`, raising `ratelimit.chat_requests_per_minute`, `chat.context_token_budget` or any input of `S` starts with `docchat-ops chat capacity-check --env prod --set <key>=<json>` under the `config` just-in-time level. When it exits 2: raise the deployment's capacity in `infra/env/prod.json`, within the subscription's Global Standard quota (which the region preflight checks), deploy, run the check again, and only then run `docchat-ops config set`. The ops-config models keep every input inside its range.
6. **Throttling.** On the Azure OpenAI throttling alert the operator compares the deployment's tokens-per-minute capacity with use and raises capacity through Bicep; lowering `chat.answer_max_completion_tokens` also lowers each request's charge on arrival. The check runs again after either change. A deploy failed by the check is resolved by raising the capacity or lowering the limit with `docchat-ops config set`, then deploying again.

## Alternatives considered

- **Limits chosen without reference to capacity.** Rejected: at 6 requests per minute one user could reserve 48% of the chat deployment, so two users at their limits would refuse everyone else.
- **A per-user tokens-per-minute limiter in the API.** Rejected: it needs a token estimate on arrival and another window per user, while the request window already bounds each user's reservations, because the context budget bounds every request's charge.
- **A 50% share.** Rejected: two users at their limits would fill a deployment.
- **A 10% share.** Rejected: 3 requests per minute would need 1,200 units of chat and 1,314 of chat small, beyond the 1,000-unit Tier-1 pool that `dev` and `prod` share.
- **Checking only in CI against `infra/env/prod.json`.** Rejected: ops-config values change at run time through `docchat-ops config set`, so the deployment checks the live rows.
- **Provisioned throughput for chat.** Rejected: it costs capacity whether or not anyone asks a question, and Global Standard is the product owner's default deployment type (ADR 0099).

## Consequences

- One function computes both shares, a `prod` deploy fails before an unsafe value runs, and an operator can evaluate a change before making it.
- Every raise of a formula input in `prod` needs a capacity change first, bounded by the subscription's quota: across `dev` and `prod` the defaults use 530 of 1,000 units of `gpt-5.6-sol` and 740 of `gpt-5.6-terra`.
- At a 25% share, at least four users at their limits fit on a deployment before anyone gets `model_busy`; in total the 500-unit chat deployment admits about 12 answers per minute at the 40,000-token budget.
- The share bounds the window's per-minute rate. A 60-second span that crosses a minute boundary can admit up to 2 × `L` − 1 requests (5 at the default 3, ADR 0058), briefly reserving up to 40% of the chat deployment; the deployment's own limiter answers anything beyond its capacity with `model_busy`, which is the backstop the product owner named.
- Chat-small failures only degrade an answer (the standalone query, broadening and follow-ups fall back; summaries and memory extraction defer), but a starved small model lowers answer quality for everyone, so chat small is held to the same share.
- Obligations (84-testing.md): a unit test that the seeded shares are 0.24 and 0.248, that `--set ratelimit.chat_requests_per_minute=4` exits 2 in `prod`, that `dev` exits 0 whatever the shares and `load` exits 0, and that an empty `DOCCHAT_OPENAI_SMALL_CAPACITY` is reported absent and exits 0 in `dev` and 4 in `prod`; a deployed test that every deploy from stage 3 runs the check and that the first `prod` deploy fails while either share is above 25%; a CLI test that the command is registered, refuses `--ticket` and `--reason`, accepts a service-principal caller and maps the exits; stage 3's acceptance (the `dev` deploy prints both shares) and stage 6's (the check passes in `prod`); runbooks for changing a limit, the throttling alert and a failed capacity check (86-operations.md).

## References

- Azure OpenAI quota management and rate limits: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota (checked 2026-10-14)
- Azure OpenAI quotas and limits: https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits (checked 2026-10-14)
- Deployment types: https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types (checked 2026-10-14)
