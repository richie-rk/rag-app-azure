// Every role assignment the bootstrap owns (83-infrastructure.md sections 6.3, 6.8, 6.9, 6.11 and
// 11.2 step 9): the pipeline identities' rights, with the initial role-assignment conditions, and
// the group assignments in rg-docchat-shared, rg-docchat-identity, the environment groups and at
// the subscription. Deployed only by the bootstrap's `assignments` step, once per subscription in
// use; every name is guid(<scope resource ID>, <principal ID>, <role definition ID>), so a re-run
// finds each assignment in place.
targetScope = 'subscription'

import { assignmentTarget, roleAssignment } from 'modules/role-assignments.bicep'

@sealed()
@description('Principal (object) IDs of the five pipeline identities in rg-docchat-identity.')
type pipelinePrincipals = {
  deployDev: string
  deployProd: string
  deployLoad: string
  pr: string
  citest: string
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

@description('Principal IDs of the pipeline identities, read by the bootstrap with az identity show.')
param pipelinePrincipalIds pipelinePrincipals = {
  deployDev: '00000000-0000-0000-0000-000000000000'
  deployProd: '00000000-0000-0000-0000-000000000000'
  deployLoad: '00000000-0000-0000-0000-000000000000'
  pr: '00000000-0000-0000-0000-000000000000'
  citest: '00000000-0000-0000-0000-000000000000'
}

@description('Object ID of sg-docchat-operators.')
param operatorsGroupId string = '00000000-0000-0000-0000-000000000000'

@description('Object ID of sg-docchat-dev-config-operators.')
param devConfigOperatorsGroupId string = '00000000-0000-0000-0000-000000000000'

@description('bootstrap.config.json ciPrivateNetworking: while false, id-docchat-citest edits the test vault\'s runner rule (section 6.4).')
param ciPrivateNetworking bool = false

@description('Whether log-docchat-dev exists, read by the bootstrap. dev\'s first provision creates it after the bootstrap\'s first run, so the step is run again then (section 11.5 step 2).')
param devWorkspaceExists bool = false

var isHomeSubscription = toLower(subscription().subscriptionId) == toLower(homeSubscriptionId)

// The same token shared.bicep derives in the home subscription (section 1.4), so the registry and
// the test vault are found by name.
var sharedToken = take(uniqueString('/subscriptions/${toLower(homeSubscriptionId)}', 'shared'), 6)

var roleIds = {
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  rbacAdministrator: 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  templateValidator: 'dca0c110-c46b-4a92-9466-128364b58b52'
  defenderScannerEnabler: '2b1b37e4-43e4-4a34-8e89-05dacdf549cf'
  registryFirewallEditor: '1c6b44a0-732f-4fb9-b65e-520d77014b79'
  registryEndpointApprover: 'aa667e85-6da6-4ceb-838f-505789b07fc5'
  sharedDeploymentOperator: 'fefff72b-0dcb-4495-8fc3-c93f2aeef356'
  testSecretWriter: '4cd80105-6b57-4baa-b1dd-ef94ca94278e'
  testVaultFirewallEditor: '28936cba-1b39-4715-987a-4fc5adcdc5ab'
  preflightReader: '63c4387a-e306-4ef7-9bfa-2e921c67f02b'
  loadTestOperator: 'cc43bd13-3731-4abc-9386-d2a7c3e6fcb4'
}

// <R> of section 6.11: every role main.bicep assigns at or under an environment group.
var environmentAssignableRoles = [
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
  '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Storage Blob Data Owner
  '974c5e8b-45b9-4653-ba55-5f855dd0fb88' // Storage Queue Data Contributor
  '19e7f393-937e-4f77-808e-94535e297925' // Storage Queue Data Reader
  '8a0f0c08-91a1-4084-bc3d-661d67233fed' // Storage Queue Data Message Processor
  'c6a89b2d-59bc-44d0-9896-0f6e12d7b80a' // Storage Queue Data Message Sender
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3' // Storage Table Data Contributor
  '1407120a-92aa-4202-b7e9-c0e197c71c8f' // Search Index Data Reader
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User
  '3913510d-42f4-4e42-8a64-420c390055eb' // Monitoring Metrics Publisher
  '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User
  'acdd72a7-3385-48ef-bd42-f606fba81ae7' // Reader
  '73c42c96-874c-492b-b04d-ab87d138a893' // Log Analytics Reader
  '1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40' // Defender for Storage Data Scanner
  'e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d' // DocChat Search Index Manager
  'a5f5ab92-08e9-42d6-b9f2-31ca2b30a345' // DocChat Blob Tag Reader
]

// One clause of the initial condition: a write (or delete) passes only for a role in the set and a
// principal of the listed types. tighten_conditions.py later replaces both clauses with
// role-and-principal pairs.
func conditionClause(action string, attribute string, roles string[], principalTypes string[]) string =>
  join(
    [
      ' ('
      '  !(ActionMatches{\'Microsoft.Authorization/roleAssignments/${action}\'})'
      ' )'
      ' OR'
      ' ('
      '  ${attribute}[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${join(roles, ', ')}}'
      '  AND'
      '  ${attribute}[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {${join(map(principalTypes, principalType => '\'${principalType}\''), ', ')}}'
      ' )'
    ],
    '\n'
  )

func initialCondition(roles string[], principalTypes string[]) string =>
  join(
    [
      '('
      conditionClause('write', '@Request', roles, principalTypes)
      ')'
      'AND'
      '('
      conditionClause('delete', '@Resource', roles, principalTypes)
      ')'
    ],
    '\n'
  )

var resourceGroupCondition = initialCondition(environmentAssignableRoles, ['ServicePrincipal', 'Group'])

var registryCondition = initialCondition([roleIds.acrPull], ['ServicePrincipal'])

var deployPrincipals = {
  dev: pipelinePrincipalIds.deployDev
  prod: pipelinePrincipalIds.deployProd
  load: pipelinePrincipalIds.deployLoad
}

func identityAssignment(target assignmentTarget, principalId string, roleDefinitionId string) roleAssignment => {
  target: target
  principalId: principalId
  principalType: 'ServicePrincipal'
  roleDefinitionId: roleDefinitionId
}

func groupAssignment(target assignmentTarget, principalId string, roleDefinitionId string) roleAssignment => {
  target: target
  principalId: principalId
  principalType: 'Group'
  roleDefinitionId: roleDefinitionId
}

// rg-docchat-dev: the pull-request identity's what-if rights, the dev config operators' rights for
// `chat model-check` and the live contract suites, and the CI test identity's Log Analytics Reader
// on the dev workspace alone, for the deployed suites' health, memory and trace readings (sections
// 6.3, 6.8 and 6.9). The workspace is main.bicep's, so that assignment waits until it exists.
var devWorkspaceName = 'log-docchat-dev'

var devGroupAssignments roleAssignment[] = concat(
  [
    identityAssignment('resourceGroup', pipelinePrincipalIds.pr, roleIds.reader)
    identityAssignment('resourceGroup', pipelinePrincipalIds.pr, roleIds.templateValidator)
    groupAssignment('resourceGroup', devConfigOperatorsGroupId, roleIds.cognitiveServicesUser)
    groupAssignment('resourceGroup', devConfigOperatorsGroupId, roleIds.searchServiceContributor)
    groupAssignment('resourceGroup', devConfigOperatorsGroupId, roleIds.searchIndexDataContributor)
  ],
  devWorkspaceExists ? [identityAssignment('workspace', pipelinePrincipalIds.citest, roleIds.logAnalyticsReader)] : []
)

// rg-docchat-load: the CI test identity's load-run rights, and the operators' Reader, named as
// main.bicep names its own `load` declaration of it, so a provision finds it in place.
var loadGroupAssignments roleAssignment[] = [
  identityAssignment('resourceGroup', pipelinePrincipalIds.citest, roleIds.logAnalyticsReader)
  identityAssignment('resourceGroup', pipelinePrincipalIds.citest, roleIds.loadTestOperator)
  groupAssignment('resourceGroup', operatorsGroupId, roleIds.reader)
]

module environmentAssignments 'modules/role-assignments.bicep' = [
  for environmentName in environmentNames: {
    name: 'docchat-bootstrap-assignments-${environmentName}'
    scope: resourceGroup('rg-docchat-${environmentName}')
    params: {
      workspaceName: environmentName == 'dev' ? devWorkspaceName : ''
      assignments: concat(
        [
          identityAssignment('resourceGroup', deployPrincipals[environmentName], roleIds.contributor)
          {
            ...identityAssignment('resourceGroup', deployPrincipals[environmentName], roleIds.rbacAdministrator)
            condition: resourceGroupCondition
          }
        ],
        environmentName == 'dev' ? devGroupAssignments : [],
        environmentName == 'load' ? loadGroupAssignments : []
      )
    }
  }
]

var deployIdentitySharedAssignments = flatten(map(
  [pipelinePrincipalIds.deployDev, pipelinePrincipalIds.deployProd, pipelinePrincipalIds.deployLoad],
  principalId => [
    identityAssignment('resourceGroup', principalId, roleIds.sharedDeploymentOperator)
    {
      ...identityAssignment('registry', principalId, roleIds.rbacAdministrator)
      condition: registryCondition
    }
    identityAssignment('registry', principalId, roleIds.registryEndpointApprover)
  ]
))

module sharedAssignments 'modules/role-assignments.bicep' = if (isHomeSubscription) {
  name: 'docchat-bootstrap-assignments-shared'
  scope: resourceGroup('rg-docchat-shared')
  params: {
    registryName: 'crdocchat${sharedToken}'
    vaultName: 'kv-docchat-tst-${sharedToken}'
    assignments: concat(
      deployIdentitySharedAssignments,
      [
        identityAssignment('registry', pipelinePrincipalIds.deployDev, roleIds.acrPush)
        identityAssignment('registry', pipelinePrincipalIds.deployDev, roleIds.registryFirewallEditor)
        identityAssignment('resourceGroup', pipelinePrincipalIds.pr, roleIds.reader)
        identityAssignment('resourceGroup', pipelinePrincipalIds.pr, roleIds.templateValidator)
        identityAssignment('vault', pipelinePrincipalIds.citest, roleIds.keyVaultSecretsUser)
        groupAssignment('resourceGroup', operatorsGroupId, roleIds.reader)
        groupAssignment('vault', operatorsGroupId, roleIds.testSecretWriter)
      ],
      // Under the private-networking option the vault's CI readers reach it through its private
      // endpoint and nobody edits its network rules; the bootstrap removes an earlier assignment.
      ciPrivateNetworking ? [] : [identityAssignment('vault', pipelinePrincipalIds.citest, roleIds.testVaultFirewallEditor)]
    )
  }
}

module identityGroupAssignments 'modules/role-assignments.bicep' = if (isHomeSubscription) {
  name: 'docchat-bootstrap-assignments-identity'
  scope: resourceGroup('rg-docchat-identity')
  params: {
    workspaceName: 'log-docchat-security'
    assignments: [
      groupAssignment('workspace', operatorsGroupId, roleIds.logAnalyticsReader)
    ]
  }
}

// At this subscription: the preflight's reads for every deploy identity and the pull-request
// identity in every subscription in use, and Defender enablement for the deploy identity of each
// environment that lives here.
var subscriptionAssignments = concat(
  map(
    [
      pipelinePrincipalIds.deployDev
      pipelinePrincipalIds.deployProd
      pipelinePrincipalIds.deployLoad
      pipelinePrincipalIds.pr
    ],
    principalId => {
      principalId: principalId
      roleDefinitionId: roleIds.preflightReader
    }
  ),
  map(environmentNames, environmentName => {
    principalId: deployPrincipals[environmentName]
    roleDefinitionId: roleIds.defenderScannerEnabler
  })
)

resource subscriptionRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in subscriptionAssignments: {
    name: guid(subscription().id, assignment.principalId, assignment.roleDefinitionId)
    properties: {
      principalId: assignment.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', assignment.roleDefinitionId)
    }
  }
]
