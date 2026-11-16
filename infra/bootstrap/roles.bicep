// The twelve DocChat custom roles of 83-infrastructure.md section 6.4. Deployed only by the
// bootstrap's `roles` step: neither Contributor nor Role Based Access Control Administrator can
// create a role definition, so no pipeline identity can change these.
targetScope = 'subscription'

@description('Every subscription in use (bootstrap.config.json `subscriptions`); each role is assignable in each of them.')
param subscriptionIds string[] = [subscription().subscriptionId]

var assignableScopes = [for id in subscriptionIds: '/subscriptions/${id}']

// The bootstrap reads this variable from the compiled template and checks every action with
// `az provider operation show` before it deploys (section 11.2 step 4, `unknown_action`), so the
// action lists exist in one place only.
var roles = [
  {
    id: 'e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d'
    roleName: 'DocChat Search Index Manager'
    description: 'Creates, reads and deletes AI Search indexes; held by the ingestion identity on its search service.'
    actions: [
      'Microsoft.Search/searchServices/indexes/read'
      'Microsoft.Search/searchServices/indexes/write'
      'Microsoft.Search/searchServices/indexes/delete'
    ]
    dataActions: []
  }
  {
    id: 'a5f5ab92-08e9-42d6-b9f2-31ca2b30a345'
    roleName: 'DocChat Blob Tag Reader'
    description: 'Reads blob index tags (malware scan results); held by the ingestion identity on the documents container.'
    actions: []
    dataActions: [
      'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read'
    ]
  }
  {
    id: 'dca0c110-c46b-4a92-9466-128364b58b52'
    roleName: 'DocChat Template Validator'
    description: 'Validates and runs what-if on templates without deploying; held by id-docchat-pr.'
    actions: [
      'Microsoft.Resources/deployments/read'
      'Microsoft.Resources/deployments/validate/action'
      'Microsoft.Resources/deployments/whatIf/action'
      'Microsoft.Resources/deployments/operations/read'
      'Microsoft.Resources/deployments/operationstatuses/read'
    ]
    dataActions: []
  }
  {
    id: '2b1b37e4-43e4-4a34-8e89-05dacdf549cf'
    roleName: 'DocChat Defender Scanner Enabler'
    description: 'Reads and writes the Defender for Storage data scanner; held by each deploy identity at the subscription.'
    actions: [
      'Microsoft.Security/datascanners/read'
      'Microsoft.Security/datascanners/write'
    ]
    dataActions: []
  }
  {
    id: '1c6b44a0-732f-4fb9-b65e-520d77014b79'
    roleName: 'DocChat Registry Firewall Editor'
    description: 'Edits the shared registry, for the transient runner rule; held by id-docchat-deploy-dev only.'
    actions: [
      'Microsoft.ContainerRegistry/registries/read'
      'Microsoft.ContainerRegistry/registries/write'
    ]
    dataActions: []
  }
  {
    id: 'aa667e85-6da6-4ceb-838f-505789b07fc5'
    roleName: 'DocChat Registry Endpoint Approver'
    description: 'Approves private endpoint connections to the shared registry; held by each deploy identity.'
    actions: [
      'Microsoft.ContainerRegistry/registries/read'
      'Microsoft.ContainerRegistry/registries/privateEndpointConnectionsApproval/action'
    ]
    dataActions: []
  }
  {
    id: 'fefff72b-0dcb-4495-8fc3-c93f2aeef356'
    roleName: 'DocChat Shared Deployment Operator'
    description: 'Runs deployments in rg-docchat-shared without any resource write; held by each deploy identity.'
    actions: [
      'Microsoft.Resources/subscriptions/resourceGroups/read'
      'Microsoft.Resources/deployments/read'
      'Microsoft.Resources/deployments/write'
      'Microsoft.Resources/deployments/validate/action'
      'Microsoft.Resources/deployments/whatIf/action'
      'Microsoft.Resources/deployments/cancel/action'
      'Microsoft.Resources/deployments/operations/read'
      'Microsoft.Resources/deployments/operationstatuses/read'
    ]
    dataActions: []
  }
  {
    id: '4cd80105-6b57-4baa-b1dd-ef94ca94278e'
    roleName: 'DocChat Test Secret Writer'
    description: 'Sets test-user passwords and reads their metadata, never a value; held by sg-docchat-operators on the test-credential vault.'
    actions: []
    dataActions: [
      'Microsoft.KeyVault/vaults/secrets/setSecret/action'
      'Microsoft.KeyVault/vaults/secrets/readMetadata/action'
    ]
  }
  {
    id: '28936cba-1b39-4715-987a-4fc5adcdc5ab'
    roleName: 'DocChat Test Vault Firewall Editor'
    description: 'Edits the test-credential vault, for its transient runner rule; held by id-docchat-citest on that vault only, while ciPrivateNetworking is false.'
    actions: [
      'Microsoft.KeyVault/vaults/read'
      'Microsoft.KeyVault/vaults/write'
    ]
    dataActions: []
  }
  {
    id: '63c4387a-e306-4ef7-9bfa-2e921c67f02b'
    roleName: 'DocChat Preflight Reader'
    description: 'Reads the provider, region, model, quota and capacity facts the preflight checks; held by each deploy identity and id-docchat-pr.'
    actions: [
      'Microsoft.CognitiveServices/locations/models/read'
      'Microsoft.CognitiveServices/locations/modelCapacities/read'
      'Microsoft.CognitiveServices/modelCapacities/read'
      'Microsoft.CognitiveServices/locations/usages/read'
      'Microsoft.CognitiveServices/locations/checkSkuAvailability/action'
      'Microsoft.CognitiveServices/accounts/read'
      'Microsoft.CognitiveServices/accounts/deployments/read'
      'Microsoft.ContainerInstance/locations/capabilities/read'
      'Microsoft.ContainerInstance/locations/usages/read'
      'Microsoft.Web/geoRegions/read'
      'Microsoft.Resources/subscriptions/read'
      'Microsoft.Resources/subscriptions/locations/read'
      'Microsoft.Resources/subscriptions/providers/read'
    ]
    dataActions: []
  }
  {
    id: 'f6b96867-d30e-4380-9a16-fbac2370fcd8'
    roleName: 'DocChat Cosmos Data Access Granter'
    description: 'Creates and deletes per-principal Cosmos DB data-plane assignments; held by the just-in-time approvers only when cosmosGroupDataAccess is false.'
    actions: [
      'Microsoft.DocumentDB/databaseAccounts/read'
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions/read'
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/read'
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write'
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/delete'
    ]
    dataActions: []
  }
  {
    id: 'cc43bd13-3731-4abc-9386-d2a7c3e6fcb4'
    roleName: 'DocChat Load Test Operator'
    description: 'Runs the load generator container groups and changes the API autoscale profile; held by id-docchat-citest at rg-docchat-load.'
    actions: [
      'Microsoft.Resources/deployments/write'
      'Microsoft.Resources/deployments/validate/action'
      'Microsoft.ContainerInstance/containerGroups/write'
      'Microsoft.ContainerInstance/containerGroups/delete'
      'Microsoft.Insights/autoscalesettings/write'
    ]
    dataActions: []
  }
]

resource definitions 'Microsoft.Authorization/roleDefinitions@2022-04-01' = [
  for role in roles: {
    name: role.id
    properties: {
      roleName: role.roleName
      description: role.description
      type: 'CustomRole'
      permissions: [
        {
          actions: role.actions
          notActions: []
          dataActions: role.dataActions
          notDataActions: []
        }
      ]
      assignableScopes: assignableScopes
    }
  }
]
