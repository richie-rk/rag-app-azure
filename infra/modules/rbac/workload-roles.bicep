// Azure RBAC assignments of the environment's workload identities and groups, each at the
// narrowest scope that permits its call (83-infrastructure.md sections 6.3 to 6.9). Every name is
// guid(<scope ID>, <principal ID>, <role definition GUID>), so a redeploy is idempotent and the
// bootstrap can declare the same assignment under the same name. Only roles whose target this
// stage provisions are declared; the model stub's site (load, stage 6) needs none.

param apiPrincipalId string
param ingestionPrincipalId string

@description('Object IDs of sg-docchat-operators, sg-docchat-<env>-config-operators and sg-docchat-<env>-user-data-operators.')
param operatorsGroupId string
param configOperatorsGroupId string
param userDataOperatorsGroupId string

param dataStorageAccountName string
param hostStorageAccountName string
param appInsightsName string
param workspaceName string
param searchServiceName string

@description('openAi.enabled: load has no Azure OpenAI account, and its apps call the model stub with no role.')
param openAiEnabled bool
param openAiAccountName string
param contentSafetyAccountName string
param docIntelAccountName string

var roles = {
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageQueueDataReader: '19e7f393-937e-4f77-808e-94535e297925'
  storageQueueDataMessageProcessor: '8a0f0c08-91a1-4084-bc3d-661d67233fed'
  storageQueueDataMessageSender: 'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  docchatBlobTagReader: 'a5f5ab92-08e9-42d6-b9f2-31ca2b30a345'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  docchatSearchIndexManager: 'e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d'
  cognitiveServicesOpenAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
}

var api = {
  principalId: apiPrincipalId
  principalType: 'ServicePrincipal'
}
var ingestion = {
  principalId: ingestionPrincipalId
  principalType: 'ServicePrincipal'
}
var operators = {
  principalId: operatorsGroupId
  principalType: 'Group'
}
var configOperators = {
  principalId: configOperatorsGroupId
  principalType: 'Group'
}
var userDataOperators = {
  principalId: userDataOperatorsGroupId
  principalType: 'Group'
}

resource dataAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: dataStorageAccountName

  resource blobService 'blobServices' existing = {
    name: 'default'

    resource documents 'containers' existing = {
      name: 'documents'
    }

    resource renditions 'containers' existing = {
      name: 'renditions'
    }

    resource ingestionWork 'containers' existing = {
      name: 'ingestion-work'
    }
  }

  resource queueService 'queueServices' existing = {
    name: 'default'

    resource requests 'queues' existing = {
      name: 'ingestion-requests'
    }

    resource poison 'queues' existing = {
      name: 'ingestion-requests-poison'
    }
  }
}

resource hostAccount 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: hostStorageAccountName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' existing = {
  name: workspaceName
}

resource searchService 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchServiceName
}

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: openAiAccountName
}

resource contentSafetyAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: contentSafetyAccountName
}

resource docIntelAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: docIntelAccountName
}

var resourceGroupGrants = [
  union(operators, { roleId: roles.reader })
]

var workspaceGrants = [
  union(operators, { roleId: roles.logAnalyticsReader })
]

var appInsightsGrants = [
  union(api, { roleId: roles.monitoringMetricsPublisher })
  union(ingestion, { roleId: roles.monitoringMetricsPublisher })
]

// The Functions host and the Durable task hub.
var hostAccountGrants = [
  union(ingestion, { roleId: roles.storageBlobDataOwner })
  union(ingestion, { roleId: roles.storageQueueDataContributor })
  union(ingestion, { roleId: roles.storageTableDataContributor })
]

var documentsGrants = [
  union(api, { roleId: roles.storageBlobDataContributor })
  union(ingestion, { roleId: roles.storageBlobDataContributor })
  union(ingestion, { roleId: roles.docchatBlobTagReader })
  // docchat-ops document fetch-source; no write, delete or tag role.
  union(userDataOperators, { roleId: roles.storageBlobDataReader })
]

var renditionsGrants = [
  union(api, { roleId: roles.storageBlobDataReader })
  union(ingestion, { roleId: roles.storageBlobDataContributor })
]

var ingestionWorkGrants = [
  union(ingestion, { roleId: roles.storageBlobDataContributor })
]

var requestsQueueGrants = [
  union(api, { roleId: roles.storageQueueDataMessageSender })
  // The queue trigger gets and deletes messages; it never updates one.
  union(ingestion, { roleId: roles.storageQueueDataReader })
  union(ingestion, { roleId: roles.storageQueueDataMessageProcessor })
]

var poisonQueueGrants = [
  // The runtime's move after 5 dequeues, and the dispatcher's poison gauge.
  union(ingestion, { roleId: roles.storageQueueDataMessageSender })
  union(ingestion, { roleId: roles.storageQueueDataReader })
  // docchat-ops ingestion drain-poison.
  union(configOperators, { roleId: roles.storageQueueDataMessageProcessor })
]

var searchGrants = [
  // First-pass and broadened retrieval.
  union(api, { roleId: roles.searchIndexDataReader })
  // Physical index lifecycle (create, read, delete) and chunk upload and removal.
  union(ingestion, { roleId: roles.docchatSearchIndexManager })
  union(ingestion, { roleId: roles.searchIndexDataContributor })
]

var openAiGrants = openAiEnabled
  ? [
      // chat and chat-small calls, and query and passage embeddings (embed-query-<key>).
      union(api, { roleId: roles.cognitiveServicesOpenAiUser })
      // Table summaries (ingest-summary) and ingestion embeddings (embed-ingest-<key>).
      union(ingestion, { roleId: roles.cognitiveServicesOpenAiUser })
    ]
  : []

// Prompt Shields screening of sources and memory facts, in every environment: the model stub
// stands in for Azure OpenAI only.
var contentSafetyGrants = [
  union(api, { roleId: roles.cognitiveServicesUser })
]

// Layout analysis for the recursive method.
var docIntelGrants = [
  union(ingestion, { roleId: roles.cognitiveServicesUser })
]

resource resourceGroupAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in resourceGroupGrants: {
    name: guid(resourceGroup().id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource workspaceAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in workspaceGrants: {
    scope: workspace
    name: guid(workspace.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource appInsightsAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in appInsightsGrants: {
    scope: appInsights
    name: guid(appInsights.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource hostAccountAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in hostAccountGrants: {
    scope: hostAccount
    name: guid(hostAccount.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource documentsAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in documentsGrants: {
    scope: dataAccount::blobService::documents
    name: guid(dataAccount::blobService::documents.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource renditionsAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in renditionsGrants: {
    scope: dataAccount::blobService::renditions
    name: guid(dataAccount::blobService::renditions.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource ingestionWorkAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in ingestionWorkGrants: {
    scope: dataAccount::blobService::ingestionWork
    name: guid(dataAccount::blobService::ingestionWork.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource requestsQueueAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in requestsQueueGrants: {
    scope: dataAccount::queueService::requests
    name: guid(dataAccount::queueService::requests.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource poisonQueueAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in poisonQueueGrants: {
    scope: dataAccount::queueService::poison
    name: guid(dataAccount::queueService::poison.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource searchAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in searchGrants: {
    scope: searchService
    name: guid(searchService.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource openAiAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in openAiGrants: {
    scope: openAiAccount
    name: guid(openAiAccount.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource contentSafetyAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in contentSafetyGrants: {
    scope: contentSafetyAccount
    name: guid(contentSafetyAccount.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]

resource docIntelAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in docIntelGrants: {
    scope: docIntelAccount
    name: guid(docIntelAccount.id, grant.principalId, grant.roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.roleId)
      principalId: grant.principalId
      principalType: grant.principalType
    }
  }
]
