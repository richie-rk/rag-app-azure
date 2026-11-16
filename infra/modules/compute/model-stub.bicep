// The model stub host of the load environment (83-infrastructure.md section 2.21; ADR 0112): a
// one-instance P1v3 plan and a container site that serves the stub's OpenAI-compatible routes on
// its private endpoint only. Both apps' DOCCHAT_OPENAI_ENDPOINT names its default host name, which
// the linked privatelink.azurewebsites.net zone resolves inside the network; the clients send their
// Entra token as everywhere and the stub ignores it. Nothing else of load is stubbed.

param resourceToken string
param location string
param tags object

@description('DOCCHAT_MODEL_STUB_IMAGE_CURRENT: docchat/model-stub@<digest> of the release.')
param image string

@description('id-docchat-stub-load, which holds AcrPull on the registry and nothing else.')
param identityResourceId string
param identityClientId string

@description('snet-stub-integration.')
param subnetId string

@description('The model stub\'s settings, composed by app-settings.bicep.')
param appSettings object

param workspaceId string

var planName = 'asp-docchat-stub-load'
var siteName = 'app-docchat-stub-load-${resourceToken}'

module plan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: planName
  params: {
    name: planName
    location: location
    tags: tags
    kind: 'linux'
    reserved: true
    skuName: 'P1v3'
    skuCapacity: 1
    // The module's default of true for Premium SKUs is always overridden: the stub exists only for
    // a load run and its scenarios don't measure it.
    zoneRedundant: false
    perSiteScaling: false
    elasticScaleEnabled: false
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
    tags: tags
    kind: 'app,linux,container'
    serverFarmResourceId: plan.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [identityResourceId]
    }
    httpsOnly: true
    clientAffinityEnabled: false
    clientAffinityProxyEnabled: false
    // Reached only through its private endpoint: an internet-reachable model endpoint would answer
    // anyone.
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
      alwaysOn: true
      http20Enabled: true
      minTlsVersion: '1.3'
      scmMinTlsVersion: '1.3'
      ftpsState: 'Disabled'
      remoteDebuggingEnabled: false
      use32BitWorkerProcess: false
      webSocketsEnabled: false
      healthCheckPath: '/healthz'
      // Public access is off, so the private endpoint is the site's only way in; a deny rule here
      // would add nothing but a risk of refusing that endpoint's traffic.
      ipSecurityRestrictionsDefaultAction: 'Allow'
      ipSecurityRestrictions: []
      // The deployment site accepts no traffic: the image comes from provisioning, never SCM.
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: []
      scmIpSecurityRestrictionsUseMain: false
      // The image's command expands DOCCHAT_STUB_WORKERS for Uvicorn's --workers.
      appCommandLine: ''
    }
    configs: [
      {
        name: 'appsettings'
        properties: appSettings
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
          { category: 'AppServicePlatformLogs' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

output name string = siteName
output id string = site.outputs.resourceId
output defaultHostName string = '${siteName}.azurewebsites.net'
