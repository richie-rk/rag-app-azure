# R11 — Run the load test

**Performer:** a maintainer starts the run; the tenant administrator (Cloud Application Administrator or Application Administrator, `86-operations.md` section 1.1), who is also in `sg-docchat-operators` for step 3's reads, clears the access gate; a Conditional Access Administrator in `sg-docchat-operators` makes the policy changes of steps 5 and 11; the bootstrap operator performs step 13's resource deletes and purges.

**Trigger:** a change that release checklist item 2 names (a plan SKU, the Gunicorn worker count, Durable concurrency, the Search tier or replicas, or a new model call on the chat path), or a capacity question.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; for the gate (steps 3 to 7) a machine whose address is in the shared allowlist (R24 step 4), because step 6 reads the shared test vault. The workflow, scenarios and verdicts are in `84-testing.md` section 7; the environment's lifecycle is `83-infrastructure.md` section 10.14. `load-test.yaml` takes `deploy_run_id` (the `deploy.yaml` run whose release to test), `scenarios` (`all` or a comma list of `s1`, `s2`, `s3`, `s4`), `rate` (optional, turns per second for S3) and `answer_tokens` (default 80, 80 to 4,000).

Placeholders in angle brackets are filled in at run time; a value from `.azure/load/.env` is written by its output name. `<DOCCHAT_RESOURCE_TOKEN>` is the run's resource token, new for every run. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy.

Approving or rejecting the gate (`86-operations.md` section 1.5):

```bash
gh api /repos/<owner>/<repo>/environments/load-access --jq .id
gh api --method POST /repos/<owner>/<repo>/actions/runs/<run ID>/pending_deployments \
  -F "environment_ids[]=<environment ID>" -f state=<approved|rejected> -f comment="<comment>"
```

## Steps

1. Pick a release:

   ```bash
   gh run list --workflow deploy.yaml --status success --limit 5 --json databaseId,headSha
   ```

2. Start the run:

   ```bash
   gh workflow run load-test.yaml -f deploy_run_id=<run ID> -f scenarios=all
   ```

   Expected: `manifest`, `create` and then `access-gate` waiting for review.

3. At the gate, from a machine in the shared allowlist:

   ```bash
   az login --tenant <tenant ID>
   azd env new load --subscription <subscription ID> --location <region> --no-prompt
   azd env set AZURE_RESOURCE_GROUP rg-docchat-load
   azd env refresh -e load --no-prompt
   ```

4. The run's registrations, assignments and consent:

   ```bash
   uv run python scripts/identity_post_provision.py --environment load --resource-token <DOCCHAT_RESOURCE_TOKEN> --users-group <sg-docchat-load-users object ID> --admins-group <sg-docchat-load-admins object ID> --test-users-group <sg-docchat-load-test-users object ID> --test-admins-group <sg-docchat-load-test-admins object ID>
   ```

   Expected: exit 0.

5. The Conditional Access Administrator adds the run's API and test-client applications to the excluded applications of the Conditional Access policy `DocChat test users: non-production APIs only`:

   ```bash
   az rest --method PATCH --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies/<policy ID> --body @<file with the full excludeApplications list>
   ```

6. Check the `load` passwords:

   ```bash
   uv run docchat-ops --env load testusers check --set load --min-hours 24 --vault-uri $(gh variable get DOCCHAT_TEST_VAULT_URI --env load-tests)
   ```

   Expected: exit 0 (every `load` password stays valid for 24 hours). Exit 5: reject the gate with `-f state=rejected -f comment="load passwords expire"`, let the run's `teardown` finish, rotate the `load` set (R08 with `--set load`) and start a new run.

7. Approve the gate (environment `load-access`) with the comment `identities ready`.

8. Follow the run:

   ```bash
   gh run watch <run ID>
   ```

9. Read the verdict table in the run summary (job `verdict`); download the evidence:

   ```bash
   gh run download <run ID> --pattern "load-results-*"
   ```

10. After `teardown`, confirm that nothing of the run remains:

    ```bash
    az resource list --resource-group rg-docchat-load --query "length(@)"
    az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-load-<DOCCHAT_RESOURCE_TOKEN>-web')"
    az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<app ID>')"
    az rest --method GET --url https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.application --query "value[?uniqueName && starts_with(uniqueName,'docchat-load-<DOCCHAT_RESOURCE_TOKEN>-')] | length(@)"
    az rest --method GET --url https://graph.microsoft.com/v1.0/directory/deletedItems/microsoft.graph.servicePrincipal --query "value[?appId=='<app ID>'] | length(@)"
    ```

    Expected: the resource list returns 0; the application read answers `404`, and the same for `-api` and `-test`; the service-principal read answers `404` for `WEB_CLIENT_ID`, `API_CLIENT_ID` and `TEST_CLIENT_ID` of step 3; both deleted-items reads return 0, the second for each of the three app IDs.

11. The Conditional Access Administrator removes the run's applications from the policy's exclusions (step 5 with the earlier list); the maintainer attaches the verdicts to the release ticket they support.

12. An `inconclusive` verdict: read its cause and measurements in the scenario's verdict file under `load-results/`, and start the follow-up run that `84-testing.md` section 7.5 prescribes for that cause, with the input it names:

    ```bash
    gh workflow run load-test.yaml -f deploy_run_id=<run ID> -f scenarios=s3 -f rate=<ceil(1.25 × R)>
    gh workflow run load-test.yaml -f deploy_run_id=<run ID> -f scenarios=s3 -f answer_tokens=<four times the last value>
    ```

    The first for cause `calibration_miss`, the second for `dependency_bound` (80, 320, 1,280, then 4,000). A `dependency_bound` verdict at an answer length of 4,000 tokens is stop SC-6: it goes to the product owner with its measurements (`90-build-order.md` section 1.4). A cause that names a `load` capacity is raised in `infra/env/load.json` by pull request before the next run.

13. Teardown failed (`teardown_incomplete` or `graph_delete_failed`):

    ```bash
    gh run rerun <run ID> --failed
    ```

    When resources or registrations remain: the bootstrap operator deletes each remaining resource (`az resource delete --ids <resource ID>`); the tenant administrator reads each remaining registration, deletes its service principal, then the application, each permanently (a `404` means it is gone):

    ```bash
    az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-load-<DOCCHAT_RESOURCE_TOKEN>-<web|api|test>')" --query "{id: id, appId: appId}"
    az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<appId>')" --query id -o tsv
    az rest --method DELETE --url https://graph.microsoft.com/v1.0/servicePrincipals/<service principal ID>
    az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<service principal ID>
    az rest --method DELETE --url https://graph.microsoft.com/v1.0/applications/<object ID>
    az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<object ID>
    ```

    The bootstrap operator purges a soft-deleted Key Vault or Cognitive Services account (both under Owner) only when a name collision blocks a run:

    ```bash
    az keyvault purge --name <vault>
    az cognitiveservices account purge --name <account> --resource-group rg-docchat-load --location <region>
    ```

    Then repeat step 10.

## Verification

Steps 9 and 10: every selected verdict is `passed` (or `inconclusive` with its follow-up run of step 12), and nothing of the run remains in `rg-docchat-load` or Microsoft Graph.

## Undo

The teardown.

## Audit record

The workflow run, the gate approval and the Activity Log; the load jobs' `ops_config.set` rows in the `load` database, gone with it.
