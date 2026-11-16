// The environment Key Vault (83-infrastructure.md section 2.15). It stays empty in the first
// version: no app reads a secret, so no app identity holds a Key Vault role.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig
param operatorIpAllowlist string[]
param workspaceId string

var vaultName = 'kv-docchat-${environmentName}-${resourceToken}'

module vault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: vaultName
  params: {
    name: vaultName
    location: location
    tags: tags
    // The module's default is premium.
    sku: 'standard'
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: config.keyVault.softDeleteDays
    // The module's default is true; purge protection can never be turned off once on.
    enablePurgeProtection: config.keyVault.purgeProtection
    enableVaultForDeployment: false
    enableVaultForTemplateDeployment: false
    enableVaultForDiskEncryption: false
    publicNetworkAccess: empty(operatorIpAllowlist) ? 'Disabled' : 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [for address in operatorIpAllowlist: { value: address }]
    }
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'AuditEvent' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

output id string = vault.outputs.resourceId
output name string = vaultName
output uri string = vault.outputs.uri
