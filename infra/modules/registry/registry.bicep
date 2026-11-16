// The shared container registry on the GA API 2025-11-01 (83-infrastructure.md section 2.16;
// ADRs 0097 and 0118). The AVM module isn't used because it deploys a preview API and defaults the
// network bypass to AzureServices.

param location string
param tags object
param sharedToken string

@description('The shared operator allowlist: the registry\'s only standing IP rules.')
param sharedOperatorIpAllowlist string[]

param securityWorkspaceId string

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: 'crdocchat${sharedToken}'
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    // Public access stays on for the dev publish jobs' transient runner rule (section 4.6), and
    // each deployment writes the rules from the shared allowlist alone.
    publicNetworkAccess: 'Enabled'
    networkRuleSet: {
      defaultAction: 'Deny'
      ipRules: [
        for address in sharedOperatorIpAllowlist: {
          action: 'Allow'
          value: address
        }
      ]
    }
    networkRuleBypassOptions: 'None'
    networkRuleBypassAllowedForTasks: false
    // The regional data endpoints resolve through the same private zone.
    dataEndpointEnabled: true
    zoneRedundancy: 'Enabled'
    // AcrPull and AcrPush stay honoured, so the conditioned delegation of AcrPull keeps working
    // when the ABAC repository-permission mode becomes the platform default.
    roleAssignmentMode: 'LegacyRegistryPermissions'
    policies: {
      // App Service managed-identity pulls need ARM-audience tokens.
      azureADAuthenticationAsArmPolicy: {
        status: 'enabled'
      }
      // Exports can't be disabled while public access stays on.
      exportPolicy: {
        status: 'enabled'
      }
      quarantinePolicy: {
        status: 'disabled'
      }
      trustPolicy: {
        status: 'disabled'
      }
    }
  }
}

// The preview register's one entry (ADR 0009): only this version selects resource-specific tables.
#disable-next-line use-recent-api-versions
resource registryAudit 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: registry
  name: 'registry-audit'
  properties: {
    workspaceId: securityWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'ContainerRegistryRepositoryEvents'
        enabled: true
      }
      {
        category: 'ContainerRegistryLoginEvents'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output id string = registry.id
output name string = registry.name
output loginServer string = registry.properties.loginServer
