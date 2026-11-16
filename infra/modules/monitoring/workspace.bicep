// The environment's Log Analytics workspace and Application Insights component, both with local
// authentication disabled (83-infrastructure.md section 2.18; ADR 0107).

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param location string
param tags object
param config environmentConfig

var workspaceName = 'log-docchat-${environmentName}'
var componentName = 'appi-docchat-${environmentName}'

// prod keeps data-plane access logs a year for access reviews, beyond its 90 days of analytics.
var auditTables = [
  'CDBDataPlaneRequests'
  'SQLSecurityAuditEvents'
  'AZKVAuditLogs'
  'StorageBlobLogs'
  'AppServiceAuditLogs'
]

module workspace 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: workspaceName
  params: {
    name: workspaceName
    location: location
    tags: tags
    skuName: 'PerGB2018'
    dataRetention: config.monitoring.retentionDays
    // -1 means no cap: prod's audit records are never dropped, and a load run sends about 6.5 GB.
    dailyQuotaGb: string(config.monitoring.dailyCapGb)
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    tables: [
      for table in (environmentName == 'prod' ? auditTables : []): {
        name: table
        retentionInDays: 90
        totalRetentionInDays: 365
      }
    ]
    enableTelemetry: false
  }
}

module component 'br/public:avm/res/insights/component:0.8.0' = {
  name: componentName
  params: {
    name: componentName
    location: location
    tags: tags
    kind: 'web'
    applicationType: 'web'
    workspaceResourceId: workspace.outputs.resourceId
    // Entra-only ingestion: both apps export with APPLICATIONINSIGHTS_AUTHENTICATION_STRING.
    disableLocalAuth: true
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    enableTelemetry: false
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: componentName
  dependsOn: [component]
}

output workspaceId string = workspace.outputs.resourceId
output workspaceName string = workspaceName
output workspaceCustomerId string = workspace.outputs.logAnalyticsWorkspaceId
output appInsightsId string = appInsights.id
output appInsightsName string = componentName
output appInsightsConnectionString string = appInsights.properties.ConnectionString
