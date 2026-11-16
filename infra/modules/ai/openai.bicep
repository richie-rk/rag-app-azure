// The Azure OpenAI account, declared only where openAi.enabled is true (dev and prod; load calls
// the model stub) (83-infrastructure.md section 5.1). The module's deployments parameter stays
// empty: the content filter policies and the deployments are declared on their GA APIs by
// openai-deployments.bicep, one deployment at a time.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object

@description('Operator allowlist; empty disables public network access.')
param operatorIpAllowlist string[]

param workspaceId string

var accountName = 'oai-docchat-${environmentName}-${resourceToken}'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: accountName
  params: {
    name: accountName
    location: location
    tags: tags
    kind: 'OpenAI'
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
    // No feature in use makes an outbound call.
    restrictOutboundNetworkAccess: true
    allowedFqdnList: []
    dynamicThrottlingEnabled: false
    deployments: []
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
