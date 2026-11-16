// AcrPull for the environment's app identities on the shared registry, deployed into
// rg-docchat-shared (83-infrastructure.md sections 2.16 and 6.8). The deploy identity's right
// there is conditioned to AcrPull for service principals only.

param registryName string

@description('Principal IDs of the identities that pull images: the API and ingestion identities.')
param principalIds string[]

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

resource acrPullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in principalIds: {
    scope: registry
    name: guid(registry.id, principalId, acrPullRoleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]
