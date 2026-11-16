// The ingestion Elastic Premium plan and function app (83-infrastructure.md sections 2.6 and 2.7;
// ADRs 0005 and 0103): a Linux custom container with no inbound traffic of any kind, identity-based
// host storage and no Azure Files content share.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig

@description('The running image\'s digest reference, so provisioning never changes the image.')
param image string

param identityResourceId string
param identityClientId string
param subnetId string
param appSettings object
param workspaceId string

var planName = 'asp-docchat-ingest-${environmentName}'
var siteName = 'func-docchat-ingest-${environmentName}-${resourceToken}'

module plan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: planName
  params: {
    name: planName
    location: location
    tags: tags
    kind: 'elastic'
    reserved: true
    skuName: config.ingestion.sku
    skuCapacity: 1
    maximumElasticWorkerCount: config.ingestion.maxBurst
    elasticScaleEnabled: true
    perSiteScaling: false
    // Ingestion is queued, checkpointed work that a zone outage delays without losing (ADR 0102);
    // the module's default of true for EP SKUs is always overridden.
    zoneRedundant: false
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

module site 'br/public:avm/res/web/site:0.24.0' = {
  name: siteName
  params: {
    name: siteName
    location: location
    tags: union(tags, { 'azd-service-name': 'ingestion' })
    kind: 'functionapp,linux,container'
    serverFarmResourceId: plan.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [identityResourceId]
    }
    httpsOnly: true
    clientAffinityEnabled: false
    // The Durable client reaches the host on its loopback listener; nothing comes in.
    publicNetworkAccess: 'Disabled'
    virtualNetworkSubnetResourceId: subnetId
    outboundVnetRouting: {
      allTraffic: false
      applicationTraffic: false
      imagePullTraffic: true
      contentShareTraffic: false
      backupRestoreTraffic: false
    }
    siteConfig: {
      linuxFxVersion: 'DOCKER|${image}'
      acrUseManagedIdentityCreds: true
      acrUserManagedIdentityID: identityClientId
      // The platform scales the app from its private queue.
      functionsRuntimeScaleMonitoringEnabled: true
      minimumElasticInstanceCount: config.ingestion.alwaysReady
      preWarmedInstanceCount: 1
      functionAppScaleLimit: config.ingestion.maxBurst
      minTlsVersion: '1.3'
      scmMinTlsVersion: '1.3'
      ftpsState: 'Disabled'
      remoteDebuggingEnabled: false
      http20Enabled: false
      ipSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
    }
    configs: [
      {
        name: 'appsettings'
        properties: appSettings
        // The set is exact: a setting this template doesn't declare is removed.
        retainCurrentAppSettings: false
      }
    ]
    basicPublishingCredentialsPolicies: [
      {
        name: 'ftp'
        allow: false
      }
      {
        name: 'scm'
        allow: false
      }
    ]
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'FunctionAppLogs' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

output id string = site.outputs.resourceId
output name string = siteName
output planId string = plan.outputs.resourceId
