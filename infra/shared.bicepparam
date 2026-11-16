// Parameters of shared.bicep, deployed only by the bootstrap's shared step (83-infrastructure.md
// section 8.5). The shared allowlist comes from the operator's shell, never from a GitHub variable.
using 'shared.bicep'

param sharedOperatorIpAllowlist = filter(
  split(readEnvironmentVariable('DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST', ''), ','),
  address => !empty(address)
)
param ciPrivateNetworking = readEnvironmentVariable('DOCCHAT_CI_PRIVATE_NETWORKING', 'false') == 'true'
param sharedRegistryLocation = 'eastus2'
param ciNetworkLocation = readEnvironmentVariable('DOCCHAT_CI_NETWORK_LOCATION', 'eastus2')
// The bootstrap's shared step passes the resource ID of log-docchat-security; the fallback lets
// the pull-request jobs build this file without it.
param securityWorkspaceId = readEnvironmentVariable(
  'DOCCHAT_SECURITY_WORKSPACE_ID',
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-docchat-identity/providers/Microsoft.OperationalInsights/workspaces/log-docchat-security'
)
