// The data storage account: containers, queues, the work-file lifecycle rule and Defender for
// Storage (83-infrastructure.md section 2.11; 80-data-model.md sections 4 and 5).

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig
param operatorIpAllowlist string[]
param workspaceId string

var accountName = 'stdocchatdata${environmentName}${resourceToken}'
var scanning = config.storage.malwareScanning

// The scanner's resource-instance rule is declared, so a redeploy never removes it and it stays
// effective while public access is disabled. Spike SP-3 compares it with what Defender writes.
var scannerAccessRules = scanning
  ? [
      {
        tenantId: config.tenantId
        resourceId: subscriptionResourceId('Microsoft.Security/datascanners', 'StorageDataScanner')
      }
    ]
  : []

var serviceDiagnostics = [
  {
    name: 'to-log-analytics'
    workspaceResourceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logCategoriesAndGroups: [
      { category: 'StorageRead' }
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
    tags: tags
    kind: 'StorageV2'
    // Derived from the environment name, never a parameter (ADR 0102).
    skuName: environmentName == 'dev' ? 'Standard_LRS' : 'Standard_ZRS'
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    allowedCopyScope: 'AAD'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // Blob index tags, which carry the scan results, need a flat namespace.
    enableHierarchicalNamespace: false
    enableSftp: false
    isLocalUserEnabled: false
    enableNfsV3: false
    requireInfrastructureEncryption: true
    dnsEndpointType: 'Standard'
    publicNetworkAccess: empty(operatorIpAllowlist) ? 'Disabled' : 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [
        for address in operatorIpAllowlist: {
          value: address
          action: 'Allow'
        }
      ]
      virtualNetworkRules: []
      resourceAccessRules: scannerAccessRules
    }
    blobServices: {
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: 14
      deleteRetentionPolicyAllowPermanentDelete: false
      containerDeleteRetentionPolicyEnabled: true
      containerDeleteRetentionPolicyDays: 30
      isVersioningEnabled: false
      changeFeedEnabled: false
      restorePolicyEnabled: false
      containers: [
        {
          name: 'documents'
          publicAccess: 'None'
        }
        {
          name: 'renditions'
          publicAccess: 'None'
        }
        {
          name: 'ingestion-work'
          publicAccess: 'None'
        }
      ]
      diagnosticSettings: serviceDiagnostics
    }
    // The poison queue exists in advance, so the Functions runtime needs only Message Sender on it.
    queueServices: {
      queues: [
        { name: 'ingestion-requests' }
        { name: 'ingestion-requests-poison' }
      ]
      diagnosticSettings: serviceDiagnostics
    }
    managementPolicyRules: [
      {
        name: 'ingestion-work-expiry'
        enabled: true
        type: 'Lifecycle'
        definition: {
          filters: {
            blobTypes: ['blockBlob']
            prefixMatch: ['ingestion-work/']
          }
          actions: {
            baseBlob: {
              delete: {
                daysAfterModificationGreaterThan: 7
              }
            }
          }
        }
      }
    ]
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

// Always declared, so turning scanning off is a parameter change, not a resource deletion.
resource defenderSettings 'Microsoft.Security/defenderForStorageSettings@2025-06-01' = {
  scope: storageAccount
  name: 'current'
  properties: {
    isEnabled: scanning
    malwareScanning: {
      onUpload: {
        isEnabled: scanning
        capGBPerMonth: config.storage.malwareScanCapGbPerMonth
        // Renditions, work files and converted PDFs are written by the trusted ingestion app
        // from a scanned source, and no code reads their scan tags.
        filters: {
          excludeBlobsWithPrefix: ['renditions/', 'ingestion-work/']
          excludeBlobsWithSuffix: ['/converted.pdf']
        }
      }
      blobScanResultsOptions: 'blobIndexTags'
    }
    sensitiveDataDiscovery: {
      isEnabled: false
    }
    overrideSubscriptionLevelSettings: true
  }
}

output id string = storageAccount.id
output name string = accountName
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output queueEndpoint string = storageAccount.properties.primaryEndpoints.queue
