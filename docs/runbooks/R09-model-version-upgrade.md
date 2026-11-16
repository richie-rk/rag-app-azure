# R09 — Model version upgrade

**Performer:** a maintainer; for step 4, an operator at level `config` in `dev` (`86-operations.md` section 1.3).

**Trigger:** a `Model retirement: <deployment> <model> <version>` issue from the nightly `model-watch` job; the preflight's `model_retires_soon`; the alerts `ttft-p95`, `chat-openai-throttling`, `prices-missing`, `openai-first-chunk-latency` and `openai-call-latency`; a planned upgrade.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5. For step 4, a machine whose public address is in `dev`'s `DOCCHAT_OPERATOR_IP_ALLOWLIST`, with `.azure/dev/.env` refreshed (`azd env refresh -e dev --no-prompt`).

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. Choose the replacement version and its deployment type (`GlobalStandard` unless the product owner decides otherwise) from Microsoft Learn's model availability and retirement pages for the environment's region. A `model_retires_soon` finding is resolved within its 6-month window.

2. A new model name first needs its row in the model capability table, a code release (`12-chat-pipeline.md` section 10.7); open and merge that pull request before step 3.

3. Edit the deployment's object (model, version, type, capacity) in `openAi.deployments` or `openAi.embeddingModels` of `infra/env/dev.json` (`81-config.md` sections 4.3 and 4.4) and open the pull request:

   ```bash
   gh pr create --title "Model version upgrade: <deployment> <model> <version>" --body "<ticket and the Microsoft Learn pages read>"
   ```

   Its `preflight-whatif` job (`83-infrastructure.md` section 10.5) runs the preflight against `dev`, model checks included, and the what-if. Expected: the job passes and its summary shows the deployment change. A failed model check fails the job and names the deployment (`model_version_not_offered`, `model_sku_not_offered`, `model_retired`, `model_deprecated` or `model_retires_soon`); choose the version once more (step 1). A `quota_insufficient` finding gives the shortfall in units of the model's quota for that type.

4. Chat and chat-small deployments, after the `dev` deploy (R01): an operator holding level `config` in `dev`, whose group's Cognitive Services User at `rg-docchat-dev` permits the Azure OpenAI and Content Safety calls, gets the membership as R03 steps 2 and 3 (PIM activation, or a group owner's `docchat-ops jit grant --level config`, then `az logout` and `az login --tenant <tenant ID>`), then runs the deployed model check, the tokenizer check, the chat-small latency check and the Prompt Shields probes (`12-chat-pipeline.md` section 10.7):

   ```bash
   uv run docchat-ops --env dev chat model-check --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0, each call's deployment, kind, effort and status, each tokenizer probe's local and reported counts, each latency set's p95 and timeout, and both Prompt Shields probes `passed`. Exit 2 names each failed call, latency set or probe. A tokenizer difference above 1% means setting the encoding the model's documentation names (`DOCCHAT_OPENAI_CHAT_ENCODING` or `DOCCHAT_OPENAI_SMALL_ENCODING`, from the deployment object's `encoding`) by pull request. `prod` runs the same pinned model names and versions, whose availability the preflight checks against each deployment's type in the region (step 10).

5. When a capacity changed, run the capacity check (R03 step 4):

   ```bash
   uv run docchat-ops --env <env> chat capacity-check
   ```

   Expected: both shares printed, exit 0. In `prod`, exit 2 means a share above 25%: raise the capacity in the deployment's object before going on; exit 4 means a capacity output is absent from `.azure/prod/.env` (refresh it and rerun).

6. Before users reach the new model (`prices-missing`: a deployed model without a price), set its price with R03 (`pricing.model_rates`, the rates of the Azure OpenAI pricing page for the deployment's type, region and currency). Expected: the `budget` tile "Models without a price" stays empty and `prices-missing` silent for an hour.

7. Embedding deployments are never changed in place (`NoAutoUpgrade`): add a new `openAi.embeddingModels` entry with a new key (step 3), deploy (R01), re-index every logical index that uses the old pair into a new physical index (R05), and remove the old entry only when no physical index names its deployments.

8. Throttling (`chat-openai-throttling`): compare the deployment's capacity with the `openai` tile "Tokens by deployment and kind"; raise the capacity in its object (step 3), or lower `chat.answer_max_completion_tokens` with R03, and run the capacity check of step 5 after either change.

9. Latency (`openai-first-chunk-latency`, `openai-call-latency`, `ttft-p95`) with no model change in flight: compare the `openai` tiles "First-chunk latency p95 per deployment" and "Mean duration of successful calls by deployment and call" with the deployment's capacity and the region's service health. A threshold that doesn't fit the chosen models (`alerts.ttftP95Ms`, `alerts.openAiFirstChunkP95Ms`, `alerts.openAiCallMeanMs`) changes in `infra/env/<env>.json` by pull request.

10. `prod`: the same change in `infra/env/prod.json`, deployed with R01, its price set with R03 before the approval (release checklist item 5). Each `prod` deploy runs `chat capacity-check` in its `postprovision` hook and stops with `chat_capacity_exceeded` while a share is above 25%.

11. Close the issue when the next nightly `model-watch` passes:

    ```bash
    gh run list --workflow nightly.yaml --limit 1
    gh issue close <issue number> --comment "model-watch passed in run <run ID>"
    ```

    Expected: the newest `nightly.yaml` run's `model-watch` job succeeded, and the issue closes.

12. Revoke the level of step 4, or let PIM expire; then sign out:

    ```bash
    uv run docchat-ops --env dev jit revoke --level config --member <operator object ID> --ticket <id> --reason "work finished"
    az logout
    ```

## Verification

The nightly `model-watch` passes for `dev` and `prod` with no `Model retirement:` issue open for the deployment; `chat model-check` exited 0 in `dev` for a changed chat or chat-small deployment; `prices-missing` stays silent for an hour after users reach the new model.

## Undo

The pull request reverted and deployed (R01). An embedding entry added in step 7 is removed only while no physical index names its deployments.

## Audit record

The pull requests, the deploy runs, the `model_deployments.checked` row of step 4 and the `ops_config.set` row of the price; the membership change in Entra ID's audit log (`jit` commands write no audit row); the ticket.
