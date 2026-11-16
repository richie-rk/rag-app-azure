# ADR 0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-10-18; amended 2026-10-30 (the GitHub-plan stop of ADR 0122 withdrawn)
- **Decided by:** Architect
- **Specified in:** 00-overview.md, 01-repository.md, 90-build-order.md
- **Related ADRs:** 0004, 0009, 0011, 0103, 0122, 0136, 0141

## Context

DocChat is built from a complete written design by people who weren't present when its decisions were taken. Each significant choice (one that shapes the structure, a security control or a quality attribute, or that is costly to reverse) needs its reason, the options it beat and its consequences kept beside the code it explains. Each choice also has to say whose it is: the product owner sets the product's scope and its non-negotiable rules, and only the product owner reopens them; the architect makes the technical choices that implement them, and a later decision may supersede those. A security reviewer must be able to see why every choice was made.

Some decisions rest on platform behaviour that vendor documentation doesn't settle, so a test during the build finishes them: the stage-0 spikes and the CORS preflight test of ADR 0004. The record has to say how such a result is written down, and when a result needs a new decision.

Vendor guidance (Microsoft Azure Well-Architected Framework): the decision record is an append-only log; accepted records aren't edited; a changed decision gets a new record that supersedes the earlier one and links to it; each record carries a status (Proposed, Accepted or Superseded) and, the guidance suggests, a confidence level; only choices that affect the structure or key quality attributes, or are hard to reverse, belong in it.

## Decision

1. **One file per decision.** Architecture decision records live in `docs/adr/` as `NNNN-<slug>.md`: a four-digit, zero-padded number and a lowercase kebab-case slug taken from the title. The first line is `# ADR NNNN: <title in sentence case>`.
2. **Template.** A header list with Status, Date, Decided by, Specified in (the design documents that implement the decision, from `00-overview.md` to `90-build-order.md`) and Related ADRs; then the sections Context, Decision, Alternatives considered, Consequences and References. The Decision section carries every name, value and unit a builder needs. Every reason is stated as a need of this product, and nothing in an ADR is specific to one organisation or industry: anything domain-specific is configuration or data.
3. **Attribution.** `Decided by` is `Product owner`, `Architect` or `Product owner and architect`. A product-owner decision changes only with the product owner's approval.
4. **Statuses.** `Accepted` or `Superseded by NNNN`. No ADR in the repository is `Proposed`: the pull request that adds an ADR is the proposal, and merging it accepts it.
5. **Append-only record.** An accepted ADR changes in two ways only. (a) When a later ADR replaces it, its Status line becomes `Superseded by NNNN`, and the new ADR lists it under Related ADRs. (b) When a test that the ADR itself names reports, one line `Outcome (YYYY-MM-DD): <measured result>; <the branch of the ADR's rule that applies>` is added at the end of its Consequences. An outcome that needs a decision the ADR doesn't make gets a new ADR: the second content-share fallback of ADR 0103 and the product owner's choice when the preflight test of ADR 0004 fails. (The stop in ADR 0122 when the GitHub plan offers no larger runners was withdrawn on 2026-10-30: the build no longer needs a paid GitHub plan.) Where a decision rests on unconfirmed platform behaviour, its ADR names the test that settles it and the rule that turns the result into a decision.
6. **Numbering.** Numbers are never reused or renumbered. ADRs are grouped by area; a new ADR takes the next free number, starting at 0153, and stands at the end of its group:

| Group | ADRs |
|---|---|
| Foundations and product scope | 0001 to 0015 |
| Identity and access | 0016 to 0024, 0143 |
| Data | 0025 to 0033 |
| Ingestion | 0034 to 0054, 0144 to 0146, 0152 |
| Chat | 0055 to 0070, 0147, 0148 |
| Conversations and memory | 0071 to 0081, 0149 |
| Web | 0082 to 0094 |
| Infrastructure and networking | 0095 to 0112 |
| Identities and delivery | 0113 to 0127 |
| Quality and operations | 0128 to 0142, 0150, 0151 |

7. **When an ADR is written.** For every significant choice as defined above; every dependency on a preview feature, with its GA alternative (ADR 0009); every change to the repository layout and every new top-level subpackage in `docchat_core`, `docchat_api` or `docchat_ingestion` (ADR 0011); and every test outcome that needs a new decision (item 5).
8. **References.** Official vendor documentation (Microsoft Learn; the Azure SDK, REST and Bicep references; Azure Verified Modules; vendor-owned repositories and release notes; the PyPI or npm pages of the libraries used) and the web standards and browser documentation the design relies on (W3C, WHATWG, MDN). Each URL carries the date it was checked.
9. **Review.** `docs/adr/` falls under the CODEOWNERS default `*` → `docchat-maintainers`, so an ADR merges only with one code-owner approval under the `main` ruleset. A pull request that supersedes an ADR decided by the product owner states the product owner's approval in its description.

## Alternatives considered

- **Decisions only inside the design documents.** Lost: reasons and rejected options get scattered through requirement text, with no attribution and no history when a decision changes.
- **A wiki or an issue tracker.** Lost: it lives outside the repository, isn't versioned with the code it explains and isn't reviewed in the pull request that changes that code.
- **One log file for every decision.** Lost: one decision per file lets a pull request touch exactly the decisions it changes and lets a superseding ADR point at one file.
- **A heavier template (deciders, consulted and informed lists, a confidence field, a `Proposed` status in the repository).** Lost: `Decided by` carries the attribution that matters here and pull requests carry the proposal stage. In place of a confidence level, a decision that rests on uncertain platform behaviour names the test and the decision rule (item 5), which tells a reader what would change it.
- **Editing accepted ADRs in place.** Lost: it erases why a direction changed; the Well-Architected guidance keeps the record append-only and supersedes instead.

## Consequences

- A reviewer can trace every choice to its owner and its reason, and a builder never has to make an architectural decision without a record.
- Every pull request that changes a decision adds or supersedes an ADR in the same change as the code it affects.
- Obligations: build stage 0 places this set in `docs/adr/` (90-build-order.md); whoever runs a named test adds its Outcome line; the build stops for a new ADR at each stop point listed in item 5; new ADRs start at 0152.
- Risk: an ADR and the design documents drift apart. Mitigation: each ADR names the documents that implement it (Specified in), and a change to either side updates the other in the same pull request.

## References

- Microsoft Azure Well-Architected Framework, "Maintain an architecture decision record (ADR)": https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record (checked 2026-10-18)
