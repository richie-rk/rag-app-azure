// The only parameter file of main.bicep (83-infrastructure.md section 8.2; ADR 0096). It reads
// only what must stay out of the repository, from environment variables resolved at compile time,
// so azd, the pull-request what-if and PSRule all compile this same file. Every other value comes
// from infra/env/<env>.json, which main.bicep selects by environmentName.
using 'main.bicep'

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME')
param apiImage = readEnvironmentVariable('DOCCHAT_API_IMAGE_CURRENT')
param ingestionImage = readEnvironmentVariable('DOCCHAT_INGESTION_IMAGE_CURRENT')
param modelStubImage = readEnvironmentVariable('DOCCHAT_MODEL_STUB_IMAGE_CURRENT', '')
param operatorIpAllowlist = filter(
  split(readEnvironmentVariable('DOCCHAT_OPERATOR_IP_ALLOWLIST', ''), ','),
  address => !empty(address)
)
param resourceTokenSalt = readEnvironmentVariable('DOCCHAT_RESOURCE_TOKEN_SALT', '')
param releaseSha = readEnvironmentVariable('DOCCHAT_RELEASE_SHA', 'local')
param opsAlertEmails = filter(
  split(readEnvironmentVariable('DOCCHAT_OPS_ALERT_EMAILS', ''), ','),
  address => !empty(address)
)
// No default: a provision that skipped the preprovision hook fails here, at compile time.
param budgetStartDate = readEnvironmentVariable('DOCCHAT_BUDGET_START_DATE')
