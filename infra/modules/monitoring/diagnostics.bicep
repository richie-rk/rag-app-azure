// Diagnostic settings of the resources the templates declare outside an AVM module
// (83-infrastructure.md section 2.19). Each goes to the environment's workspace in resource-
// specific tables, on the one preview API of the preview register (section 8.7).

param workspaceId string
param cosmosAccountName string
param dataStorageAccountName string

@description('storage.malwareScanning: the scan-results setting exists only while scanning is on.')
param malwareScanning bool

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2026-03-15' existing = {
  name: cosmosAccountName
}

resource dataStorageAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: dataStorageAccountName
}

resource defenderSettings 'Microsoft.Security/defenderForStorageSettings@2025-06-01' existing = {
  scope: dataStorageAccount
  name: 'current'
}

// The preview register's one entry (ADR 0009): only this version selects resource-specific tables.
#disable-next-line use-recent-api-versions
resource cosmosDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: cosmosAccount
  name: 'to-log-analytics'
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'DataPlaneRequests'
        enabled: true
      }
      {
        category: 'ControlPlaneRequests'
        enabled: true
      }
      {
        category: 'PartitionKeyRUConsumption'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Requests'
        enabled: true
      }
    ]
  }
}

#disable-next-line use-recent-api-versions
resource scanResultsDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (malwareScanning) {
  scope: defenderSettings
  name: 'scan-results'
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'ScanResults'
        enabled: true
      }
    ]
  }
}
