# ADR 0153: One named exception for the ingestion image's root user

- **Status:** Accepted
- **Date:** 2026-11-06; amended 2026-11-07 (no longer the only exception at high severity: ADR 0154 adds one for HIGH or CRITICAL OS-package findings without a fix, so the gate holds two kinds and this one's scope is unchanged)
- **Decided by:** Product owner, 2026-11-06
- **Specified in:** 84-testing.md, 82-security.md, 01-repository.md, 83-infrastructure.md, 86-operations.md, 90-build-order.md, CONTEXT.md
- **Related ADRs:** 0010, 0011, 0049, 0136, 0141, 0154

## Context

- ADR 0136 makes every high or critical scanner finding fail the build, with no waiver, and leaves a finding that nothing in the repository can remove to the product owner through a new ADR (its item 9; runbook R17 step 10; stop SC-7 of `90-build-order.md`).
- The ingestion image keeps the Functions base image's UID 0 by design: `setpriv` changes user only from root, and the parsing child switches to one of the 16 parse users `docchat-parse-0` to `docchat-parse-15` (UIDs 10002 to 10017) under the system-call filter before it reads any document byte (ADR 0049; `14-ingestion.md` section 15.6; `01-repository.md` section 4.6). The Functions host and worker never parse document bytes, and `docchat_ingestion.function_app` refuses to load when its effective UID isn't 0.
- Trivy's Dockerfile check DS002 ("Image user should not be 'root'") is HIGH and fails on any Dockerfile whose last `USER` is root or that sets none. `trivy config` on `apps/ingestion/Dockerfile` reports it in `trivy-config.json` on every pull request; an image scan with image-configuration checks turned on would report it in `trivy-ingestion.json`. Neither a base-image update, a package removal nor a dependency change clears it, so under ADR 0136 alone the `images` job could never pass.

## Decision

1. **One exception, named in code.** The security gate passes exactly one high finding: Trivy's misconfiguration DS002, rated HIGH, on the ingestion image, either in that image's report `trivy-ingestion.json` or in `trivy-config.json` on the target `apps/ingestion/Dockerfile`. `scripts/ci/security_gate.py` holds the only allowed pair for this check, `(DS002, ingestion)`, in a constant (since 2026-11-07 beside ADR 0154's `unfixed-os-packages` kind, which covers no misconfiguration); an entry for any other check, image or report is invalid and fails the gate, and the exception never covers a vulnerability, a secret, a CRITICAL rating, another check of the ingestion image or DS002 on the API or model-stub image. Both the gate and the file sit under `docchat-security` code ownership, so widening the exception takes a reviewed change to the gate, the spec and a new ADR.
2. **Where it is written.** `.github/security/exceptions.yaml`, beside the waivers and read by the same YAML subset: `exceptions:` and a list of entries with exactly `id`, `image`, `reason` (at least 20 characters), `decision` (this record, `ADR 0153`), `added` and `review_by` (dates `YYYY-MM-DD`). A missing file, a file outside the subset or an entry with a missing, unknown, repeated or malformed field fails the gate.
3. **A finite review date.** `added` isn't after the day the gate runs and `review_by` is at most 184 days after `added` (six calendar months). From the day after `review_by` the entry is invalid and every gate run fails, the nightly `--waivers-only` run included, until `docchat-security` reviews the reason and renews the entry with new dates or removes it. The gate warns 14 days before `review_by` in the job summary and the nightly log, as it does for waivers. The first entry is dated 2026-11-06 with `review_by` 2027-05-06.
4. **What stays.** Every other high or critical finding fails the build whatever any file holds, but for the unfixed OS-package findings that ADR 0154's exception passes since 2026-11-07; waivers still exist only below high, and none names a Trivy finding. The image's other proofs are unchanged: the parse users, their lock files, the system-call filter and the readable-files check are built and smoke-tested in the `images` job (`01-repository.md` section 4.6).

## Alternatives considered

- **A waiver at high in `waivers.yaml`.** Rejected: waivers are defined below high only, name no Trivy finding, scope by package or path glob without the report, and last at most 90 days; admitting this finding there would special-case four of their rules and weaken the invariant that a waiver naming a high finding changes nothing.
- **Trivy's own ignore file (`.trivyignore.yaml`) or `--skip-check DS002`.** Rejected for ADR 0136's reason: no approver, no expiry, and it would hide DS002 on the API and model-stub images too.
- **A `USER` instruction naming a non-root user in the ingestion Dockerfile.** Rejected: the parsing child's user switch needs the host to run as root (ADR 0049 item 10), so the image would no longer work.
- **Leaving the `images` job failing.** Rejected: every pull request would be blocked for a finding the design requires.

## Consequences

- Every run of the gate (the `images` job, and the deploy workflow's `build` job and the nightly `trivy-rescan` should their image scans report it) passes DS002 on the ingestion image until the review date, and fails on it again after that date until the exception is renewed.
- The accepted risk is ADR 0049's: a flaw in the Functions host or worker runs as root inside the container; a parsing flaw runs as a parse user under the system-call filter.
- Obligations: `docchat-security` reviews the exception before each review date (runbook R17); the release's final security pass checks it hasn't passed its review date.
- Tests (84-testing.md, T-527): the exception passes DS002 in `trivy-ingestion.json` and on `apps/ingestion/Dockerfile` in `trivy-config.json`; DS002 in `trivy-api.json`, `trivy-model-stub.json` or on another Dockerfile still fails; another HIGH finding of the ingestion image still fails; an exception past its review date fails; a malformed entry or file is refused.

## References

- Trivy, container image misconfiguration scanning (image-configuration checks off by default, `--image-config-scanners misconfig`) and Dockerfile check DS002: https://trivy.dev/latest/docs/target/container_image/ (checked 2026-11-06)
