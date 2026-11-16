// Deny policy assignments at one resource group (83-infrastructure.md section 12.1). The bootstrap's
// policies.bicep defines the custom policies at subscription scope and needs a module to assign at a
// resource group (Bicep error BCP139), so this is that module and nothing more.
targetScope = 'resourceGroup'

@export()
@sealed()
@description('One deny policy assignment of 83-infrastructure.md sections 12.1 and 12.2.')
type policyAssignment = {
  @description('The assignment name, docchat-deny-<rule>.')
  name: string

  @description('The display name shown in the portal and in compliance results.')
  displayName: string

  @description('What the assignment denies and where it is specified.')
  description: string

  @description('A built-in definition (its GUID name, a tenant-level definition) or a custom one (its name, defined in this subscription by policies.bicep).')
  builtIn: bool

  @description('The definition\'s name: the built-in GUID, or docchat-deny-<rule> for a custom definition.')
  definitionName: string

  @description('The non-compliance message, naming the rule and its section.')
  nonComplianceMessage: string
}

@description('The assignments to make at this resource group.')
param assignments policyAssignment[] = []

resource policyAssignments 'Microsoft.Authorization/policyAssignments@2026-07-01' = [
  for assignment in assignments: {
    name: assignment.name
    properties: {
      displayName: assignment.displayName
      description: assignment.description
      // Compliance results and the portal name the bootstrap as the assignment's owner
      // (Azure.Policy.AssignmentAssignedBy, 83-infrastructure.md section 13.4).
      metadata: {
        assignedBy: 'DocChat bootstrap'
      }
      policyDefinitionId: assignment.builtIn
        ? tenantResourceId('Microsoft.Authorization/policyDefinitions', assignment.definitionName)
        : subscriptionResourceId('Microsoft.Authorization/policyDefinitions', assignment.definitionName)
      enforcementMode: 'Default'
      // Built-in definitions take the effect as a parameter; the two custom ones fix it in their
      // rule (deny and denyAction) and declare no parameter, so they get none.
      parameters: assignment.builtIn
        ? {
            effect: {
              value: 'Deny'
            }
          }
        : {}
      nonComplianceMessages: [
        {
          message: assignment.nonComplianceMessage
        }
      ]
    }
  }
]
