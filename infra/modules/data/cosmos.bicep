// The Cosmos DB account, database docchat and its three containers on the GA API 2026-03-15
// (83-infrastructure.md section 2.10; 80-data-model.md section 3; ADR 0097). The AVM module
// isn't used because it deploys a preview API and sets serverless through a preview-only property.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig

@description('Operator allowlist; empty disables public network access.')
param operatorIpAllowlist string[]

var serverless = config.cosmos.serverless
var secondsPerDay = 86400

// Every path the queries of 80-data-model.md section 3.11 filter or sort on, and nothing else, so
// questions, answers and model input add no index cost to a write.
var conversationsIndexingPolicy = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: [
    { path: '/userId/?' }
    { path: '/sessionId/?' }
    { path: '/type/?' }
    { path: '/projectId/?' }
    { path: '/lastTurnAt/?' }
    { path: '/seq/?' }
    { path: '/status/?' }
    { path: '/endedAt/?' }
    { path: '/memoryExtraction/state/?' }
    { path: '/retrieval/kept/[]/documentId/?' }
  ]
  excludedPaths: [
    { path: '/*' }
    { path: '/"_etag"/?' }
  ]
  compositeIndexes: [
    [
      {
        path: '/lastTurnAt'
        order: 'ascending'
      }
      {
        path: '/sessionId'
        order: 'ascending'
      }
    ]
  ]
}

// memories and usage serve point reads and conditioned writes only.
var pointAccessIndexingPolicy = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: []
  excludedPaths: [
    { path: '/*' }
  ]
}

var containers = [
  {
    name: 'conversations'
    partitionKey: {
      paths: ['/userId', '/sessionId']
      kind: 'MultiHash'
      version: 2
    }
    defaultTtl: config.conversationRetentionDays == 0 ? -1 : config.conversationRetentionDays * secondsPerDay
    indexingPolicy: conversationsIndexingPolicy
    maxThroughput: serverless ? 0 : config.cosmos.maxThroughput!.conversations
  }
  {
    name: 'memories'
    partitionKey: {
      paths: ['/userId']
      kind: 'Hash'
      version: 2
    }
    defaultTtl: config.memoryRetentionDays == 0 ? -1 : config.memoryRetentionDays * secondsPerDay
    indexingPolicy: pointAccessIndexingPolicy
    maxThroughput: serverless ? 0 : config.cosmos.maxThroughput!.memories
  }
  {
    name: 'usage'
    partitionKey: {
      paths: ['/userId']
      kind: 'Hash'
      version: 2
    }
    // Only usage items that carry their own ttl expire.
    defaultTtl: -1
    indexingPolicy: pointAccessIndexingPolicy
    maxThroughput: serverless ? 0 : config.cosmos.maxThroughput!.usage
  }
]

resource account 'Microsoft.DocumentDB/databaseAccounts@2026-03-15' = {
  name: 'cosmos-docchat-${environmentName}-${resourceToken}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        // Derived from the environment name, never a parameter (ADR 0102).
        isZoneRedundant: environmentName != 'dev'
      }
    ]
    capabilities: serverless ? [{ name: 'EnableServerless' }] : []
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    // The only settable value; clients that offer TLS 1.3 negotiate 1.3 (ADR 0105).
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: empty(operatorIpAllowlist) ? 'Disabled' : 'Enabled'
    ipRules: [for address in operatorIpAllowlist: { ipAddressOrRange: address }]
    isVirtualNetworkFilterEnabled: false
    networkAclBypass: 'None'
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    enableFreeTier: false
    enableAnalyticalStorage: false
    enableBurstCapacity: false
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: config.cosmos.backupTier
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2026-03-15' = {
  parent: account
  name: 'docchat'
  properties: {
    resource: {
      id: 'docchat'
    }
  }
}

// Autoscale maxima apply when a container is created, so its physical partitions exist before
// the first load scenario; a serverless account takes no throughput options at all.
resource sqlContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2026-03-15' = [
  for container in containers: {
    parent: database
    name: container.name
    properties: union(
      {
        resource: {
          id: container.name
          partitionKey: container.partitionKey
          defaultTtl: container.defaultTtl
          indexingPolicy: container.indexingPolicy
        }
      },
      serverless
        ? {}
        : {
            options: {
              autoscaleSettings: {
                maxThroughput: container.maxThroughput
              }
            }
          }
    )
  }
]

output id string = account.id
output name string = account.name
output documentEndpoint string = account.properties.documentEndpoint
