// Deny policies of 83-infrastructure.md sections 12.1 and 12.2 (ADR 0116), deployed only by the
// bootstrap's `policies` step, once per subscription in use. A policy assignment is a
// Microsoft.Authorization object, so no pipeline identity can change or delete one.
targetScope = 'subscription'

import { policyAssignment } from 'modules/policy-assignments.bicep'

@sealed()
type builtInPolicy = {
  name: string
  definition: string
  displayName: string
  scopes: ('environment' | 'prod' | 'shared')[]
}

@description('The subscription of rg-docchat-identity and rg-docchat-shared (the subscription of `dev`).')
param homeSubscriptionId string = subscription().subscriptionId

@description('The environments whose resource group lives in this subscription.')
@allowed([
  'dev'
  'prod'
  'load'
])
param environmentNames string[] = []

var isHomeSubscription = toLower(subscription().subscriptionId) == toLower(homeSubscriptionId)

// The bootstrap reads this variable from the compiled template and confirms every definition with
// `az policy definition show` before it deploys (section 11.2 step 10, `policy_definition_missing`),
// so the definitions are listed in one place only. `scopes`: `environment` is each environment
// group, `prod` is rg-docchat-prod alone, `shared` is rg-docchat-shared.
var builtInPolicies builtInPolicy[] = [
  {
    name: 'docchat-deny-storage-shared-key'
    definition: '8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54'
    displayName: 'Storage accounts should prevent shared key access'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-storage-public-access'
    definition: '4fa4b6c0-31ca-4c0d-b10d-24b96f62a751'
    displayName: 'Storage account public access should be disallowed'
    scopes: ['environment', 'shared']
  }
  {
    name: 'docchat-deny-cosmos-local-auth'
    definition: '5450f5bd-9c72-4390-a9c4-a7aba4edfdd2'
    displayName: 'Cosmos DB database accounts should have local authentication methods disabled'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-cosmos-azure-datacenters'
    definition: '12339a85-a25c-4f17-9f82-4766f13f5c4c'
    displayName: 'Azure Cosmos DB accounts should not allow traffic from all Azure data centers'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-ai-key-access'
    definition: '71ef260a-8f18-47b7-abcb-62d0673d94dc'
    displayName: 'Azure AI Services resources should have key access disabled'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-search-local-auth'
    definition: '6300012e-e9a4-4649-b41f-a85f5c43be91'
    displayName: 'Azure AI Search services should have local authentication methods disabled'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-keyvault-access-policies'
    definition: '12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5'
    displayName: 'Azure Key Vault should use RBAC permission model'
    scopes: ['environment', 'shared']
  }
  {
    name: 'docchat-deny-keyvault-purge'
    definition: '0b60c0b2-2dc2-4e1c-b5c9-abbed971de53'
    displayName: 'Key vaults should have deletion protection enabled'
    scopes: ['prod', 'shared']
  }
  {
    name: 'docchat-deny-sql-local-auth'
    definition: 'b3a22bc9-66de-45fb-98fa-00f5df42f41a'
    displayName: 'Azure SQL Database should have Microsoft Entra-only authentication enabled'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-sql-local-auth-create'
    definition: 'abda6d70-9778-44e7-84a8-06713e6db027'
    displayName: 'Azure SQL Database should have Microsoft Entra-only authentication enabled during creation'
    scopes: ['environment']
  }
  {
    name: 'docchat-deny-acr-admin'
    definition: 'dc921057-6b28-4fbe-9b83-f7bec05db6c2'
    displayName: 'Container registries should have local admin account disabled.'
    scopes: ['shared']
  }
  {
    name: 'docchat-deny-acr-anonymous'
    definition: '9f2dea28-e834-476c-99c5-3507b4728395'
    displayName: 'Container registries should have anonymous authentication disabled.'
    scopes: ['shared']
  }
  {
    name: 'docchat-deny-acr-tokens'
    definition: 'ff05e24e-195c-447e-b322-5e90c9f9f366'
    displayName: 'Container registries should have repository scoped access token disabled.'
    scopes: ['shared']
  }
  {
    name: 'docchat-deny-acr-open-network'
    definition: 'd0793b48-0edc-4296-a390-4c75d1bdfd71'
    displayName: 'Container registries should not allow unrestricted network access'
    scopes: ['shared']
  }
]

// Stops anyone with Contributor in an environment group from adding a credential to an app
// identity and signing in as it; the pipeline identities in rg-docchat-identity keep theirs.
resource federatedCredentialsDefinition 'Microsoft.Authorization/policyDefinitions@2026-07-01' = {
  name: 'docchat-deny-federated-credentials'
  properties: {
    policyType: 'Custom'
    mode: 'All'
    displayName: 'DocChat: deny federated credentials on app identities'
    description: 'Denies every federated identity credential in the environment resource groups (83-infrastructure.md section 12.2).'
    metadata: {
      category: 'DocChat'
    }
    policyRule: {
      if: {
        field: 'type'
        equals: 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials'
      }
      then: {
        effect: 'deny'
      }
    }
  }
}

// denyAction blocks only DELETE, so the stores' children (firewall rules, data-plane role
// assignments, private endpoint connections) stay deletable; `Indexed` because cascadeBehaviors
// requires it.
resource storeDeletionDefinition 'Microsoft.Authorization/policyDefinitions@2026-07-01' = {
  name: 'docchat-deny-store-deletion'
  properties: {
    policyType: 'Custom'
    mode: 'Indexed'
    displayName: 'DocChat: deny deletion of the prod SQL server, Cosmos DB account and resource group'
    description: 'Denies DELETE of the SQL server and the Cosmos DB account, and of their resource group (83-infrastructure.md sections 12.2 and 12.3).'
    metadata: {
      category: 'DocChat'
    }
    policyRule: {
      if: {
        field: 'type'
        in: [
          'Microsoft.Sql/servers'
          'Microsoft.DocumentDB/databaseAccounts'
        ]
      }
      then: {
        effect: 'denyAction'
        details: {
          actionNames: ['delete']
          cascadeBehaviors: {
            resourceGroup: 'deny'
          }
        }
      }
    }
  }
}

func builtInAssignment(policy builtInPolicy) policyAssignment => {
  name: policy.name
  displayName: policy.displayName
  description: 'Denies, in this resource group, every write that fails the built-in policy "${policy.displayName}" (83-infrastructure.md section 12.1).'
  builtIn: true
  definitionName: policy.definition
  nonComplianceMessage: '${policy.name}: ${policy.displayName}. Assigned by the DocChat bootstrap (83-infrastructure.md section 12.1).'
}

var federatedCredentialsAssignment policyAssignment = {
  name: 'docchat-deny-federated-credentials'
  displayName: 'DocChat: deny federated credentials on app identities'
  description: 'Denies every federated identity credential on the app identities of this resource group (83-infrastructure.md sections 12.1 and 12.2).'
  builtIn: false
  definitionName: federatedCredentialsDefinition.name
  nonComplianceMessage: 'docchat-deny-federated-credentials: no federated credential may be added to an app identity. Assigned by the DocChat bootstrap (83-infrastructure.md section 12.1).'
}

var storeDeletionAssignment policyAssignment = {
  name: 'docchat-deny-store-deletion'
  displayName: 'DocChat: deny deletion of the prod stores'
  description: 'Denies DELETE of the prod SQL server, the prod Cosmos DB account and this resource group (83-infrastructure.md sections 12.2 and 12.3).'
  builtIn: false
  definitionName: storeDeletionDefinition.name
  nonComplianceMessage: 'docchat-deny-store-deletion: the prod SQL server, Cosmos DB account and resource group can\'t be deleted. Assigned by the DocChat bootstrap (83-infrastructure.md section 12.3).'
}

module environmentPolicies 'modules/policy-assignments.bicep' = [
  for environmentName in environmentNames: {
    name: 'docchat-bootstrap-policies-${environmentName}'
    scope: resourceGroup('rg-docchat-${environmentName}')
    params: {
      assignments: concat(
        map(
          filter(
            builtInPolicies,
            policy => contains(policy.scopes, 'environment') || (environmentName == 'prod' && contains(policy.scopes, 'prod'))
          ),
          policy => builtInAssignment(policy)
        ),
        [federatedCredentialsAssignment],
        environmentName == 'prod' ? [storeDeletionAssignment] : []
      )
    }
  }
]

module sharedPolicies 'modules/policy-assignments.bicep' = if (isHomeSubscription) {
  name: 'docchat-bootstrap-policies-shared'
  scope: resourceGroup('rg-docchat-shared')
  params: {
    assignments: map(filter(builtInPolicies, policy => contains(policy.scopes, 'shared')), policy => builtInAssignment(policy))
  }
}
