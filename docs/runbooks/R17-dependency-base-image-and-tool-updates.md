# R17 — Dependency, base-image and tool updates

**Performer:** a maintainer.

**Trigger:** weekly, monthly and quarterly as listed; a high or critical finding.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; for steps 3 to 5, a synced checkout (`uv sync --locked --all-packages --all-groups`) on a branch for the pull request.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Weekly: review Dependabot pull requests and merge when CI passes; a major version, `openai`, an `azure-*` package, MSAL or Fluent UI also needs the full browser suite and a `dev` `smoke` run before it reaches `prod`.

2. The Functions host base image (Dependabot on `apps/ingestion/Dockerfile`) merges only after the ingestion integration suite and the image's offline self-check pass.

3. Parsing model pins change with this command, which records the new SHA-256 values; CI builds the image and runs the offline self-check; a new method version or new parsing models then reach indexes through R05 (`14-ingestion.md` section 19.3):

   ```bash
   python apps/ingestion/image/fetch_models.py --update <repository> <revision>
   ```

   A pull request that changes the pinned `pikepdf`, or the qpdf it bundles, also raises `RENDITION_RULES_VERSION` by 1 and adds its entry to the rendition rules version unit test (`14-ingestion.md` section 14.8 item 6); without both, CI fails, and after the deploy sweep duty 12 re-renders every served version under the new library.

4. Monthly, in a pull request:

   ```bash
   pre-commit autoupdate --freeze
   ```

5. Quarterly: read the latest k6 release's `k6-v<version>-checksums.txt` and update `tests/load/k6.lock.json` (version, URL, SHA-256 and member path); then:

   ```bash
   uv run --frozen python scripts/ci/check_k6_lock.py
   ```

   Expected: it passes. The load generator's image needs no update of its own: `docchat-load aci-params` reads it from the ingestion Dockerfile, which step 2 keeps current.

6. Quarterly: compare every `br/public:avm/res/<path>:<version>` pin in `infra/` with the Azure Verified Modules index, read each newer version's changelog for API-version and default changes, and move the pins in one pull request that passes the infrastructure gate (`bicep build`, `bicep build-params` for the three environments, lint, PSRule, `check_bicep_modules.py` and the what-if against `dev`). A version that deploys a preview API version isn't adopted; one that moves diagnostic settings to a GA API version is adopted at once.

7. Quarterly, and as soon as a stable PSRule for Azure release ships: move `ps-rule.yaml`'s `requires` range for `PSRule.Rules.Azure` and its baseline together to the new release and its newest GA baseline in one pull request that passes the gate, resolving each newly failing built-in rule as `83-infrastructure.md` section 13.4 says.

8. Quarterly: compare the tool versions of `83-infrastructure.md` section 9.4 and `86-operations.md` section 1.5 with their release pages and move them by pull request.

9. A new Python minor version is adopted only through an ADR, once both Azure Functions and `unstructured` support it.

10. A high or critical finding with no patched version fails every build until it is gone. Remove it in this order: merge the base-image update that drops it; remove the package from the image's final stage when the app doesn't need it at run time; move to another pinned base image by pull request; when none of these works, the product owner decides through a new ADR, because no waiver exists for high or critical findings. An npm finding whose package has a patched release that the range of the dependency bringing it excludes isn't such a case: force the patched release with an exact `overrides` entry (`01-repository.md` section 8.1) in a pull request whose CI passes, and remove the entry as `01-repository.md` section 8.3 item 9 says.

    The one such decision is ADR 0153's exception for Trivy's DS002 on the ingestion image. Before its `review_by` date in `.github/security/exceptions.yaml` (the security gate warns 14 days ahead in the job summary and the nightly run, and fails every run after the date), a `docchat-security` member confirms that the ingestion host still needs UID 0 for the parsing child's user switch (`14-ingestion.md` section 15.6), then opens a pull request that sets `added` to that day and `review_by` to at most 184 days later, or removes the entry once the image no longer raises DS002. Check it locally first:

    ```bash
    uv run --frozen python scripts/ci/security_gate.py --waivers-only
    ```

    Expected: exit 0, and the exception's row reads `valid, matches no finding`.

## Verification

Each pull request's CI run passes, and steps 1 and 2 name the extra suites a change needs before it reaches `prod`.

## Undo

A revert pull request.

## Audit record

The pull requests and their CI runs.
