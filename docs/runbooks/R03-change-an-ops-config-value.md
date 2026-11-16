# R03 — Change an ops-config value

**Performer:** an operator at level `config` (`81-config.md` section 3.3).

**Trigger:** a ticket for a value; the alerts `ops-config-invalid`, `quota-rejections` and `rate-rejections-share`; a step of another runbook that names a key.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`).

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. Open a ticket with the environment, the key, the new JSON value and the reason.

2. Get the membership: PIM activation, or a group owner runs this and sends the receipt to the ticket:

   ```bash
   uv run docchat-ops --env <env> jit grant --level config --member <operator object ID> --hours 1 --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0 and the receipt (exit 6: the operator is already a member).

3. Sign in again, so the new token carries the membership:

   ```bash
   az logout
   az login --tenant <tenant ID>
   ```

4. Linked checks, before the change:
   - In `prod`, a key in either capacity formula (`ratelimit.chat_requests_per_minute`, `chat.context_token_budget` or any input of the chat-small charge, `12-chat-pipeline.md` section 3.7):

     ```bash
     uv run docchat-ops --env prod chat capacity-check --set <key>=<JSON value>
     ```

     Exit 0: continue. Exit 2: raise the deployment's capacity in `infra/env/prod.json` by pull request, deploy it (R01), run the check once more and continue only on exit 0.
   - `ingestion.max_parallel_items` raised: R18 step 8 first.
   - `pricing.model_rates` or `pricing.currency`: the rates come from the Azure OpenAI pricing page for each deployment's type, region and currency.

5. Read the current value:

   ```bash
   uv run docchat-ops --env <env> config show --key <key>
   ```

   Expected: the current value, default, last change time and actor; note the value for the undo.

6. Set the value; in `prod` type `prod` when asked:

   ```bash
   uv run docchat-ops --env <env> config set --key <key> --value '<JSON value>' --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0 and "live within 60 seconds". Exit 2 quotes the allowed range or the broken rule; exit 6 means someone changed the row: repeat from step 5.

7. Wait 60 seconds, then:

   ```bash
   uv run docchat-ops --env <env> config show --key <key>
   ```

   Expected: the new value and your object ID. Check the workbook of the key's area: `api.*`, `ratelimit.*` and `quota.*` on `quota`; `chat.*`, `retrieval.*` and `followups.*` on `ttft`; `pricing.*` on `budget`; `conversations.*`, `summary.*`, `memory.*` and `feedback.*` on `conversations`; `ingestion.*` on `ingestion`.

8. Revoke, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

## Changes this runbook carries

| Change | Rule |
|---|---|
| Memory off platform-wide | `memory.enabled` `false`: extraction and memory blocks stop within 60 s; users keep their memory screens, and facts stay stored until users clear them or R06 deletes them (`13-conversations-and-memory.md` section 10.12) |
| Prices | `pricing.model_rates` and `pricing.currency` for every deployed model; afterwards the `budget` tile "Models without a price" stays empty and `prices-missing` silent for an hour; `prod` opens to users only once every deployed model has a rate |
| Quotas | `quota.*` are global: raising one raises it for everyone, and no per-user exception exists |
| Retrieval tuning | `retrieval.reranker_threshold` and `retrieval.min_confident_results`, once evaluation results exist; then watch the `ttft` tiles "Broadened-search rate" and "Time to first token p50, p95, p99" for a day |
| Summary or memory completion caps | `summary.max_completion_tokens` and `memory.max_completion_tokens` are inputs of the chat-small charge: the capacity check of step 4 applies in `prod` |
| Ingestion retention | `ingestion.run_retention_days` and `ingestion.durable_history_retention_days`; the next 02:40 UTC maintenance applies them |
| `ops-config-invalid` | the event names the area and key: `config show --key <key>` shows the stored value; set a valid value or `config reset`; the area keeps its last valid values meanwhile |
| `quota-rejections`, `rate-rejections-share` | the `quota` tiles "Rejections by reason" and "Distinct users rejected per hour, by operation": many users near a limit is a limit to raise (with the capacity check in `prod`); one user is R07 step 2's case |
| `load` values | the load workflow's `seed-config`, `stream-reset`, `s4-set1-config`, `s4-set2-config` and `s4-reset` jobs apply them (`81-config.md` section 3.9); nobody sets them by hand |

## Verification

Step 7: `config show` returns the new value and your object ID within 60 seconds of step 6.

## Undo

Reset the key to its default, or run `config set` with the value noted in step 5:

```bash
uv run docchat-ops --env <env> config reset --key <key> --ticket <id> --reason "<reason>"
```

## Audit record

`ops_config.set` or `ops_config.reset`.
