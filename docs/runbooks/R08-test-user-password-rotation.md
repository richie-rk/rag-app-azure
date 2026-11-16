# R08 — Test-user password rotation

**Performer:** a rotating operator (`86-operations.md` section 1.1: an operator who also holds Password Administrator scoped to the administrative unit `DocChat test users`), on a machine in the shared allowlist (the test vault) and in `dev`'s allowlist (the audit row).

**Trigger:** every 30 days for both sets (the nightly `test-credential-expiry` job warns 7 days before an expiry), and at once after a suspected exposure; never while a load test runs (`83-infrastructure.md` section 2.17).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. With an empty shared allowlist, add this machine's address first (R24 step 4, run while no test job reads the vault).

2. Sign in, refresh `dev`'s azd environment and export the vault URI and the user domain (both variables exist only in `dev-tests` and `load-tests`):

   ```bash
   az login --tenant <tenant ID>
   azd env refresh -e dev --no-prompt
   export DOCCHAT_TEST_VAULT_URI=$(gh variable get DOCCHAT_TEST_VAULT_URI --env dev-tests)
   export DOCCHAT_TEST_USER_DOMAIN=$(gh variable get DOCCHAT_TEST_USER_DOMAIN --env dev-tests)
   ```

3. Get the `config` membership in `dev` (R03 steps 2 and 3).

4. Rotate both sets:

   ```bash
   uv run docchat-ops --env dev testusers rotate --set all --ticket <id> --reason "30-day test password rotation"
   ```

   Expected: 25 aliases (4 of the `dev` set, 21 of the `load` set) with new expiry dates, exit 0.

5. Exit 5: run the same command once more; it completes the remaining aliases. Exit 3: check the Password Administrator assignment on the administrative unit, the `config` membership and both allowlists. Exit 2 with `load_run_in_progress`: run `--set dev` now and `--set load` after the load run's teardown.

6. Verify:

   ```bash
   uv run docchat-ops --env dev testusers check --set all
   gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke
   ```

   Expected: `testusers check` exits 0 and lists every expiry about 30 days ahead; the `smoke` run passes. The `load` set is proven by the next load run's token step.

7. Revoke the `config` membership (R03 step 8), and remove the address added in step 1 (R24 step 4 with the earlier list).

## Verification

Step 6.

## Undo

None; the earlier passwords are gone.

## Audit record

The `dev` row `test_users.rotated`; the Graph password changes in Entra's audit log; the secret versions in the vault.
