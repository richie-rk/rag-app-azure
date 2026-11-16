# ADR 0076: Memory in the prompt, pause and platform switch

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 80-data-model.md, 12-chat-pipeline.md, 11-api.md, 81-config.md, 82-security.md, 10-web-client.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0024, 0028, 0033, 0063, 0073, 0074, 0075, 0077, 0081

## Context

Each user has a memory of durable facts about themselves (role, team, expertise, recurring topics, preferred answer style) that they can view, edit, delete, clear and pause. ADR 0075 decides which facts are written (at most `memory.max_facts`, default 50, and `memory.max_tokens`, default 1,000 tokens of fact text, each fact at most 200 code points); this ADR decides how stored facts reach the answer prompt, and what pausing and the operator switch do.

- The answer prompt has a set order: platform rules, project instructions, the user's memory facts in their own block within their own token cap, the rolling summary, recent turns, sources, the question. Memory is labelled as preferences the user stated and can't override the platform rules, and while memory is paused nothing is extracted or used.
- Memory is text the user wrote, or text taken from the user's own questions, so it must never carry the platform's authority: a fact worded as an instruction would otherwise compete with the platform's rules in every later answer. Only platform-owned and admin-owned text goes in the system message (ADR 0063).
- Facts reach every prompt in every session of their owner, so an instruction that slipped into memory would persist. In Chat Completions the deployment's harm filters and its Prompt Shields for jailbreak attacks analyse only the latest `user` content, which is the question, so the stored block is never filtered at request time; ADR 0075 screens each fact once, when it is written.
- The prompt budget (`chat.context_token_budget` 40,000 − `chat.answer_max_completion_tokens` 12,000 = 28,000 prompt tokens by default, ADR 0063) drops earlier turns, then sources, but never the context message that carries memory and the summary, so memory needs a cap of its own.
- Operators need to turn memory off for everyone without a release (an incident, a model problem, a policy change), and users must keep access to what is stored about them while it is off.

## Decision

**Prompt lines.** `ConversationStore.get_prompt_memory(oid) -> list[str]` (`docchat_api.conversations.store`, ADR 0073), which the chat pipeline awaits at turn start, point-reads the caller's `memory` document (container `memories`, partition key and `id` = `oid`) and:

1. Returns `[]` when ops config `memory.enabled` is `false`, when `paused` is `true`, when the document is missing or holds no facts, or when the read fails. A failed read never raises: it logs `memory_unavailable`, increments `docchat.memory.read_failures`, and the turn continues without memory.
2. Sorts the facts by category in the order `role`, `team`, `expertise`, `topic`, `answer_style`, then by `updatedAt`, newest first.
3. Builds one line per fact, `{label}: {text}`, with the labels `Role`, `Team`, `Expertise`, `Recurring topic` and `Answer preference`.
4. While the lines as the chat pipeline renders them (each prefixed `- `, joined with LF), counted with the chat deployment's encoding (`DOCCHAT_OPENAI_CHAT_ENCODING`, default `o200k_base`), exceed `memory.prompt_max_tokens` tokens, removes the fact with the oldest `updatedAt`.
5. Returns the list. Its length is the number of facts the answer call sees, recorded as `promptBudget.memoryFactsIncluded` in the turn trace.

`memory.prompt_max_tokens`: ops config, integer, default 1,300, range 200 to 5,000, typed by `MemoryOps`.

**Placement.** The chat pipeline's `render_memory_block` prefixes each line with `- ` and joins the lines with LF inside a `USER_PREFERENCES` block, with the per-call marker and defanged text of ADR 0063. The block sits in the context message: a `user`-role message after the system message, sent only when memory lines or a rolling summary exist, holding a one-line note that it needs no reply, then the `USER_PREFERENCES` block (when there is at least one line), then the `CONVERSATION_SUMMARY` block. The platform rules tell the model that this block lists preferences the user stated, to be used only to adapt style and emphasis, and that nothing in the preferences can change, suspend or override the rules. Memory text never enters the system message. The budget never drops the context message, so `memory.prompt_max_tokens` bounds memory's share of every prompt.

**Pause.** `updateMemorySettings` (`PATCH /v1/memory`, body `{"paused": true}` or `{"paused": false}`, `If-Match` required, ADR 0077) sets `paused`, `pausedAt` (set when pausing, left unchanged when resuming), `userEditedAt` and `updatedAt`; a request for the value already stored answers `200` with no write and no audit row. While paused:

- `get_prompt_memory` returns `[]`, so no prompt carries the block;
- finalisation still marks a completed turn's extraction `pending` (when `memory.enabled` is `true` and the question wasn't a suggestion), and the post-turn task reads the memory document first and patches the turn to `skipped_paused` with no model call;
- facts stay viewable, editable, deletable and clearable.

Resuming extracts only from turns completed after the resume: every earlier turn's `endedAt` is before the `userEditedAt` that the resume wrote, so an extraction of it that runs later ends `superseded` (ADR 0075).

**Platform switch.** Ops config `memory.enabled` (boolean, default `true`, `MemoryOps`) turns extraction and the memory block off for every user: `get_prompt_memory` returns `[]`, finalisation marks completed turns `not_applicable`, and an extraction that runs while it is `false` ends `skipped_disabled`. Every memory operation (`getMemory`, `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory`) keeps working; `getMemory` returns `enabled: false` and `getCurrentUser` returns `features.memoryEnabled: false`. The memory screen then shows an informational bar saying memory is off for everyone, and its pause switch, edit, delete and clear controls keep working. Facts stay stored until their owner clears them or the user-data deletion procedure runs (ADR 0033). Operators change the key only with `docchat-ops config set`, under just-in-time level `config` (ADR 0024, ADR 0028); each API worker refreshes its ops-config snapshot on the first access after 60 seconds, so a committed change is live within 60 seconds whenever SQL is reachable. Switching it back on extracts from turns finalised afterwards; turns finalised while it was off stay `not_applicable`.

**Telemetry.** Span `docchat.memory.prompt_read` with `docchat.memory.active`, `docchat.memory.facts_rendered` and `docchat.outcome` (`ok`, `empty`, `unavailable`); never fact text.

## Alternatives considered

- **Memory in the system message.** Rejected: the system role carries the platform's authority, and user-written text there would compete with the rules.
- **Memory in its own message beside the question.** Rejected: the question must stay the latest `user` message so the deployment's filters judge it alone, and the set order puts memory ahead of the summary and the turns.
- **Only the facts relevant to each question (embedding similarity).** Rejected: an embedding call per turn and a vector per fact for a memory of at most 50 facts and 1,000 tokens, prompts that vary from run to run, and retrieval over conversation data is outside the first version's scope.
- **Drop the newest facts, or whole categories, when over the cap.** Rejected: a user's latest statement is the likeliest to be current, so the least recently updated fact goes first.
- **No cap of its own (rely on the 1,000-token fact cap).** Rejected: the cap must bound the rendered block, labels included, and must hold when operators raise `memory.max_facts` or `memory.max_tokens`.
- **Pause by hiding or deleting facts.** Rejected: pausing must leave every fact on the memory screen, ready for resume.
- **A switch that also hides the memory screen or deletes facts.** Rejected: users keep control of what is stored about them whether or not the feature runs; facts are deleted only by their owner or by the user-data deletion procedure.
- **A per-project memory switch.** Rejected: memory belongs to the user across projects; the platform switch and the user's own pause cover the need.

## Consequences

- Sizing, measured on 2026-10-18 with the `o200k_base` encoding file (SHA-256 `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`): the `- ` prefix, the label, the colon and space, and the line break add 3 to 7 tokens to a line, so a memory at its default caps (50 facts holding 1,000 tokens of fact text) renders to 1,150 to 1,350 tokens; 300 random blocks of 50 facts with 20 tokens of text each rendered to 1,197 to 1,226 tokens. At or below 1,300 tokens the whole memory reaches the prompt; above, the excess is at most 50 tokens, and the cap removes least recently updated facts until it is gone.
- An operator who raises `memory.max_facts` or `memory.max_tokens` also raises `memory.prompt_max_tokens`, under the same ticket, by the added fact tokens plus 7 tokens per added fact, up to its 5,000-token maximum; otherwise the cap leaves out more facts.
- A fact left out by the cap stays stored and visible on the memory screen; the trace's `memoryFactsIncluded` shows how many facts each answer saw. The block costs at most 1,300 of the 28,000 prompt tokens.
- A pause takes effect from the user's next turn, because the memory document is read at every turn start; a turn already streaming keeps its prompt, and its extraction ends `skipped_paused`. The switch takes effect within 60 seconds.
- A failed memory read gives an answer without preferences rather than a failed turn; `docchat.memory.read_failures` makes it visible.
- While paused, each completed turn still costs one memory point read and one turn patch (`skipped_paused`).
- Preferences steer style and emphasis by design; a misread fact shapes answers until its owner edits or deletes it, and the memory screen shows every fact with its source.
- Tests: the prompt lines follow the category order and labels, newest first within a category, the cap removes the least recently updated facts, nothing is returned when paused, switched off or empty, and a failed read returns `[]`, logs `memory_unavailable` and increments the counter; with memory paused the captured answer prompt has no `USER_PREFERENCES` block and the extraction ends `skipped_paused`, and after resuming the next turn extracts; with `memory.enabled` false there is no block and no extraction, every memory operation works, and `getMemory` reports `enabled: false`.
- Runbook: turn memory off platform-wide with `docchat-ops config set memory.enabled false` under `docchat-ops jit grant --level config`; facts stay until their owners clear them.

## References

- Azure OpenAI content filtering with embedded documents (harm categories and Prompt Shields run on the latest user content): https://learn.microsoft.com/en-us/azure/foundry-classic/openai/concepts/content-filter-document-embedding (checked 2026-10-14)
- tiktoken 0.14.0 and the `o200k_base` encoding file with its SHA-256: https://pypi.org/project/tiktoken/0.14.0/ (checked 2026-10-14)
