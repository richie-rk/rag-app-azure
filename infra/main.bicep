// One DocChat environment (83-infrastructure.md sections 2 and 8; ADRs 0095 and 0096). dev, prod
// and load deploy this same template into rg-docchat-<env>, differing only in their environment
// file. Declared so far: the network, monitoring, the Key Vault, SQL, both storage accounts, the
// workload identities and their roles, the ingestion plan and function app, the API plan, its
// autoscale setting and the API site, Cosmos DB, the Static Web App, the app registrations, AI
// Search, the Azure OpenAI account with all five model deployments, Content Safety and Document
// Intelligence, and in load the model stub host in place of Azure OpenAI (section 2.21).

targetScope = 'resourceGroup'

extension graphV1

import { environmentConfig } from 'types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string

@description('DOCCHAT_API_IMAGE_CURRENT: the running image, so provisioning never changes it.')
param apiImage string

@description('DOCCHAT_INGESTION_IMAGE_CURRENT: the running image, so provisioning never changes it.')
param ingestionImage string

@description('DOCCHAT_MODEL_STUB_IMAGE_CURRENT: the model stub\'s digest reference in load, empty elsewhere.')
param modelStubImage string

@description('DOCCHAT_OPERATOR_IP_ALLOWLIST: single public IPv4 addresses, at most 20.')
@maxLength(20)
param operatorIpAllowlist string[]

@description('DOCCHAT_RESOURCE_TOKEN_SALT: run-<GitHub run ID> in load, empty elsewhere.')
param resourceTokenSalt string

@description('DOCCHAT_RELEASE_SHA: the deployed commit (40 hexadecimal characters), or local.')
param releaseSha string

@description('DOCCHAT_OPS_ALERT_EMAILS: shared mailboxes receiving ag-docchat-<env>-ops.')
param opsAlertEmails string[]

@description('DOCCHAT_BUDGET_START_DATE, yyyy-MM-01, set by the preprovision hook.')
param budgetStartDate string

// Every build type-checks all three files, whichever environment it targets.
var envConfigs {dev: environmentConfig, prod: environmentConfig, load: environmentConfig} = {
  dev: loadJsonContent('env/dev.json')
  prod: loadJsonContent('env/prod.json')
  load: loadJsonContent('env/load.json')
}
var config = envConfigs[environmentName]
var appSettingDefaults = loadJsonContent('config/app-settings.json')

var location = config.location

// load takes new globally unique names on every run, so no soft-deleted name of an earlier run
// blocks the next one (section 1.4).
var resourceToken = environmentName == 'load'
  ? take(uniqueString(subscription().id, environmentName, resourceTokenSalt), 6)
  : take(uniqueString(subscription().id, environmentName), 6)
var sharedToken = take(uniqueString(subscription().id, 'shared'), 6)
var sharedResourceGroupName = 'rg-docchat-shared'
var registryName = 'crdocchat${sharedToken}'
// Names the role and diagnostics modules address resources by; computed here rather than read
// from module outputs, so what-if can predict those modules (lint rule what-if-short-circuiting).
var cosmosAccountName = 'cosmos-docchat-${environmentName}-${resourceToken}'
var dataStorageAccountName = 'stdocchatdata${environmentName}${resourceToken}'
var hostStorageAccountName = 'stdocchathost${environmentName}${resourceToken}'
var workspaceName = 'log-docchat-${environmentName}'
var appInsightsName = 'appi-docchat-${environmentName}'
var searchServiceName = 'srch-docchat-${environmentName}-${resourceToken}'
var openAiAccountName = 'oai-docchat-${environmentName}-${resourceToken}'
var contentSafetyAccountName = 'cs-docchat-${environmentName}-${resourceToken}'
var docIntelAccountName = 'di-docchat-${environmentName}-${resourceToken}'
var apiSiteName = 'app-docchat-api-${environmentName}-${resourceToken}'
var registryId = resourceId(
  subscription().subscriptionId,
  sharedResourceGroupName,
  'Microsoft.ContainerRegistry/registries',
  registryName
)

var baseTags = {
  'docchat-env': environmentName
  'azd-env-name': environmentName
}
var networkTags = union(baseTags, { 'docchat-component': 'network' })
var monitoringTags = union(baseTags, { 'docchat-component': 'monitoring' })
var dataTags = union(baseTags, { 'docchat-component': 'data' })
var ingestionTags = union(baseTags, { 'docchat-component': 'ingestion' })
var apiTags = union(baseTags, { 'docchat-component': 'api' })
var securityTags = union(baseTags, { 'docchat-component': 'security' })
var aiTags = union(baseTags, { 'docchat-component': 'ai' })

// Exactly one of openAi.enabled and modelStub.enabled is true; load calls the model stub and
// consumes no model quota (section 2.21).
var openAiEnabled = config.openAi.enabled
var modelStubEnabled = config.modelStub.enabled
// The stub's default host name follows from its name, so the apps' endpoint setting needs no
// output of the stub module.
var modelStubSiteName = 'app-docchat-stub-load-${resourceToken}'
var modelStubHostName = '${modelStubSiteName}.azurewebsites.net'
var openAiEndpoint = openAiEnabled ? 'https://${openAiAccountName}.openai.azure.com' : 'https://${modelStubHostName}'

// Derived, never a parameter (section 8.4): prod and load keep the query SLA's two replicas.
var searchReplicas = environmentName != 'dev' && config.search.replicas < 2
  ? fail('search.replicas must be at least 2 in prod and load')
  : config.search.replicas
var searchMaxIndexes = { basic: 15, standard: 50 }[config.search.sku]

// The API calls only chat, chat-small and embed-query-<key>, and the ingestion app only
// ingest-summary and embed-ingest-<key>, so a bulk upload's throttling stays on ingestion's own
// deployments and never starves chat (section 5.3). Each deployment takes its type and capacity
// from its own fields; none inherits another's.
var textDeployments = [
  config.openAi.deployments.chat
  config.openAi.deployments.chatSmall
  config.openAi.deployments.tableSummary
]
var embeddingDeployments = concat(
  map(config.openAi.embeddingModels, entry => {
    name: 'embed-query-${entry.key}'
    model: entry.model
    version: entry.version
    type: entry.queryType
    capacity: entry.queryCapacity
  }),
  map(config.openAi.embeddingModels, entry => {
    name: 'embed-ingest-${entry.key}'
    model: entry.model
    version: entry.version
    type: entry.ingestionType
    capacity: entry.ingestionCapacity
  })
)

var subnetPrefixes = {
  api: cidrSubnet(config.network.addressSpace, 24, 0)
  ingest: cidrSubnet(config.network.addressSpace, 24, 1)
  privateEndpoints: cidrSubnet(config.network.addressSpace, 24, 2)
  // The first /26 of the fourth /24 (load only); the rest of that /24 stays reserved.
  stub: cidrSubnet(config.network.addressSpace, 26, 12)
}

var zoneNames = {
  sql: 'privatelink${environment().suffixes.sqlServerHostname}'
  cosmos: 'privatelink.documents.azure.com'
  blob: 'privatelink.blob.${environment().suffixes.storage}'
  queue: 'privatelink.queue.${environment().suffixes.storage}'
  table: 'privatelink.table.${environment().suffixes.storage}'
  vault: 'privatelink.vaultcore.azure.net'
  registry: 'privatelink${environment().suffixes.acrLoginServer}'
  search: 'privatelink.search.windows.net'
  openAi: 'privatelink.openai.azure.com'
  cognitiveServices: 'privatelink.cognitiveservices.azure.com'
  sites: 'privatelink.azurewebsites.net'
}

// The workbooks and alert queries name the API's resources by ID; dev has no autoscale setting,
// so the capacity workbook's autoscale tile stays empty there.
var apiPlanId = resourceId('Microsoft.Web/serverfarms', 'asp-docchat-api-${environmentName}')
var apiSiteId = resourceId('Microsoft.Web/sites', apiSiteName)
var autoscaleId = resourceId('Microsoft.Insights/autoscalesettings', 'autoscale-docchat-api-${environmentName}')

module monitoring 'modules/monitoring/workspace.bicep' = {
  name: 'monitoring'
  params: {
    environmentName: environmentName
    location: location
    tags: monitoringTags
    config: config
  }
}

module actionGroup 'modules/monitoring/action-group.bicep' = {
  name: 'action-group'
  params: {
    environmentName: environmentName
    tags: monitoringTags
    opsAlertEmails: opsAlertEmails
  }
}

module budget 'modules/monitoring/budget.bicep' = if (config.budget.enabled) {
  name: 'budget'
  params: {
    environmentName: environmentName
    amount: config.budget.amount
    startDate: budgetStartDate
    actionGroupId: actionGroup.outputs.id
  }
}

module nsgs 'modules/network/nsgs.bicep' = {
  name: 'nsgs'
  params: {
    environmentName: environmentName
    location: location
    tags: networkTags
    subnetPrefixes: subnetPrefixes
  }
}

module network 'modules/network/vnet.bicep' = {
  name: 'network'
  params: {
    environmentName: environmentName
    location: location
    tags: networkTags
    addressSpace: config.network.addressSpace
    subnetPrefixes: subnetPrefixes
    privateEndpointsNsgId: nsgs.outputs.privateEndpointsNsgId
    apiNsgId: nsgs.outputs.apiNsgId
    ingestNsgId: nsgs.outputs.ingestNsgId
    stubNsgId: nsgs.outputs.stubNsgId
  }
}

module privateDns 'modules/network/private-dns.bicep' = {
  name: 'private-dns'
  params: {
    tags: networkTags
    zoneNames: concat(
      [
        zoneNames.sql
        zoneNames.cosmos
        zoneNames.blob
        zoneNames.queue
        zoneNames.table
        zoneNames.vault
        zoneNames.registry
        zoneNames.search
        zoneNames.cognitiveServices
      ],
      openAiEnabled ? [zoneNames.openAi] : [],
      modelStubEnabled ? [zoneNames.sites] : []
    )
    vnetId: network.outputs.vnetId
    vnetName: network.outputs.vnetName
  }
}

module identities 'modules/identity/workload-identities.bicep' = {
  name: 'workload-identities'
  params: {
    environmentName: environmentName
    location: location
    baseTags: baseTags
    modelStubEnabled: modelStubEnabled
  }
}

module keyVault 'modules/security/key-vault.bicep' = {
  name: 'key-vault'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: securityTags
    config: config
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module sql 'modules/data/sql.bicep' = {
  name: 'sql'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: dataTags
    config: config
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module cosmos 'modules/data/cosmos.bicep' = {
  name: 'cosmos'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: dataTags
    config: config
    operatorIpAllowlist: operatorIpAllowlist
  }
}

module dataStorage 'modules/data/storage-data.bicep' = {
  name: 'storage-data'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: dataTags
    config: config
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module hostStorage 'modules/data/storage-host.bicep' = {
  name: 'storage-host'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: ingestionTags
    workspaceId: monitoring.outputs.workspaceId
  }
}

module search 'modules/ai/search.bicep' = {
  name: 'search'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: aiTags
    sku: config.search.sku
    replicaCount: searchReplicas
    partitionCount: config.search.partitions
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module openAi 'modules/ai/openai.bicep' = if (openAiEnabled) {
  name: 'openai'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: aiTags
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module openAiDeployments 'modules/ai/openai-deployments.bicep' = if (openAiEnabled) {
  name: 'openai-deployments'
  params: {
    accountName: openAiAccountName
    textDeployments: textDeployments
    embeddingDeployments: embeddingDeployments
  }
  dependsOn: [openAi]
}

module contentSafety 'modules/ai/content-safety.bicep' = {
  name: 'content-safety'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: aiTags
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module documentIntelligence 'modules/ai/document-intelligence.bicep' = {
  name: 'document-intelligence'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: aiTags
    operatorIpAllowlist: operatorIpAllowlist
    workspaceId: monitoring.outputs.workspaceId
  }
}

module diagnostics 'modules/monitoring/diagnostics.bicep' = {
  name: 'diagnostics'
  params: {
    workspaceId: monitoring.outputs.workspaceId
    cosmosAccountName: cosmosAccountName
    dataStorageAccountName: dataStorageAccountName
    malwareScanning: config.storage.malwareScanning
  }
  dependsOn: [cosmos, dataStorage]
}

module cosmosDataRoles 'modules/rbac/cosmos-data-roles.bicep' = {
  name: 'cosmos-data-roles'
  params: {
    accountName: cosmosAccountName
    apiPrincipalId: identities.outputs.apiPrincipalId
    conversationReadersGroupId: config.groups.conversationReaders
    userDataOperatorsGroupId: config.groups.userDataOperators
    groupDataAccess: config.cosmosGroupDataAccess
  }
  dependsOn: [cosmos]
}

module workloadRoles 'modules/rbac/workload-roles.bicep' = {
  name: 'workload-roles'
  params: {
    apiPrincipalId: identities.outputs.apiPrincipalId
    ingestionPrincipalId: identities.outputs.ingestionPrincipalId
    operatorsGroupId: config.groups.operators
    configOperatorsGroupId: config.groups.configOperators
    userDataOperatorsGroupId: config.groups.userDataOperators
    dataStorageAccountName: dataStorageAccountName
    hostStorageAccountName: hostStorageAccountName
    appInsightsName: appInsightsName
    workspaceName: workspaceName
    searchServiceName: searchServiceName
    openAiEnabled: openAiEnabled
    openAiAccountName: openAiAccountName
    contentSafetyAccountName: contentSafetyAccountName
    docIntelAccountName: docIntelAccountName
  }
  dependsOn: [dataStorage, hostStorage, monitoring, search, openAi, contentSafety, documentIntelligence]
}

module registryPull 'modules/rbac/registry-pull.bicep' = {
  name: 'registry-pull-${environmentName}-${resourceToken}'
  scope: resourceGroup(sharedResourceGroupName)
  params: {
    registryName: registryName
    principalIds: concat(
      [
        identities.outputs.apiPrincipalId
        identities.outputs.ingestionPrincipalId
      ],
      modelStubEnabled ? [identities.outputs.stubPrincipalId] : []
    )
  }
}

module staticSite 'modules/web/static-site.bicep' = {
  name: 'static-site'
  params: {
    environmentName: environmentName
    tags: union(baseTags, { 'docchat-component': 'web' })
    config: config
  }
}

module entraApps 'modules/identity/entra-apps.bicep' = {
  name: 'entra-apps'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    tenantId: toLower(tenant().tenantId)
    staticWebAppHostName: staticSite.outputs.defaultHostname
    webCustomDomains: config.webCustomDomains
  }
}

module appSettings 'modules/compute/app-settings.bicep' = {
  name: 'app-settings'
  params: {
    environmentName: environmentName
    config: config
    apiDefaults: appSettingDefaults.api
    ingestionDefaults: appSettingDefaults.ingestion
    modelStubDefaults: appSettingDefaults.modelStub
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    apiIdentityClientId: identities.outputs.apiClientId
    apiIdentityPrincipalId: identities.outputs.apiPrincipalId
    ingestionIdentityClientId: identities.outputs.ingestionClientId
    ingestionIdentityPrincipalId: identities.outputs.ingestionPrincipalId
    sqlServerFqdn: sql.outputs.fullyQualifiedDomainName
    cosmosEndpoint: cosmos.outputs.documentEndpoint
    dataBlobEndpoint: dataStorage.outputs.blobEndpoint
    dataQueueEndpoint: dataStorage.outputs.queueEndpoint
    hostBlobEndpoint: hostStorage.outputs.blobEndpoint
    hostQueueEndpoint: hostStorage.outputs.queueEndpoint
    hostTableEndpoint: hostStorage.outputs.tableEndpoint
    searchEndpoint: search.outputs.endpoint
    openAiEndpoint: openAiEndpoint
    docIntelEndpoint: documentIntelligence.outputs.endpoint
    contentSafetyEndpoint: contentSafety.outputs.endpoint
    searchMaxIndexes: searchMaxIndexes
    tenantId: toLower(tenant().tenantId)
    apiAppId: entraApps.outputs.apiAppId
    webAppId: entraApps.outputs.webAppId
    testAppId: entraApps.outputs.testAppId
  }
}

module api 'modules/compute/api.bicep' = {
  name: 'api'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: apiTags
    config: config
    image: apiImage
    releaseSha: releaseSha
    identityResourceId: identities.outputs.apiResourceId
    identityClientId: identities.outputs.apiClientId
    subnetId: network.outputs.apiSubnetId
    appSettings: appSettings.outputs.api
    workspaceId: monitoring.outputs.workspaceId
    tenantId: toLower(tenant().tenantId)
    apiAppId: entraApps.outputs.apiAppId
    webAppId: entraApps.outputs.webAppId
    testAppId: entraApps.outputs.testAppId
    staticWebAppHostName: staticSite.outputs.defaultHostname
  }
  // The identity must hold AcrPull before the first pull, and its data roles before the app serves.
  dependsOn: [workloadRoles, cosmosDataRoles, registryPull]
}

module ingestion 'modules/compute/ingestion.bicep' = {
  name: 'ingestion'
  params: {
    environmentName: environmentName
    resourceToken: resourceToken
    location: location
    tags: ingestionTags
    config: config
    image: ingestionImage
    identityResourceId: identities.outputs.ingestionResourceId
    identityClientId: identities.outputs.ingestionClientId
    subnetId: network.outputs.ingestSubnetId
    appSettings: appSettings.outputs.ingestion
    workspaceId: monitoring.outputs.workspaceId
  }
  // The app's identity must hold its host-storage and queue roles before the host starts.
  dependsOn: [workloadRoles, registryPull]
}

module modelStub 'modules/compute/model-stub.bicep' = if (modelStubEnabled) {
  name: 'model-stub'
  params: {
    resourceToken: resourceToken
    location: location
    tags: aiTags
    image: modelStubImage
    identityResourceId: identities.outputs.stubResourceId
    identityClientId: identities.outputs.stubClientId
    subnetId: network.outputs.stubSubnetId
    appSettings: appSettings.outputs.modelStub
    workspaceId: monitoring.outputs.workspaceId
  }
  // The identity must hold AcrPull before the first pull.
  dependsOn: [registryPull]
}

module privateEndpoints 'modules/network/private-endpoints.bicep' = {
  name: 'private-endpoints'
  params: {
    location: location
    tags: networkTags
    subnetId: network.outputs.privateEndpointsSubnetId
    endpoints: concat(
      [
        {
          targetName: sql.outputs.name
          groupId: 'sqlServer'
          targetId: sql.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.sql]
        }
        {
          targetName: cosmos.outputs.name
          groupId: 'Sql'
          targetId: cosmos.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.cosmos]
        }
        {
          targetName: dataStorage.outputs.name
          groupId: 'blob'
          targetId: dataStorage.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.blob]
        }
        {
          targetName: dataStorage.outputs.name
          groupId: 'queue'
          targetId: dataStorage.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.queue]
        }
        {
          targetName: hostStorage.outputs.name
          groupId: 'blob'
          targetId: hostStorage.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.blob]
        }
        {
          targetName: hostStorage.outputs.name
          groupId: 'queue'
          targetId: hostStorage.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.queue]
        }
        {
          targetName: hostStorage.outputs.name
          groupId: 'table'
          targetId: hostStorage.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.table]
        }
        {
          targetName: keyVault.outputs.name
          groupId: 'vault'
          targetId: keyVault.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.vault]
        }
        {
          // Approved automatically: each deploy identity holds the approval action on the registry.
          targetName: registryName
          groupId: 'registry'
          targetId: registryId
          zoneId: privateDns.outputs.zoneIds[zoneNames.registry]
        }
        {
          targetName: search.outputs.name
          groupId: 'searchService'
          targetId: search.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.search]
        }
        {
          targetName: contentSafety.outputs.name
          groupId: 'account'
          targetId: contentSafety.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.cognitiveServices]
        }
        {
          targetName: documentIntelligence.outputs.name
          groupId: 'account'
          targetId: documentIntelligence.outputs.id
          zoneId: privateDns.outputs.zoneIds[zoneNames.cognitiveServices]
        }
      ],
      openAiEnabled
        ? [
            {
              targetName: openAiAccountName
              groupId: 'account'
              targetId: resourceId('Microsoft.CognitiveServices/accounts', openAiAccountName)
              zoneId: privateDns.outputs.zoneIds[zoneNames.openAi]
            }
          ]
        : [],
      modelStubEnabled
        ? [
            {
              targetName: modelStubSiteName
              groupId: 'sites'
              targetId: resourceId('Microsoft.Web/sites', modelStubSiteName)
              zoneId: privateDns.outputs.zoneIds[zoneNames.sites]
            }
          ]
        : []
    )
  }
  // The Azure OpenAI account and the model stub are conditional, so their endpoints name them by ID.
  dependsOn: [openAi, modelStub]
}

module alerts 'modules/monitoring/alerts.bicep' = {
  name: 'alerts'
  params: {
    environmentName: environmentName
    location: location
    tags: monitoringTags
    workspaceId: monitoring.outputs.workspaceId
    actionGroupId: actionGroup.outputs.id
    alertsEnabled: config.alerts.enabled
    malwareScanning: config.storage.malwareScanning
    // The metric rules' scope must exist before they do.
    apiSiteId: api.outputs.id
    apiPlanId: apiPlanId
    apiMaxInstances: config.api.maxInstances
    apiPrincipalId: identities.outputs.apiPrincipalId
    ttftP95Ms: config.alerts.ttftP95Ms
    openAiFirstChunkP95Ms: config.alerts.openAiFirstChunkP95Ms
    openAiCallMeanMs: config.alerts.openAiCallMeanMs
  }
}

module workbooks 'modules/monitoring/workbooks.bicep' = {
  name: 'workbooks'
  params: {
    environmentName: environmentName
    location: location
    tags: monitoringTags
    workspaceId: monitoring.outputs.workspaceId
    apiSiteId: apiSiteId
    apiPlanId: apiPlanId
    autoscaleId: autoscaleId
    budgetId: config.budget.enabled ? budget!.outputs.id : ''
    apiPrincipalId: identities.outputs.apiPrincipalId
  }
}

output AZURE_TENANT_ID string = tenant().tenantId
output WEB_CLIENT_ID string = entraApps.outputs.webAppId
output API_CLIENT_ID string = entraApps.outputs.apiAppId
output TEST_CLIENT_ID string = entraApps.outputs.testAppId
output API_SCOPE string = entraApps.outputs.apiScope
output API_BASE_URL string = api.outputs.baseUrl
output WEB_BASE_URL string = 'https://${empty(config.webCustomDomains) ? staticSite.outputs.defaultHostname : config.webCustomDomains[0]}'
output DOCCHAT_RESOURCE_TOKEN string = resourceToken
output DOCCHAT_STATIC_WEB_APP_NAME string = staticSite.outputs.name
output DOCCHAT_API_APP_NAME string = api.outputs.name
output DOCCHAT_INGESTION_APP_NAME string = ingestion.outputs.name
output DOCCHAT_MODEL_STUB_APP_NAME string = modelStubEnabled ? modelStubSiteName : ''
output DOCCHAT_SQL_SERVER_NAME string = sql.outputs.name
output DOCCHAT_SQL_SERVER string = sql.outputs.fullyQualifiedDomainName
output DOCCHAT_SQL_DATABASE string = 'docchat'
output DOCCHAT_COSMOS_ACCOUNT_NAME string = cosmos.outputs.name
output DOCCHAT_COSMOS_ENDPOINT string = cosmos.outputs.documentEndpoint
output DOCCHAT_COSMOS_DATABASE string = 'docchat'
output DOCCHAT_COSMOS_GROUP_DATA_ACCESS bool = config.cosmosGroupDataAccess
output DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS string = join(cosmosDataRoles.outputs.assignmentIds, ',')
output DOCCHAT_STORAGE_ACCOUNT_NAME string = dataStorage.outputs.name
output DOCCHAT_STORAGE_BLOB_ENDPOINT string = dataStorage.outputs.blobEndpoint
output DOCCHAT_STORAGE_QUEUE_ENDPOINT string = dataStorage.outputs.queueEndpoint
output DOCCHAT_SEARCH_SERVICE_NAME string = search.outputs.name
output DOCCHAT_SEARCH_ENDPOINT string = search.outputs.endpoint
output DOCCHAT_SEARCH_MAX_INDEXES string = string(searchMaxIndexes)
output DOCCHAT_OPENAI_ACCOUNT_NAME string = openAiEnabled ? openAiAccountName : ''
output DOCCHAT_OPENAI_ENDPOINT string = openAiEndpoint
// Read only by docchat-ops chat capacity-check, never an app setting; empty in load, which has no
// Azure OpenAI account and which the command treats as absent (section 5.7).
output DOCCHAT_OPENAI_CHAT_CAPACITY string = openAiEnabled ? string(config.openAi.deployments.chat.capacity) : ''
output DOCCHAT_OPENAI_SMALL_CAPACITY string = openAiEnabled ? string(config.openAi.deployments.chatSmall.capacity) : ''
output DOCCHAT_CONTENTSAFETY_ENDPOINT string = contentSafety.outputs.endpoint
output DOCCHAT_KEY_VAULT_NAME string = keyVault.outputs.name
output DOCCHAT_KEY_VAULT_URI string = keyVault.outputs.uri
output DOCCHAT_API_IDENTITY_NAME string = identities.outputs.apiName
output DOCCHAT_API_IDENTITY_CLIENT_ID string = identities.outputs.apiClientId
output DOCCHAT_API_IDENTITY_PRINCIPAL_ID string = identities.outputs.apiPrincipalId
output DOCCHAT_INGESTION_IDENTITY_NAME string = identities.outputs.ingestionName
output DOCCHAT_INGESTION_IDENTITY_CLIENT_ID string = identities.outputs.ingestionClientId
output DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID string = identities.outputs.ingestionPrincipalId
output DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceCustomerId
output DOCCHAT_LOG_ANALYTICS_RESOURCE_ID string = monitoring.outputs.workspaceId
output DOCCHAT_APPINSIGHTS_NAME string = monitoring.outputs.appInsightsName
output DOCCHAT_OPERATORS_GROUP_ID string = config.groups.operators
output DOCCHAT_CONFIG_OPERATORS_GROUP_ID string = config.groups.configOperators
output DOCCHAT_OPENAI_EMBEDDING_MODELS string = appSettings.outputs.embeddingModels
