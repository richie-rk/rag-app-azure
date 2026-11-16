// The Static Web App (83-infrastructure.md section 2.2). No repository, build, linked backend,
// managed functions or custom authentication is declared, so nothing but the deployment token of
// deploy_web.py publishes content to it.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param tags object
param config environmentConfig

module staticSite 'br/public:avm/res/web/static-site:0.9.6' = {
  name: 'stapp-docchat-${environmentName}'
  params: {
    name: 'stapp-docchat-${environmentName}'
    // Static Web Apps serves content globally; the location places only the management resource.
    location: config.staticWebAppLocation
    tags: union(tags, { 'azd-service-name': 'web' })
    sku: config.web.sku
    stagingEnvironmentPolicy: 'Disabled'
    allowConfigFileUpdates: true
    publicNetworkAccess: 'Enabled'
    enterpriseGradeCdnStatus: 'Disabled'
    customDomains: config.webCustomDomains
    enableTelemetry: false
  }
}

output id string = staticSite.outputs.resourceId
output name string = staticSite.outputs.name
output defaultHostname string = staticSite.outputs.defaultHostname
