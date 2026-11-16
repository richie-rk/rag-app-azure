// The API App Service plan, its autoscale setting and the API web app with platform authentication
// (83-infrastructure.md sections 2.3 to 2.5, 2.22 and 4.5; ADRs 0004, 0007, 0017, 0023, 0110). The
// site is the only internet-facing endpoint: platform authentication and platform CORS in front of
// a custom container pulled through the network, and a deployment (SCM) site that takes no traffic.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig

@description('The running image\'s digest reference, so provisioning never changes the image.')
param image string

@description('DOCCHAT_RELEASE_SHA: the docchat-release-sha tag, which the postdeploy hook also writes.')
param releaseSha string

param identityResourceId string
param identityClientId string
param subnetId string
param appSettings object
param workspaceId string

param tenantId string
param apiAppId string
param webAppId string

@description('The test client\'s appId in dev and load; empty in prod, which has no test client.')
param testAppId string

param staticWebAppHostName string

var planName = 'asp-docchat-api-${environmentName}'
var siteName = 'app-docchat-api-${environmentName}-${resourceToken}'
var autoscaleName = 'autoscale-docchat-api-${environmentName}'

// A zone-redundant plan needs at least two instances, the platform's zone-redundant minimum.
var minimumInstances = config.api.zoneRedundant ? max(2, config.api.minInstances) : config.api.minInstances

// dev's B1 runs one fixed instance; prod and load scale between their bounds.
var autoscaleEnabled = config.api.maxInstances > config.api.minInstances

// Exact origins: the Static Web App's default host and each web custom domain, never a wildcard.
var allowedOrigins = concat(
  ['https://${staticWebAppHostName}'],
  map(config.webCustomDomains, domain => 'https://${domain}')
)

module plan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: planName
  params: {
    name: planName
    location: location
    tags: tags
    kind: 'linux'
    reserved: true
    skuName: config.api.sku
    skuCapacity: minimumInstances
    // The module's default of true for Premium SKUs is always overridden.
    zoneRedundant: config.api.zoneRedundant
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
    tags: union(tags, {
      'azd-service-name': 'api'
      // Declared here so a provision keeps the tag the postdeploy hook writes for release ordering.
      'docchat-release-sha': releaseSha
    })
    kind: 'app,linux,container'
    serverFarmResourceId: plan.outputs.resourceId
    managedIdentities: {
      userAssignedResourceIds: [identityResourceId]
    }
    httpsOnly: true
    clientAffinityEnabled: false
    clientAffinityProxyEnabled: false
    publicNetworkAccess: 'Enabled'
    virtualNetworkSubnetResourceId: subnetId
    // Route-all off: only private-endpoint traffic and image pulls go through the network.
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
      ipSecurityRestrictionsDefaultAction: 'Allow'
      ipSecurityRestrictions: []
      // The deployment site accepts no traffic: images deploy through config/web, never SCM.
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: []
      scmIpSecurityRestrictionsUseMain: false
      // Platform CORS takes precedence over app code, so the app runs no CORS code at all.
      cors: {
        allowedOrigins: allowedOrigins
        supportCredentials: false
      }
      // The image's own exec-form command runs, so Gunicorn receives the stop signal.
      appCommandLine: ''
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
    // AppServiceHTTPLogs, AppServiceConsoleLogs and AppServiceAuthenticationLogs stay off: they
    // would carry sign-in names, unscrubbed output or undocumented content (section 2.19).
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: workspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'AppServicePlatformLogs' }
          { category: 'AppServiceAuditLogs' }
          { category: 'AppServiceIPSecAuditLogs' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

resource apiSite 'Microsoft.Web/sites@2025-03-01' existing = {
  name: siteName
  dependsOn: [site]
}

// Layer 1 of the two-layer authentication (ADR 0017): a bearer-only Entra provider that answers
// 401 and never redirects, so it needs no client secret; the API validates every token again.
resource authSettings 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: apiSite
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
      // App Service health checks pass through platform authentication without an exclusion.
      excludedPaths: []
    }
    httpSettings: {
      requireHttps: true
      routes: {
        apiPrefix: '/.auth'
      }
      forwardProxy: {
        convention: 'NoProxy'
      }
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: apiAppId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        login: {
          disableWWWAuthenticate: false
          loginParameters: []
        }
        validation: {
          allowedAudiences: [apiAppId]
          defaultAuthorizationPolicy: {
            allowedApplications: filter([webAppId, testAppId], appId => !empty(appId))
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
      preserveUrlFragmentsForLogins: false
      allowedExternalRedirectUrls: []
    }
  }
}

resource autoscale 'Microsoft.Insights/autoscalesettings@2022-10-01' = if (autoscaleEnabled) {
  name: autoscaleName
  location: location
  tags: tags
  properties: {
    name: autoscaleName
    enabled: true
    targetResourceUri: plan.outputs.resourceId
    profiles: [
      {
        name: 'default'
        capacity: {
          minimum: string(minimumInstances)
          default: string(minimumInstances)
          maximum: string(config.api.maxInstances)
        }
        // Scale-in at 35% sits far enough below 70% that removing an instance can't push the
        // rest back over the scale-out threshold, which autoscale would abort as flapping.
        rules: [
          {
            metricTrigger: {
              metricName: 'CpuPercentage'
              metricNamespace: 'microsoft.web/serverfarms'
              metricResourceUri: plan.outputs.resourceId
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT5M'
              timeAggregation: 'Average'
              operator: 'GreaterThan'
              threshold: 70
            }
            scaleAction: {
              direction: 'Increase'
              type: 'ChangeCount'
              value: '1'
              cooldown: 'PT5M'
            }
          }
          {
            metricTrigger: {
              metricName: 'CpuPercentage'
              metricNamespace: 'microsoft.web/serverfarms'
              metricResourceUri: plan.outputs.resourceId
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT10M'
              timeAggregation: 'Average'
              operator: 'LessThan'
              threshold: 35
            }
            scaleAction: {
              direction: 'Decrease'
              type: 'ChangeCount'
              value: '1'
              cooldown: 'PT10M'
            }
          }
        ]
      }
    ]
  }
}

// The preview register's one entry (ADR 0009): only this version selects resource-specific tables.
#disable-next-line use-recent-api-versions
resource autoscaleDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (autoscaleEnabled) {
  scope: autoscale
  name: 'to-log-analytics'
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'AutoscaleScaleActionsLog'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output id string = site.outputs.resourceId
output name string = siteName
output planId string = plan.outputs.resourceId
// The default host only: no custom domain, certificate or host-name binding (section 2.22).
output baseUrl string = 'https://${siteName}.azurewebsites.net'
