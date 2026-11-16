# R16 — Repository and GitHub settings

**Performer:** an owner of the GitHub organization.

**Trigger:** once, and after any re-creation of the repository.

**Prerequisites:** the GitHub CLI of `86-operations.md` section 1.5, signed in as an organization owner; a checkout of the repository on `main` (for `.github/rulesets/main.json`).

Placeholders in angle brackets are filled in at run time.

## Steps

1. Create the teams `docchat-maintainers`, `docchat-backend`, `docchat-frontend`, `docchat-platform`, `docchat-security`, `docchat-release-approvers` and `docchat-tenant-admins`, one call per team:

   ```bash
   gh api --method POST /orgs/<org>/teams -f name=<team> -f privacy=closed
   ```

2. Grant access: `docchat-maintainers` admin; `docchat-backend`, `docchat-frontend`, `docchat-platform` and `docchat-security` push; the two reviewer teams pull:

   ```bash
   gh api --method PUT /orgs/<org>/teams/<team>/repos/<org>/<repo> -f permission=<admin|maintain|push|pull>
   ```

3. Apply the ruleset:

   ```bash
   gh api --method POST /repos/<org>/<repo>/rulesets --input .github/rulesets/main.json
   ```

4. Security features, with a file setting `security_and_analysis.secret_scanning` and `secret_scanning_push_protection` to enabled:

   ```bash
   gh api --method PATCH /repos/<org>/<repo> --input <file setting security_and_analysis.secret_scanning and secret_scanning_push_protection to enabled>
   gh api --method PUT /repos/<org>/<repo>/vulnerability-alerts
   gh api --method PUT /repos/<org>/<repo>/automated-security-fixes
   gh api --method PUT /repos/<org>/<repo>/private-vulnerability-reporting
   ```

5. Actions, with a file listing the pinned actions of `83-infrastructure.md` section 10.3:

   ```bash
   gh api --method PUT /repos/<org>/<repo>/actions/permissions -F enabled=true -f allowed_actions=selected
   gh api --method PUT /repos/<org>/<repo>/actions/permissions/selected-actions --input <file listing the pinned actions of 83-infrastructure.md section 10.3>
   gh api --method PUT /repos/<org>/<repo>/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
   ```

6. Keep CodeQL on its workflow:

   ```bash
   gh api --method PATCH /repos/<org>/<repo>/code-scanning/default-setup -f state=not-configured
   ```

7. The GitHub environments come from the bootstrap's `github` step (`configure_github.py`). Read the two protected ones back:

   ```bash
   gh api /repos/<org>/<repo>/environments/prod --jq '{reviewers: [.protection_rules[] | select(.type == "required_reviewers") | .reviewers[].reviewer.slug], prevent_self_review: ([.protection_rules[] | select(.type == "required_reviewers") | .prevent_self_review] | first), can_admins_bypass}'
   gh api /repos/<org>/<repo>/environments/load-access --jq '{reviewers: [.protection_rules[] | select(.type == "required_reviewers") | .reviewers[].reviewer.slug], prevent_self_review: ([.protection_rules[] | select(.type == "required_reviewers") | .prevent_self_review] | first), can_admins_bypass}'
   ```

   Expected: `prod` returns `["docchat-release-approvers"]`, `true` and `false`; `load-access` returns `["docchat-tenant-admins"]`, `true` and `false`. A difference: the bootstrap operator runs this, which stops with `github_protection_mismatch` until both read back correctly:

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py --step github
   ```

8. Verify: a pull request with a failing test can't merge.

9. Keep `.github/security/approvers.yaml` equal to the `docchat-security` team now and after every team change:

   ```bash
   gh api /orgs/<org>/teams/docchat-security/members --jq ".[].login"
   ```

   Expected: the same handles as `approvers.yaml`; a difference is corrected by a pull request, which only `docchat-security` can approve.

## Verification

Steps 7 and 8, and step 9's comparison.

## Undo

The inverse API calls.

## Audit record

The organization's audit log.
