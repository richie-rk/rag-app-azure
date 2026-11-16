// The Document Intelligence account, whose layout model (API 2024-11-30) serves the recursive
// ingestion method (83-infrastructure.md section 2.14). Documents are sent as bytes, never as URLs,
// so no feature in use calls out.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object

@description('Operator allowlist; empty disables public network access.')
param operatorIpAllowlist string[]

param workspaceId string

var accountName = 'di-docchat-${environmentName}-${resourceToken}'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: accountName
  params: {
    name: accountName
    location: location
    tags: tags
    kind: 'FormRecognizer'
    sku: 'S0'
    // Microsoft Entra authentication needs a custom subdomain.
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: empty(operatorIpAllowlist) ? 'Disabled' : 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [for address in operatorIpAllowlist: { value: address }]
    }
    restrictOutboundNetworkAccess: true
    allowedFqdnList: []
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'Audit' }
          { category: 'RequestResponse' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

output id string = account.outputs.resourceId
output name string = accountName
output endpoint string = 'https://${accountName}.cognitiveservices.azure.com'
