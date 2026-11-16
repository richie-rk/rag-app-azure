// Composes each app's settings from resource outputs, environment-file fields, the SKU maps and
// the owners' defaults in infra/config/app-settings.json, and nothing else (83-infrastructure.md
// section 8.9; 81-config.md section 2). No setting is set by hand, and none is a secret.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param config environmentConfig

@description('The api key of infra/config/app-settings.json.')
param apiDefaults object

@description('The ingestion key of infra/config/app-settings.json.')
param ingestionDefaults object

@description('The modelStub key of infra/config/app-settings.json: the DOCCHAT_STUB_* values.')
param modelStubDefaults object

param appInsightsConnectionString string
param apiIdentityClientId string
param apiIdentityPrincipalId string
param ingestionIdentityClientId string
param ingestionIdentityPrincipalId string
param sqlServerFqdn string
param cosmosEndpoint string
param dataBlobEndpoint string
param dataQueueEndpoint string
param hostBlobEndpoint string
param hostQueueEndpoint string
param hostTableEndpoint string
param searchEndpoint string

@description('The Azure OpenAI account\'s endpoint where openAi.enabled is true, else the model stub\'s.')
param openAiEndpoint string
param docIntelEndpoint string
param contentSafetyEndpoint string

@description('DOCCHAT_SEARCH_MAX_INDEXES, the derived {basic: 15, standard: 50}[search.sku] of main.bicep.')
param searchMaxIndexes int

param tenantId string
param apiAppId string
param webAppId string

@description('The test client\'s appId; empty in prod, which has no test client.')
param testAppId string

// One Gunicorn worker per vCPU, moving with the plan's SKU (section 8.4).
var apiWorkers = { B1: 1, P0v3: 1, P1v3: 2, P2v3: 4 }[config.api.sku]
// Two activity slots per worker process, so the dispatcher's activities never queue behind a parse.
var workerProcessCount = { EP1: 1, EP2: 2, EP3: 4 }[config.ingestion.sku]
var durableConcurrency = { EP1: 2, EP2: 4, EP3: 8 }[config.ingestion.sku]

var embeddingCatalogue = map(
  config.openAi.embeddingModels,
  entry => {
    model: entry.model
    modelVersion: entry.version
    queryDeployment: 'embed-query-${entry.key}'
    ingestionDeployment: 'embed-ingest-${entry.key}'
    dimensions: entry.allowedDimensions
    defaultDimensions: entry.defaultDimensions
    maxInputTokens: entry.maxInputTokens
    encoding: entry.encoding
  }
)

var apiSettings = union(apiDefaults, {
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsightsConnectionString
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD;ClientId=${apiIdentityClientId}'
  DOCCHAT_TELEMETRY_SAMPLING_RATIO: string(config.telemetrySamplingRatio)
  DOCCHAT_CORE_ENVIRONMENT: environmentName
  DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID: apiIdentityPrincipalId
  DOCCHAT_CORE_IDENTITY_CLIENT_ID: apiIdentityClientId
  DOCCHAT_SQL_SERVER: sqlServerFqdn
  DOCCHAT_SQL_DATABASE: 'docchat'
  DOCCHAT_STORAGE_BLOB_ENDPOINT: dataBlobEndpoint
  DOCCHAT_STORAGE_QUEUE_ENDPOINT: dataQueueEndpoint
  DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED: config.storage.malwareScanning ? 'true' : 'false'
  DOCCHAT_SEARCH_ENDPOINT: searchEndpoint
  DOCCHAT_OPENAI_ENDPOINT: openAiEndpoint
  DOCCHAT_OPENAI_EMBEDDING_MODELS: string(embeddingCatalogue)
  DOCCHAT_AUTH_TENANT_ID: tenantId
  DOCCHAT_AUTH_API_CLIENT_ID: apiAppId
  DOCCHAT_AUTH_WEB_CLIENT_ID: webAppId
  DOCCHAT_AUTH_TEST_CLIENT_ID: testAppId
  // Platform authentication refuses every other tenant before a request reaches the app.
  WEBSITE_AUTH_AAD_ALLOWED_TENANTS: toLower(tenantId)
  DOCCHAT_API_WORKERS: string(apiWorkers)
  DOCCHAT_COSMOS_ENDPOINT: cosmosEndpoint
  DOCCHAT_COSMOS_DATABASE: 'docchat'
  DOCCHAT_SEARCH_MAX_INDEXES: string(searchMaxIndexes)
  DOCCHAT_OPENAI_CHAT_DEPLOYMENT: config.openAi.deployments.chat.name
  DOCCHAT_OPENAI_CHAT_MODEL: config.openAi.deployments.chat.model
  DOCCHAT_OPENAI_CHAT_MODEL_VERSION: config.openAi.deployments.chat.version
  DOCCHAT_OPENAI_CHAT_ENCODING: config.openAi.deployments.chat.encoding
  DOCCHAT_OPENAI_SMALL_DEPLOYMENT: config.openAi.deployments.chatSmall.name
  DOCCHAT_OPENAI_SMALL_MODEL: config.openAi.deployments.chatSmall.model
  DOCCHAT_OPENAI_SMALL_MODEL_VERSION: config.openAi.deployments.chatSmall.version
  DOCCHAT_OPENAI_SMALL_ENCODING: config.openAi.deployments.chatSmall.encoding
  DOCCHAT_CONTENTSAFETY_ENDPOINT: contentSafetyEndpoint
  WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'false'
  WEBSITES_PORT: '8000'
  WEBSITES_CONTAINER_START_TIME_LIMIT: '300'
  // The platform maximum, inside which Gunicorn's 110-second graceful timeout ends.
  WEBSITES_CONTAINER_STOP_TIME_LIMIT: '120'
  WEBSITE_HEALTHCHECK_MAXPINGFAILURES: '3'
})

var ingestionSettings = union(ingestionDefaults, {
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsightsConnectionString
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'Authorization=AAD;ClientId=${ingestionIdentityClientId}'
  DOCCHAT_TELEMETRY_SAMPLING_RATIO: string(config.telemetrySamplingRatio)
  DOCCHAT_CORE_ENVIRONMENT: environmentName
  DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID: ingestionIdentityPrincipalId
  DOCCHAT_CORE_IDENTITY_CLIENT_ID: ingestionIdentityClientId
  DOCCHAT_SQL_SERVER: sqlServerFqdn
  DOCCHAT_SQL_DATABASE: 'docchat'
  DOCCHAT_STORAGE_BLOB_ENDPOINT: dataBlobEndpoint
  DOCCHAT_STORAGE_QUEUE_ENDPOINT: dataQueueEndpoint
  DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED: config.storage.malwareScanning ? 'true' : 'false'
  DOCCHAT_SEARCH_ENDPOINT: searchEndpoint
  DOCCHAT_OPENAI_ENDPOINT: openAiEndpoint
  DOCCHAT_OPENAI_EMBEDDING_MODELS: string(embeddingCatalogue)
  DOCCHAT_OPENAI_TABLE_SUMMARY_DEPLOYMENT: config.openAi.deployments.tableSummary.name
  DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING: config.openAi.deployments.tableSummary.encoding
  DOCCHAT_DOCINTEL_ENDPOINT: docIntelEndpoint
  DOCCHAT_INGESTION_QUEUE__queueServiceUri: dataQueueEndpoint
  DOCCHAT_INGESTION_QUEUE__credential: 'managedidentity'
  DOCCHAT_INGESTION_QUEUE__clientId: ingestionIdentityClientId
  AzureWebJobsStorage__blobServiceUri: hostBlobEndpoint
  AzureWebJobsStorage__queueServiceUri: hostQueueEndpoint
  AzureWebJobsStorage__tableServiceUri: hostTableEndpoint
  AzureWebJobsStorage__credential: 'managedidentity'
  // Durable Functions doesn't support the resource-ID form of the identity.
  AzureWebJobsStorage__clientId: ingestionIdentityClientId
  FUNCTIONS_EXTENSION_VERSION: '~4'
  FUNCTIONS_WORKER_RUNTIME: 'python'
  FUNCTIONS_WORKER_PROCESS_COUNT: string(workerProcessCount)
  AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions: string(durableConcurrency)
  AzureFunctionsJobHost__extensions__durableTask__maxConcurrentOrchestratorFunctions: string(durableConcurrency)
  // Set here and never in host.json; letters and digits only.
  AzureFunctionsJobHost__extensions__durableTask__hubName: 'docchat${environmentName}'
  WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'false'
  // The image loads its pinned layout and table models at first start.
  WEBSITES_CONTAINER_START_TIME_LIMIT: '900'
})

// The model stub (load): its DOCCHAT_STUB_* values and the container's four constants.
var modelStubSettings = union(modelStubDefaults, {
  WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'false'
  WEBSITES_PORT: '8000'
  WEBSITES_CONTAINER_START_TIME_LIMIT: '300'
  WEBSITE_HEALTHCHECK_MAXPINGFAILURES: '3'
})

output api object = apiSettings
output ingestion object = ingestionSettings
output modelStub object = modelStubSettings
output embeddingModels string = string(embeddingCatalogue)
