// The Functions host storage account: the hardening of the data account, public access always
// disabled, and nothing declared inside it, because the Functions host and the Durable task hub
// create their own containers, queues and tables (83-infrastructure.md section 2.12).

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param workspaceId string

var accountName = 'stdocchathost${environmentName}${resourceToken}'

// StorageRead is left out on purpose: Durable Functions polls its queues and tables continuously,
// and those reads carry no audit value (ADR 0107).
var serviceDiagnostics = [
  {
    name: 'to-log-analytics'
    workspaceResourceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logCategoriesAndGroups: [
      { category: 'StorageWrite' }
      { category: 'StorageDelete' }
    ]
    metricCategories: [
      { category: 'Transaction' }
    ]
  }
]

module account 'br/public:avm/res/storage/storage-account:0.33.1' = {
  name: accountName
  params: {
    name: accountName
    location: location
    tags: union(tags, { 'resource-usage': 'azure-functions' })
    kind: 'StorageV2'
    skuName: environmentName == 'dev' ? 'Standard_LRS' : 'Standard_ZRS'
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    allowedCopyScope: 'AAD'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    enableHierarchicalNamespace: false
    enableSftp: false
    isLocalUserEnabled: false
    enableNfsV3: false
    requireInfrastructureEncryption: true
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
    blobServices: {
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: 7
      deleteRetentionPolicyAllowPermanentDelete: false
      containerDeleteRetentionPolicyEnabled: true
      containerDeleteRetentionPolicyDays: 7
      diagnosticSettings: serviceDiagnostics
    }
    queueServices: {
      diagnosticSettings: serviceDiagnostics
    }
    tableServices: {
      diagnosticSettings: serviceDiagnostics
    }
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        metricCategories: [
          { category: 'Transaction' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: accountName
  dependsOn: [account]
}

output id string = storageAccount.id
output name string = accountName
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output queueEndpoint string = storageAccount.properties.primaryEndpoints.queue
output tableEndpoint string = storageAccount.properties.primaryEndpoints.table
