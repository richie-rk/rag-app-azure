// Cosmos DB data-plane role assignments (83-infrastructure.md section 6.10; 13-conversations-and-
// memory.md section 10.2). Cosmos DB's own role system, not Azure RBAC: no role-assignment
// condition can restrict these writes, so docchat-alert-cosmos-data-access watches every write
// except a re-write of the assignments declared here, whose IDs are an output.

param accountName string
param apiPrincipalId string
param conversationReadersGroupId string
param userDataOperatorsGroupId string

@description('cosmosGroupDataAccess: the outcome of spike SP-7; false removes both group assignments.')
param groupDataAccess bool

var dataReaderRoleId = '00000000-0000-0000-0000-000000000001'
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2026-03-15' existing = {
  name: accountName
}

var databaseScope = '${account.id}/dbs/docchat'
var conversationsScope = '${account.id}/dbs/docchat/colls/conversations'

var assignments = concat(
  [
    {
      principalId: apiPrincipalId
      roleDefinitionId: dataContributorRoleId
      scope: databaseScope
    }
  ],
  groupDataAccess
    ? [
        {
          principalId: conversationReadersGroupId
          roleDefinitionId: dataReaderRoleId
          scope: conversationsScope
        }
        {
          principalId: userDataOperatorsGroupId
          roleDefinitionId: dataContributorRoleId
          scope: databaseScope
        }
      ]
    : []
)

resource sqlRoleAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2026-03-15' = [
  for assignment in assignments: {
    parent: account
    name: guid(account.id, assignment.principalId, assignment.roleDefinitionId, assignment.scope)
    properties: {
      roleDefinitionId: '${account.id}/sqlRoleDefinitions/${assignment.roleDefinitionId}'
      principalId: assignment.principalId
      scope: assignment.scope
    }
  }
]

output assignmentIds string[] = [for (assignment, index) in assignments: sqlRoleAssignments[index].id]
