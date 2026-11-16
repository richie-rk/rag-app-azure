# Changelog

All notable changes to DocChat are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Changes are recorded under `## [Unreleased]` with that format's change types: Added, Changed, Deprecated, Removed, Fixed and Security. DocChat has no version numbers: the release identity is the commit SHA, and each production release moves the entries under `[Unreleased]` to a new heading named by its `deploy-prod-<yyyymmdd-hhmm>` Git tag and the release date, `## [deploy-prod-<yyyymmdd-hhmm>] - YYYY-MM-DD`.

## [Unreleased]

### Added

- The repository scaffold of build stage 0: the uv workspace, the GitHub workflows (`ci.yaml`, `codeql.yaml`, `nightly.yaml`, `deploy.yaml`, `deploy-environment.yaml`, `deployed-tests.yaml`), Dependabot, code owners, the main-branch ruleset, the security waiver and approver lists, pre-commit and secret-scanning configuration, and the repository documents.
