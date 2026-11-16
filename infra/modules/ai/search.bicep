// Azure AI Search (83-infrastructure.md section 2.13). Role-based access only, no identity (the
// service makes no outbound connection: no indexer, skillset, vectorizer or customer-managed key),
// and no index at deployment: the ingestion app creates the physical indexes from the schemas in
// packages/core through the data-plane API.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object

@description('search.sku of the environment file.')
param sku 'basic' | 'standard'

@description('search.replicas, already checked by main.bicep against the minimum of 2 outside dev.')
param replicaCount int

@description('search.partitions; exactly 1.')
param partitionCount int

@description('Operator allowlist; empty disables public network access.')
param operatorIpAllowlist string[]

param workspaceId string

var serviceName = 'srch-docchat-${environmentName}-${resourceToken}'

module service 'br/public:avm/res/search/search-service:0.13.0' = {
  name: serviceName
  params: {
    name: serviceName
    location: location
    tags: tags
    sku: sku
    replicaCount: replicaCount
    partitionCount: partitionCount
    hostingMode: 'Default'
    // The free plan returns billing errors once its monthly allowance is spent, which would stop
    // retrieval mid-month; standard charges only beyond the allowance.
    semanticSearch: 'standard'
    // authOptions stays unset: with local authentication off only Entra tokens are accepted.
    disableLocalAuth: true
    publicNetworkAccess: empty(operatorIpAllowlist) ? 'Disabled' : 'Enabled'
    networkRuleSet: {
      ipRules: [for address in operatorIpAllowlist: { value: address }]
      bypass: 'None'
    }
    computeType: 'Default'
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'OperationLogs' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

output id string = service.outputs.resourceId
output name string = serviceName
output endpoint string = 'https://${serviceName}.search.windows.net'
