// The exported, sealed types of 83-infrastructure.md section 8.3 and 81-config.md section 4.
// Every object type is sealed, so an undeclared member in an environment file is a compile error
// instead of warning BCP089 (ADR 0096).
//
// Bicep has no fractional number type: a JSON fraction such as 0.5 loads as a number that an
// `int` property accepts, so the fractional fields (`sql.minCapacity`, `telemetrySamplingRatio`)
// are typed `int` and their ranges are checked by the preflight and the settings classes.

@export()
@description('Azure OpenAI deployment SKU, written to the deployment\'s sku.name (ADR 0099).')
type deploymentType = 'GlobalStandard' | 'DataZoneStandard' | 'Standard'

@export()
type tokenEncoding = 'o200k_base' | 'cl100k_base'

@export()
@sealed()
@description('One chat model deployment (83-infrastructure.md section 5.2).')
type openAiDeployment = {
  @minLength(2)
  @maxLength(64)
  name: string
  model: string
  @minLength(1)
  version: string
  type: deploymentType
  @minValue(1)
  @maxValue(10000)
  capacity: int
  versionUpgradeOption: 'NoAutoUpgrade' | 'OnceCurrentVersionExpired' | 'OnceNewDefaultVersionAvailable'
  @minLength(1)
  raiPolicyName: string
  encoding: tokenEncoding
}

@export()
@sealed()
@description('One embedding model, deployed as embed-query-<key> and embed-ingest-<key>.')
type embeddingModel = {
  @minLength(1)
  key: string
  model: string
  @minLength(1)
  version: string
  queryType: deploymentType
  ingestionType: deploymentType
  @minValue(1)
  @maxValue(10000)
  queryCapacity: int
  @minValue(1)
  @maxValue(10000)
  ingestionCapacity: int
  defaultDimensions: int
  @minLength(1)
  allowedDimensions: int[]
  @minValue(1)
  maxInputTokens: int
  encoding: tokenEncoding
}

@sealed()
type networkConfig = {
  @description('IPv4 /22; the subnets are derived with cidrSubnet() (section 4.1).')
  addressSpace: string
}

@sealed()
type webConfig = {
  sku: 'Free' | 'Standard'
}

@sealed()
type apiConfig = {
  sku: 'B1' | 'P0v3' | 'P1v3' | 'P2v3'
  @minValue(1)
  @maxValue(30)
  minInstances: int
  @minValue(1)
  @maxValue(30)
  maxInstances: int
  zoneRedundant: bool
}

@sealed()
type ingestionConfig = {
  sku: 'EP1' | 'EP2' | 'EP3'
  @minValue(1)
  @maxValue(20)
  alwaysReady: int
  @minValue(1)
  @maxValue(100)
  maxBurst: int
}

@sealed()
type sqlConfig = {
  sku: 'GP_S_Gen5_2' | 'GP_Gen5_2' | 'GP_Gen5_4'
  @description('Serverless floor in vCores, 0.5 to 2; null where the SKU is provisioned.')
  minCapacity: int?
  @minValue(-1)
  @maxValue(10080)
  autoPauseDelayMinutes: int
  @minValue(1)
  @maxValue(1024)
  maxSizeGb: int
  zoneRedundant: bool
  backupRedundancy: 'Local' | 'Zone' | 'Geo'
  @minValue(1)
  @maxValue(35)
  pitrDays: int
}

@sealed()
type cosmosThroughput = {
  @minValue(1000)
  @maxValue(100000)
  conversations: int
  @minValue(1000)
  @maxValue(100000)
  memories: int
  @minValue(1000)
  @maxValue(100000)
  usage: int
}

@sealed()
type cosmosConfig = {
  serverless: bool
  backupTier: 'Continuous7Days' | 'Continuous30Days'
  @description('Autoscale maxima, applied when the containers are created; null where the account is serverless.')
  maxThroughput: cosmosThroughput?
}

@sealed()
type searchConfig = {
  sku: 'basic' | 'standard'
  @minValue(1)
  @maxValue(12)
  replicas: int
  @minValue(1)
  @maxValue(1)
  partitions: int
}

@sealed()
type openAiDeployments = {
  chat: openAiDeployment
  chatSmall: openAiDeployment
  tableSummary: openAiDeployment
}

@sealed()
type openAiConfig = {
  enabled: bool
  deployments: openAiDeployments
  @minLength(1)
  @maxLength(8)
  embeddingModels: embeddingModel[]
}

@sealed()
type modelStubConfig = {
  enabled: bool
}

@sealed()
type storageConfig = {
  malwareScanning: bool
  @minValue(1)
  @maxValue(10000)
  malwareScanCapGbPerMonth: int
}

@sealed()
type keyVaultConfig = {
  purgeProtection: bool
  @minValue(7)
  @maxValue(90)
  softDeleteDays: int
}

@sealed()
type monitoringConfig = {
  @minValue(30)
  @maxValue(730)
  retentionDays: int
  @minValue(-1)
  @maxValue(100)
  dailyCapGb: int
}

@sealed()
type alertsConfig = {
  enabled: bool
  @minValue(2000)
  @maxValue(120000)
  ttftP95Ms: int
  @minValue(1000)
  @maxValue(120000)
  openAiFirstChunkP95Ms: int
  @minValue(1000)
  @maxValue(120000)
  openAiCallMeanMs: int
}

@sealed()
type budgetConfig = {
  enabled: bool
  @minValue(1)
  amount: int
}

@sealed()
@description('Entra object IDs written by the bootstrap\'s config step (83-infrastructure.md section 11.2).')
type groupIds = {
  @minLength(36)
  @maxLength(36)
  operators: string
  @minLength(36)
  @maxLength(36)
  sqlAdmins: string
  @minLength(36)
  @maxLength(36)
  users: string
  @minLength(36)
  @maxLength(36)
  admins: string
  @minLength(36)
  @maxLength(36)
  configOperators: string
  @minLength(36)
  @maxLength(36)
  conversationReaders: string
  @minLength(36)
  @maxLength(36)
  userDataOperators: string
}

@export()
@sealed()
@description('One environment file, infra/env/<env>.json (83-infrastructure.md section 8.3).')
type environmentConfig = {
  @minLength(36)
  @maxLength(36)
  subscriptionId: string
  @minLength(36)
  @maxLength(36)
  tenantId: string
  location: string
  staticWebAppLocation: string
  network: networkConfig
  @maxLength(5)
  webCustomDomains: string[]
  web: webConfig
  api: apiConfig
  ingestion: ingestionConfig
  sql: sqlConfig
  cosmos: cosmosConfig
  @minValue(0)
  @maxValue(3650)
  conversationRetentionDays: int
  @minValue(0)
  @maxValue(3650)
  memoryRetentionDays: int
  cosmosGroupDataAccess: bool
  search: searchConfig
  openAi: openAiConfig
  modelStub: modelStubConfig
  storage: storageConfig
  keyVault: keyVaultConfig
  monitoring: monitoringConfig
  @description('Trace sampling ratio, 0.1 to 1.0.')
  telemetrySamplingRatio: int
  alerts: alertsConfig
  budget: budgetConfig
  groups: groupIds
}
