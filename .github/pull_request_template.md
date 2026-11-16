## What and why

<!-- The change, and the decision record (ADR) or design section of docs/spec/ it implements.
     Name the T-ID of every stage-acceptance row whose subject this change touches. -->

## Tests

<!-- The test that failed before this change and passes after it, and the test levels that ran
     (unit, integration, contract, browser, infrastructure, deployed). -->

## Review checklist

- [ ] Comments explain why, not what.
- [ ] No logic is copied: shared code lives in `docchat_core`.
- [ ] No unused code, dependency or configuration is added.
- [ ] New settings and ops-config keys are documented in `81-config.md`.
- [ ] A migration follows the expand and contract rule of `80-data-model.md`.
- [ ] No content value, token or personal data is logged or put on a span.
