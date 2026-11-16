# ADR 0127: Release and rollback

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 86-operations.md, 84-testing.md
- **Related ADRs:** 0027, 0120, 0123, 0124, 0136, 0142

## Context

Every change reaches the environments through one pipeline: a merge to `main` deploys `dev`, and `prod` deploys the same image digests after a manual approval. Any earlier release must be redeployable, and the deploy and rollback procedures are runbooks. Every migration keeps the running release working (expand, deploy, then contract in a later release, ADR 0027), which makes a rollback without a schema change possible. Two hazards remain. Approvals can be given in any order, so approving an old queued run after a newer release would quietly roll `prod` back. And a rollback must never run a downgrade migration, because a downgrade can drop data that the newer release wrote.

Vendor facts (checked 2026-10-14 unless stated):

- A concurrency group allows one running job; `queue: max` keeps up to 100 pending runs in order and can't be combined with `cancel-in-progress: true`.
- GitHub environments with required reviewers hold a job until a reviewer approves; `prevent_self_review` stops the person who started the run from approving it, and an environment can disallow administrator bypass (checked 2026-10-15).
- "Creating a tag object does not create the reference that makes a tag in Git": an annotated tag is `POST /repos/{owner}/{repo}/git/tags` (`tag`, `message`, `object`, `type`) followed by `POST /repos/{owner}/{repo}/git/refs` (`ref`, `sha`) (checked 2026-10-21).

## Decision

1. **One delivery workflow.** `.github/workflows/deploy.yaml` runs on `push` to `main` and on `workflow_dispatch` with inputs `environment` (`dev` | `prod`), `image_commit` (40 hexadecimal characters) and `rollback` (boolean, default `false`). Its jobs, in order: `build` and `publish` (push only, ADR 0124), `resolve` and `resolve-web` (dispatch only), `deploy-dev`, `test-dev`, `deploy-prod`, `test-prod`, `tag-prod`. A failed job stops every later job. Each deploy job calls `deploy-environment.yaml` (one job `deploy`, concurrency `docchat-env-<env>` with `cancel-in-progress: false` and `queue: max`).
2. **A release on every push to `main`.** `deploy-dev` runs `mode: full`: preflight, `azd provision` (its `postprovision` hook runs migrations and grants, ADR 0027), `azd deploy api`, `azd deploy ingestion` and the web client, by digest; `test-dev` runs the deployed `smoke` suite; `deploy-prod` waits for an approval by a member of `docchat-release-approvers` other than the person who started the run, which no administrator can bypass (ADR 0120), then deploys the same digests and the same `web-dist` artifact in `mode: full`; `test-prod` runs `prod_smoke` (anonymous checks only); `tag-prod` (no GitHub environment, `permissions {contents: write}`, no Azure sign-in) creates the annotated tag `deploy-prod-<yyyymmdd-hhmm>` (UTC) on the deployed commit through the REST API: `gh api repos/{owner}/{repo}/git/tags -f tag=deploy-prod-<yyyymmdd-hhmm> -f message="prod deploy of <sha> by run <run ID>" -f object=<sha> -f type=commit`, then `gh api repos/{owner}/{repo}/git/refs -f ref=refs/tags/deploy-prod-<yyyymmdd-hhmm> -f sha=<SHA of the tag object>`.
3. **Release ordering.** In `prod`, unless `rollback` is `true`, the deploy job reads the API site's tag `docchat-release-sha` before provisioning; when the run's commit is an ancestor of that commit (`git merge-base --is-ancestor <run commit> <deployed commit>`), it writes "a newer release is already in prod" to the job summary and ends successfully without deploying. A queued `prod` run is never cancelled (`queue: max`), and approving an old one can't move `prod` backwards.
4. **Deploying a chosen commit.** A dispatch with `environment` and `image_commit` runs `resolve` (the commit is on `main`, its three tags exist, and each digest's attestation is valid for `deploy.yaml` on `main` at that commit; otherwise `commit_not_on_main`, `image_tag_missing` or `image_attestation_invalid`) and `resolve-web` (the web client built at that commit with no credential), then `deploy-dev` or `deploy-prod`, its tests, and `tag-prod` for `prod`.
5. **Rollback is a dispatch, never a downgrade.** A rollback is that dispatch with the earlier release's `image_commit` and `rollback: true`: `deploy-environment.yaml` runs `mode: images-only`, which skips `azd provision` and so runs no migration, then deploys the earlier digests and web build; `prod` still needs its approval, and only its release-ordering check is skipped. Every migration's `downgrade()` raises `NotImplementedError("roll forward")`, so the schema is never downgraded: under the expand-then-contract rule the current schema serves the earlier release.
6. **Where a rollback stops.** Runbook R02 rolls back only across releases whose migrations are all expand revisions: `git log --oneline <good commit>..<current commit> -- db/migrations/versions` must show no revision whose docstring starts with `Contract:`, because a contract revision removes what earlier code may still read; when one exists, the fix goes forward.
7. **What a rollback can reach.** Any commit whose images the registry still holds. No image is deleted automatically; R28 deletes, at most once a quarter, only images older than 400 days that no `deploy-prod-*` tag references, so every release that ever reached `prod` stays deployable.
8. **Who decides.** A failed `prod_smoke` fails the workflow and notifies the approvers; the approver decides within 30 minutes between fixing forward and rolling back (R02). After every `prod` release the approver watches the availability and time-to-first-token workbooks for 30 minutes (R01).

## Alternatives considered

- **Downgrade migrations.** Rejected: a downgrade can drop columns and rows the newer release wrote; the expand-then-contract rule makes them unnecessary.
- **Re-running an earlier `deploy.yaml` run.** Rejected: it would build and push that commit's images again under new digests, and its provision step would run that commit's migrations against a database at a newer revision.
- **Automatic rollback when `prod_smoke` fails.** Rejected: the failure may lie outside the release (an Entra or platform incident), and a rollback across a contract revision would break `prod`; a person decides within 30 minutes.
- **Deployment slots with a swap.** Rejected: azd deploys the function app only to its main site, so the two apps would release differently; deploy-by-digest with the release-ordering check and a quick `images-only` rollback covers the need with one mechanism.
- **A separate `prod` workflow triggered by tags.** Rejected: one run carries the digests that `dev` tested to `prod` and records the whole release in one place.

## Consequences

- Every `prod` release is either the same run's `dev`-tested digests or a dispatched commit on `main` whose images `main`'s pipeline built and attested.
- An approval that nobody gives expires after 30 days and nothing reaches `prod`; an earlier run approved after a newer release ends without deploying.
- The `deploy-prod-*` tags are `prod`'s release history: R02 lists good releases from them, R28 keeps their images, and the nightly re-scan reads the newest one.
- A rollback takes the time of `resolve`, `resolve-web` and an `images-only` deploy, plus the `prod` approval; no data changes.
- An `images-only` rollback runs the earlier code against the current release's app settings and `ops_config` rows. Ops-config rows are safe: an area ignores keys it doesn't know, and only a contract revision deletes one. App settings have no such guard: earlier code stops at start-up on a missing setting or a value outside its range, which the deploy's `health_check_failed` or `ingestion_start_failed` reports within 10 minutes. Code review therefore treats removing or renaming an app setting, or setting one outside the range the previous release accepts, like a contract change: it ships in a later release than the code that stops needing it.
- Obligations: R01 and R02, rehearsed in `dev` and recorded (ADR 0142).
- Tests (84-testing.md): a deployed `dev` test proves that a dispatch for a commit whose tag was re-pointed to an unattested image fails in `resolve` with `image_attestation_invalid` and deploys nothing; a unit test proves that `check_migrations.py` fails on a destructive operation outside a `Contract:` revision and when the named expand revision isn't in the commit `prod` last deployed; the rollback rehearsal proves that an `images-only` dispatch restores the earlier commit's images in `dev` with no migration run.

## References

- Workflow concurrency: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency (checked 2026-10-14)
- Deployments and environments: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments (checked 2026-10-15)
- Git tags and references REST API: https://docs.github.com/en/rest/git/tags and https://docs.github.com/en/rest/git/refs (checked 2026-10-21)
