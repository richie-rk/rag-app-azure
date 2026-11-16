# R29 — Budget alert at 80% or 100%

**Performer:** an operator (Reader on the environment group).

**Trigger:** `actual-80` or `actual-100` for `budget-docchat-<env>`.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Cost by component this month:

   ```bash
   az rest --method post --url "https://management.azure.com/subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env>/providers/Microsoft.CostManagement/query?api-version=2026-06-01" --body "{\"type\": \"ActualCost\", \"timeframe\": \"MonthToDate\", \"dataset\": {\"granularity\": \"None\", \"aggregation\": {\"totalCost\": {\"name\": \"Cost\", \"function\": \"Sum\"}}, \"grouping\": [{\"type\": \"TagKey\", \"name\": \"docchat-component\"}]}}"
   ```

   Expected: the month's cost per `docchat-component` tag value.

2. A forgotten load environment:

   ```bash
   az group show --name rg-docchat-load --query tags
   az resource list --resource-group rg-docchat-load --query "length(@)"
   ```

   The tags show `docchat-load-run` and the count is more than 0: when its run has ended, R11 step 13 (the nightly guard removes it otherwise).

3. Model spend: the `budget` tiles "Estimated model cost per day by deployment" and "Tokens per day by deployment and kind" and the `quota` workbook. One user or project driving the spike: lower `quota.tokens_per_day` with R03, knowing that it applies to every user; broad growth: step 4.

4. Expected growth: raise `budget.amount` in `infra/env/<env>.json` by pull request and R01.

5. Record the cause and the decision in the ticket.

## Verification

None of its own: step 5 records the cause and the decision, and a change it leads to is verified by its own runbook (R03, R11 or R01).

## Undo

None needed.

## Audit record

The ticket; any R03 or pull-request change it led to.
