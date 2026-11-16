# ADR 0120: GitHub Actions OIDC and environments

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-10-30 (test jobs on standard GitHub-hosted runners by default, with the test identity's runner-rule right on the test vault; the runner group only under the private-networking option, ADR 0122)
- **Decided by:** Product owner and architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 81-config.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0016, 0019, 0095, 0114, 0115, 0116, 0118, 0122, 0124, 0126, 0127, 0136

## Context

The product owner requires delivery through Bicep, azd and GitHub Actions with OIDC, with managed identities and role assignments created by the templates. GitHub Actions therefore signs in to Azure only through federated credentials: one deploy identity per environment, a pull-request identity that can only read and validate templates, nothing stored as a secret, minimal workflow permissions and actions pinned to commit SHAs. Each deploy identity is a privileged operator of its environment (ADR 0115), so its GitHub environment is the first boundary around it: `main` only, and a second person's approval for `prod`. Deployed and load tests use a separate CI test identity, the only reader of the test passwords (ADR 0019).

The token exchange decides which identity a job becomes from the client ID the job presents, and Microsoft Entra ID accepts the exchange when that identity has a federated credential matching the job's issuer and subject. Two identities that trust the same subject would let any job with that subject choose the stronger one: a test job that installs test dependencies could sign in as a deploy identity. GitHub's environment protection is the other half of the boundary, and by default a repository administrator can bypass it, so the design must turn that off where a second person's approval is required.

Vendor facts (checked 2026-10-14 unless stated):

- Subjects: a job in an environment presents `repo:<org>/<repo>:environment:<Name>`, a pull-request job `repo:<owner>/<repo>:pull_request`, other jobs `repo:<org>/<repo>:ref:<ref path>`; issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`; branch and tag patterns aren't supported. A repository can customise its subject template (`GET /repos/{owner}/{repo}/actions/oidc/customization/sub`); an immutable-subject prefix can read `repo:{owner}@{owner_id}/{repo}@{repo_id}`.
- Federated credentials on a user-assigned identity: at most 20; issuer plus subject unique per identity; no wildcards; several created at once under one identity return `409`, so they are created one at a time; a new credential can take minutes to propagate (`AADSTS70021`). Contributor or Owner may add one, which is why policy denies them on the workload identities (ADR 0116). Flexible federated credentials (claims-matching expressions) are preview and apps-only.
- GitHub environments: "By default, administrators can bypass the protection rules and force deployments to specific environments"; an environment can disallow bypassing (checked 2026-10-15). Fork pull requests get no OIDC token.
- `azure/login` v3.1.0 signs in with `client-id`, `tenant-id` and `subscription-id` and needs `id-token: write`; `what-if` under Reader needs `--validation-level ProviderNoRbac`.

## Decision

1. **Five pipeline identities**, user-assigned, in `rg-docchat-identity`, where no pipeline identity holds any right, all created and granted by the bootstrap (ADR 0114):

   | Identity | Rights |
   |---|---|
   | `id-docchat-deploy-dev`, `id-docchat-deploy-prod`, `id-docchat-deploy-load` | the privileged-operator set of ADR 0115 for its own environment (Contributor and conditioned Role Based Access Control Administrator on `rg-docchat-<env>`, the registry and shared-group rights, Microsoft Graph `Application.ReadWrite.OwnedBy`, membership of `sg-docchat-<env>-sql-admins`) |
   | `id-docchat-pr` | Reader and DocChat Template Validator on `rg-docchat-dev` and `rg-docchat-shared`, DocChat Preflight Reader at the subscription; nothing else |
   | `id-docchat-citest` | Key Vault Secrets User on the shared test-credential vault and, while `ciPrivateNetworking` is `false` (the default), DocChat Test Vault Firewall Editor on that vault for its own transient runner rule (ADR 0122); Log Analytics Reader and DocChat Load Test Operator at `rg-docchat-load`; Log Analytics Reader on `log-docchat-dev` alone (the product owner's grant of 2026-11-06, for the deployed suites' log readings) and no other right in `rg-docchat-dev`; no right in `rg-docchat-prod` and no other network right |

2. **Federated credentials**, issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`, created one at a time (`@batchSize(1)`; a `409` is retried up to 6 times, 20 seconds apart):

   | Identity | Credential name | Subject |
   |---|---|---|
   | `id-docchat-deploy-dev` | `github-environment-dev` | `repo:<owner>/<repo>:environment:dev` |
   | `id-docchat-deploy-prod` | `github-environment-prod` | `repo:<owner>/<repo>:environment:prod` |
   | `id-docchat-deploy-load` | `github-environment-load` | `repo:<owner>/<repo>:environment:load` |
   | `id-docchat-pr` | `github-pull-request` | `repo:<owner>/<repo>:pull_request` |
   | `id-docchat-citest` | `github-environment-dev-tests` | `repo:<owner>/<repo>:environment:dev-tests` |
   | `id-docchat-citest` | `github-environment-load-tests` | `repo:<owner>/<repo>:environment:load-tests` |

3. **No shared subject.** The bootstrap's `identity` step reads the repository's subject customisation: with `use_default: true` it uses the subjects above; with a returned immutable prefix it substitutes that prefix for `repo:<owner>/<repo>`; any other customisation stops it with `oidc_subject_customised`. Before creating any credential it compares every issuer and subject pair across the five identities and stops with `oidc_subject_shared` (exit 2, nothing created) when two identities would trust the same pair. So every subject maps to exactly one identity.
4. **Six GitHub environments**, written by `scripts/bootstrap/configure_github.py` through `PUT /repos/{owner}/{repo}/environments/{name}`:

   | Environment | Deployment branches | Required reviewers | `prevent_self_review` | `can_admins_bypass` | Signs in as |
   |---|---|---|---|---|---|
   | `dev` | `main` only | none | – | – | `id-docchat-deploy-dev` |
   | `prod` | `main` only | team `docchat-release-approvers` (wait timer 0) | `true` | `false` | `id-docchat-deploy-prod` |
   | `load` | `main` only | none | – | – | `id-docchat-deploy-load` |
   | `load-access` | `main` only | team `docchat-tenant-admins` (members hold Cloud Application Administrator) | `true` | `false` | nobody (a gate with no variables) |
   | `dev-tests` | `main` only | none | – | – | `id-docchat-citest` |
   | `load-tests` | `main` only | none | – | – | `id-docchat-citest` |

   The script then reads each environment back (`GET /repos/{owner}/{repo}/environments/{name}`) and exits 2 with `github_protection_mismatch` unless `prod` and `load-access` show exactly their reviewers, `prevent_self_review: true` and `can_admins_bypass: false`.
5. **Variables, never secrets.** `dev`, `prod` and `load` hold `AZURE_CLIENT_ID` (that environment's deploy identity), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION`, `AZURE_RESOURCE_GROUP` (`rg-docchat-<env>`), `DOCCHAT_OPERATOR_IP_ALLOWLIST` and `DOCCHAT_OPS_ALERT_EMAILS`. `dev-tests` and `load-tests` hold `DOCCHAT_CI_TEST_CLIENT_ID`, `DOCCHAT_TEST_VAULT_URI`, `DOCCHAT_TEST_USER_DOMAIN`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`, and `load-tests` adds `AZURE_LOCATION` and `AZURE_RESOURCE_GROUP` (`rg-docchat-load`). Pull-request jobs read the repository variables `DOCCHAT_PR_CLIENT_ID`, `AZURE_TENANT_ID`, `DOCCHAT_DEV_SUBSCRIPTION_ID` and `DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST`; the repository variable `DOCCHAT_CI_PRIVATE_NETWORKING` (`true` or `false`) chooses the test jobs' runner (item 7). The repository holds no Actions secrets.
6. **One identity per job.** A job signs in to Azure only as the identity of its GitHub environment, through `azure/login` with OIDC and `azd auth login --client-id <id> --federated-credential-provider github --tenant-id <tenant>`, with the client ID from that environment's variable. Work that needs two identities is split into jobs that pass each other only non-secret job outputs (base URLs, client IDs, the API scope, resource names, run IDs, digest references, timestamps). The pull-request job `preflight-whatif` signs in as `id-docchat-pr` and runs only when `github.event.pull_request.head.repo.full_name == github.repository`; its `what-if` uses `--validation-level ProviderNoRbac`.
7. **Workflow rules.** Every workflow declares top-level `permissions: {}`; each job grants only what it uses (`id-token: write` only in jobs that sign in to Azure or attest a build; `contents: read`; `actions: read`, `attestations: write` or `read`, and `security-events: write` only where needed). No job holding `id-token: write` runs `npm ci`, `npm install`, `npm run`, `docker build` or `docker buildx build`, so install scripts, build plugins and Dockerfile steps never run where a token for an Azure identity can be minted. Every `uses:` is pinned to a full 40-character commit SHA with the tag as a trailing comment; `actions/checkout` runs with `persist-credentials: false`; no workflow uses `pull_request_target`; every job sets `timeout-minutes`; runners are `ubuntu-24.04`; every job in `dev-tests` or `load-tests` names `runs-on: ${{ vars.DOCCHAT_CI_PRIVATE_NETWORKING == 'true' && fromJSON('{"group":"docchat-ci-private"}') || 'ubuntu-24.04' }}`, which is `ubuntu-24.04` by default and the runner group `docchat-ci-private` under the private-networking option, and no other job names that group (ADR 0122).
8. **Repository settings** (runbook R16): the default workflow token is read-only; Actions are limited to the pinned actions and the repository's own reusable workflows; "Allow GitHub Actions to create and approve pull requests" is off.

## Alternatives considered

- **One identity for every environment, or a test identity that shares the deploy identity's subject.** Rejected: any job with that subject could choose the stronger identity by its client ID.
- **Branch subjects (`ref:refs/heads/main`) in place of environment subjects.** Rejected: every workflow on `main` could sign in, and the environments' reviewers and branch rules would never apply.
- **Client secrets or certificates in Actions secrets.** Rejected: OIDC needs no stored credential, and a stored one can be read by any workflow change.
- **GitHub's default protection, which administrators can bypass.** Rejected: a bypassable approval isn't a second person's approval of a `prod` release or a `load` run's registrations.
- **Flexible federated credentials matching several subjects.** Rejected: preview and available only on app registrations, not managed identities.
- **A pull-request identity with Contributor, so `what-if` runs full RBAC checks.** Rejected: pull-request code would then run with write rights on `dev`; Reader plus template validation with `ProviderNoRbac` checks the templates without them.
- **`pull_request_target` so fork pull requests get a token.** Rejected: it runs fork code with the base repository's permissions.

## Consequences

- A compromised dependency in a build job finds no Azure token; one in a test job reaches the test passwords, `rg-docchat-load` and, by default, the test vault's own settings, never a deploy identity; one in a `dev` job reaches `dev`, never `prod`.
- Every `prod` release and every `prod` operation through `deploy-environment.yaml` waits for a member of `docchat-release-approvers` other than the person who started it, and no administrator can skip that; an approval not given within 30 days expires and nothing reaches `prod`. Every `load` run waits at `load-access` for a tenant administrator.
- Fork pull requests get no `what-if`; a maintainer re-runs the change from a branch of the repository.
- Each identity holds at most 2 of its 20 federated credentials, so new environments fit. A changed subject template or a hand edit of a protected environment is caught when the bootstrap runs its `identity` or `github` step again; the deployed `dev` suite reads the environments back too.
- Suspected compromise (runbook R31): delete the deploy identity's federated credential, review `log-docchat-security`, then re-run the bootstrap's `identity` and `assignments` steps.
- Tests (84-testing.md): unit tests prove that the bootstrap stops with `oidc_subject_shared` for a plan in which two identities trust `environment:dev` and that `configure_github.py` stops with `github_protection_mismatch` when a read-back environment shows `can_admins_bypass: true`; CI checks prove that every `uses:` is SHA-pinned, every workflow has top-level `permissions: {}`, only jobs that sign in or attest hold `id-token: write`, no such job runs `npm` or a Docker build, every job that signs in names exactly one GitHub environment and one client-ID variable, and every job reading `DOCCHAT_CI_TEST_CLIENT_ID` runs in `dev-tests` or `load-tests` with the `runs-on` expression of item 7; deployed `dev` tests prove that `prod` and `load-access` read back with their reviewers, `prevent_self_review: true` and `can_admins_bypass: false`, and that `id-docchat-citest`'s credentials name only `dev-tests` and `load-tests`.

## References

- Federated identity credentials for GitHub Actions: https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust (checked 2026-10-14)
- Federated credentials on user-assigned identities and their limits: https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust-user-assigned-managed-identity (checked 2026-10-14)
- Federated credential considerations (propagation, `409`): https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations (checked 2026-10-14)
- Deployments and environments: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments (checked 2026-10-15)
- Azure login action: https://github.com/Azure/login/blob/master/README.md (checked 2026-10-14)
- Bicep what-if and validation levels: https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-what-if (checked 2026-10-14)
