// Role assignments inside one resource group: on the group itself or on one named resource in it.
// The bootstrap's assignments.bicep runs at subscription scope and needs a module to reach a
// resource group or a resource (Bicep error BCP139), so this is that module and nothing more.
targetScope = 'resourceGroup'

@export()
@description('The resource group itself, or the one registry, vault or workspace named by this module\'s parameters.')
type assignmentTarget = 'resourceGroup' | 'registry' | 'vault' | 'workspace'

@export()
@sealed()
@description('One role assignment of 83-infrastructure.md sections 6.8 and 6.9.')
type roleAssignment = {
  target: assignmentTarget

  @description('The principal (object) ID of the identity or group.')
  principalId: string

  @description('ServicePrincipal for a managed identity, Group for a group (section 6.3).')
  principalType: 'ServicePrincipal' | 'Group'

  @description('The role definition GUID.')
  roleDefinitionId: string

  @description('An Azure ABAC condition (version 2.0), for the conditioned Role Based Access Control Administrator assignments of section 6.11.')
  condition: string?
}

@description('The assignments to make in this resource group.')
param assignments roleAssignment[] = []

@description('The shared container registry, for assignments whose target is registry.')
param registryName string = ''

@description('The test-credential vault, for assignments whose target is vault.')
param vaultName string = ''

@description('The Log Analytics workspace, for assignments whose target is workspace.')
param workspaceName string = ''

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' existing = {
  name: vaultName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' existing = {
  name: workspaceName
}

@sealed()
type assignmentProperties = {
  principalId: string
  principalType: 'ServicePrincipal' | 'Group'
  roleDefinitionId: string
  condition: string?
  conditionVersion: '2.0'?
}

// Every assignment is named guid(<scope resource ID>, <principal ID>, <role definition ID>), so a
// redeploy finds the same assignment (section 6.3).
func properties(assignment roleAssignment) assignmentProperties => {
  principalId: assignment.principalId
  principalType: assignment.principalType
  roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', assignment.roleDefinitionId)
  condition: assignment.?condition
  conditionVersion: assignment.?condition == null ? null : '2.0'
}

resource groupAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in filter(assignments, a => a.target == 'resourceGroup'): {
    name: guid(resourceGroup().id, assignment.principalId, assignment.roleDefinitionId)
    properties: properties(assignment)
  }
]

resource registryAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in filter(assignments, a => a.target == 'registry'): {
    name: guid(registry.id, assignment.principalId, assignment.roleDefinitionId)
    scope: registry
    properties: properties(assignment)
  }
]

resource vaultAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in filter(assignments, a => a.target == 'vault'): {
    name: guid(vault.id, assignment.principalId, assignment.roleDefinitionId)
    scope: vault
    properties: properties(assignment)
  }
]

resource workspaceAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in filter(assignments, a => a.target == 'workspace'): {
    name: guid(workspace.id, assignment.principalId, assignment.roleDefinitionId)
    scope: workspace
    properties: properties(assignment)
  }
]
