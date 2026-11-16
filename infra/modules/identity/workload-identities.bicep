// The workload identities of the environment (83-infrastructure.md section 6.1): the API's and
// the ingestion app's, and in load the model stub's, whose only role is AcrPull. Each app runs with
// its user-assigned identity only; no app has a system-assigned identity.

param environmentName string
param location string

@description('modelStub.enabled: load declares id-docchat-stub-load.')
param modelStubEnabled bool

@description('Tags shared by every environment resource; the component tag is added here.')
param baseTags object

module apiIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'id-docchat-api-${environmentName}'
  params: {
    name: 'id-docchat-api-${environmentName}'
    location: location
    tags: union(baseTags, { 'docchat-component': 'api' })
    enableTelemetry: false
  }
}

module ingestionIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'id-docchat-ingest-${environmentName}'
  params: {
    name: 'id-docchat-ingest-${environmentName}'
    location: location
    tags: union(baseTags, { 'docchat-component': 'ingestion' })
    enableTelemetry: false
  }
}

module stubIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = if (modelStubEnabled) {
  name: 'id-docchat-stub-load'
  params: {
    name: 'id-docchat-stub-load'
    location: location
    tags: union(baseTags, { 'docchat-component': 'ai' })
    enableTelemetry: false
  }
}

output apiName string = apiIdentity.outputs.name
output apiResourceId string = apiIdentity.outputs.resourceId
output apiClientId string = apiIdentity.outputs.clientId
output apiPrincipalId string = apiIdentity.outputs.principalId
output ingestionName string = ingestionIdentity.outputs.name
output ingestionResourceId string = ingestionIdentity.outputs.resourceId
output ingestionClientId string = ingestionIdentity.outputs.clientId
output ingestionPrincipalId string = ingestionIdentity.outputs.principalId
output stubResourceId string = modelStubEnabled ? stubIdentity!.outputs.resourceId : ''
output stubClientId string = modelStubEnabled ? stubIdentity!.outputs.clientId : ''
output stubPrincipalId string = modelStubEnabled ? stubIdentity!.outputs.principalId : ''
