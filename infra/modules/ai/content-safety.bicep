// The Content Safety account, whose Prompt Shields screen retrieved sources and memory facts
// before they reach a model (83-infrastructure.md section 2.14; 12-chat-pipeline.md section 8).
// Real in every environment, load included: the model stub stands in for Azure OpenAI only.
// Text is sent in the request body, never as a URL, so no feature in use calls out.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object

@description('Operator allowlist; empty disables public network access.')
param operatorIpAllowlist string[]

param workspaceId string

var accountName = 'cs-docchat-${environmentName}-${resourceToken}'

module account 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: accountName
  params: {
    name: accountName
    location: location
    tags: tags
    kind: 'ContentSafety'
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
// The custom-subdomain endpoint, in the one form both the API's DOCCHAT_CONTENTSAFETY_ENDPOINT and
// the azd output of the same name carry (83-infrastructure.md sections 8.8 and 8.9).
output endpoint string = 'https://${accountName}.cognitiveservices.azure.com'
