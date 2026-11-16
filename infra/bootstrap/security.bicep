// The security alerts A44 to A54 (83-infrastructure.md sections 12.5 and 12.6; 85-observability.md
// section 6.6), deployed only by the bootstrap's `security` step into rg-docchat-identity, where no
// pipeline identity holds any role, so the identities they watch can't change or delete them
// (ADR 0115). Every alert routes to ag-docchat-security.
targetScope = 'resourceGroup'

type deployIdentityName = 'id-docchat-deploy-dev' | 'id-docchat-deploy-prod' | 'id-docchat-deploy-load'

@sealed()
@description('Client and principal IDs of one deploy identity.')
type deployIdentity = {
  name: deployIdentityName
  clientId: string
  principalId: string
}

@sealed()
type activityLogAlert = {
  name: string
  description: string
  operations: string[]
}

@sealed()
type logAlert = {
  name: string
  description: string
  severity: int
  query: string
  @description('A look-back longer than the 30-minute window, for the one rule that needs events older than the window.')
  queryTimeRange: string?
}

@description('The region of rg-docchat-identity and log-docchat-security.')
param location string = resourceGroup().location

@description('Receivers of ag-docchat-security (bootstrap.config.json securityAlertEmails).')
param securityAlertEmails string[] = []

@description('Every subscription in use; the Activity Log alerts watch each of them.')
param subscriptionIds string[] = [subscription().subscriptionId]

@description('Client and principal IDs of the three deploy identities.')
param deployIdentityIds deployIdentity[] = []

@description('Principals a role assignment may name without raising docchat-alert-role-assignments (bootstrap.config.json).')
param allowedRoleAssigneePrincipalIds string[] = []

@description('Resource IDs of the template-declared Cosmos DB data-plane assignments (bootstrap.config.json).')
param templateCosmosAssignmentIds string[] = []

@description('Object IDs of the three just-in-time groups of each environment.')
param jitGroupIds string[] = []

resource securityWorkspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' existing = {
  name: 'log-docchat-security'
}

// Also declared by identity.bicep; declared here too because securityAlertEmails is this
// template's parameter (section 12.4), so a re-run of the `security` step applies changed receivers.
resource securityActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-docchat-security'
  location: 'Global'
  tags: {
    'docchat-env': 'shared'
    'docchat-component': 'monitoring'
  }
  properties: {
    groupShortName: 'docchat-sec'
    enabled: true
    emailReceivers: [
      for (address, index) in securityAlertEmails: {
        name: 'security-mailbox-${index + 1}'
        emailAddress: address
        useCommonAlertSchema: true
      }
    ]
  }
}

var tags = {
  'docchat-env': 'shared'
  'docchat-component': 'monitoring'
}

var activityLogAlerts activityLogAlert[] = [
  {
    name: 'docchat-alert-federated-credentials'
    description: 'A44: a federated identity credential was written or deleted. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    operations: [
      'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/write'
      'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/delete'
    ]
  }
  {
    name: 'docchat-alert-key-access'
    description: 'A45: a key, connection string or registry credential was listed or regenerated. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    operations: [
      'Microsoft.Storage/storageAccounts/listKeys/action'
      'Microsoft.Storage/storageAccounts/regenerateKey/action'
      'Microsoft.DocumentDB/databaseAccounts/listKeys/action'
      'Microsoft.DocumentDB/databaseAccounts/listConnectionStrings/action'
      'Microsoft.CognitiveServices/accounts/listKeys/action'
      'Microsoft.Search/searchServices/listAdminKeys/action'
      'Microsoft.Search/searchServices/listQueryKeys/action'
      'Microsoft.ContainerRegistry/registries/listCredentials/action'
    ]
  }
  {
    name: 'docchat-alert-governance'
    description: 'A46: a policy assignment or exemption was written, or a policy assignment or lock deleted. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    operations: [
      'Microsoft.Authorization/policyAssignments/write'
      'Microsoft.Authorization/policyAssignments/delete'
      'Microsoft.Authorization/policyExemptions/write'
      'Microsoft.Authorization/locks/delete'
    ]
  }
]

resource activityAlerts 'Microsoft.Insights/activityLogAlerts@2026-01-01' = [
  for alert in activityLogAlerts: {
    name: alert.name
    location: 'Global'
    tags: tags
    properties: {
      enabled: true
      description: alert.description
      scopes: [for id in subscriptionIds: '/subscriptions/${id}']
      condition: {
        allOf: [
          {
            field: 'category'
            equals: 'Administrative'
          }
          {
            field: 'status'
            equals: 'Succeeded'
          }
          {
            anyOf: [
              for operation in alert.operations: {
                field: 'operationName'
                equals: operation
              }
            ]
          }
        ]
      }
      actions: {
        actionGroups: [
          {
            actionGroupId: securityActionGroup.id
          }
        ]
      }
    }
  }
]

// KQL list literals: string() renders a JSON array, which KQL's dynamic() reads as is. Principal
// and resource IDs are compared lower-cased.
func kqlList(values string[]) string => 'dynamic(${string(map(values, value => toLower(value)))})'

var deployCallers = kqlList(flatten(map(deployIdentityIds, identity => [identity.clientId, identity.principalId])))

// Every load run creates and deletes identities, a Cosmos DB account and diagnostic settings, and
// the load deploy identity can assign only rg-docchat-load's roles and AcrPull, so rules 1 to 3
// ignore its operations there (section 12.6).
var loadDeployCallers = kqlList(flatten(map(
  filter(deployIdentityIds, identity => identity.name == 'id-docchat-deploy-load'),
  identity => [identity.clientId, identity.principalId]
)))

var watchedGroups = '\'rg-docchat-dev\', \'rg-docchat-prod\', \'rg-docchat-load\', \'rg-docchat-shared\', \'rg-docchat-identity\''

var roleAssignmentsQuery = format(
  '''let allowedPrincipals = {0};
let loadDeployCallers = {1};
AzureActivity
| where OperationNameValue in~ ('MICROSOFT.AUTHORIZATION/ROLEASSIGNMENTS/WRITE', 'MICROSOFT.AUTHORIZATION/ROLEASSIGNMENTS/DELETE')
| where ActivityStatusValue in~ ('Success', 'Succeeded')
| where ResourceGroup in~ ({2}) or isempty(ResourceGroup)
| where not(Caller in~ (loadDeployCallers) and (ResourceGroup =~ 'rg-docchat-load' or tolower(_ResourceId) contains '/providers/microsoft.containerregistry/registries/'))
| extend PrincipalId = tolower(tostring(parse_json(tostring(Properties_d.requestbody)).Properties.PrincipalId))
| where OperationNameValue endswith '/DELETE' or PrincipalId !in (allowedPrincipals)
| project TimeGenerated, OperationNameValue, ResourceGroup, _ResourceId, Caller, PrincipalId, CorrelationId''',
  kqlList(allowedRoleAssigneePrincipalIds),
  loadDeployCallers,
  watchedGroups
)

var cosmosDataAccessQuery = format(
  '''let templateAssignments = {0};
let loadDeployCallers = {1};
AzureActivity
| where OperationNameValue in~ ('MICROSOFT.DOCUMENTDB/DATABASEACCOUNTS/SQLROLEASSIGNMENTS/WRITE', 'MICROSOFT.DOCUMENTDB/DATABASEACCOUNTS/SQLROLEASSIGNMENTS/DELETE')
| where ActivityStatusValue in~ ('Success', 'Succeeded')
| where not(Caller in~ (loadDeployCallers) and ResourceGroup =~ 'rg-docchat-load')
| where OperationNameValue endswith '/DELETE' or tolower(_ResourceId) !in (templateAssignments)
| project TimeGenerated, OperationNameValue, ResourceGroup, _ResourceId, Caller, CorrelationId''',
  kqlList(templateCosmosAssignmentIds),
  loadDeployCallers
)

var diagnosticSettingsQuery = format(
  '''let deployCallers = {0};
let loadDeployCallers = {1};
let deploymentCorrelations = AzureActivity
    | where OperationNameValue =~ 'MICROSOFT.RESOURCES/DEPLOYMENTS/WRITE' and Caller in~ (deployCallers)
    | distinct CorrelationId;
AzureActivity
| where OperationNameValue in~ ('MICROSOFT.INSIGHTS/DIAGNOSTICSETTINGS/WRITE', 'MICROSOFT.INSIGHTS/DIAGNOSTICSETTINGS/DELETE')
| where ActivityStatusValue in~ ('Success', 'Succeeded')
| where ResourceGroup in~ ({2}) or isempty(ResourceGroup)
| where not(Caller in~ (loadDeployCallers) and ResourceGroup =~ 'rg-docchat-load')
| where OperationNameValue endswith '/DELETE' or CorrelationId !in (deploymentCorrelations)
| project TimeGenerated, OperationNameValue, ResourceGroup, _ResourceId, Caller, CorrelationId''',
  deployCallers,
  loadDeployCallers,
  watchedGroups
)

var appRegistrationsQuery = '''AuditLogs
| where OperationName startswith 'Update application – Certificates and secrets management' or OperationName =~ 'Add service principal credentials' or OperationName =~ 'Update application'
| mv-expand Target = TargetResources
| where tostring(Target.displayName) startswith 'DocChat'
| mv-apply Property = Target.modifiedProperties on (
    summarize ChangedAddressOrKey = countif(tostring(Property.displayName) in ('AppAddress', 'KeyDescription') and tostring(Property.newValue) != tostring(Property.oldValue))
  )
| where OperationName !~ 'Update application' or ChangedAddressOrKey > 0
| project TimeGenerated, OperationName, TargetName = tostring(Target.displayName), InitiatedBy'''

var registryWritesQuery = '''AzureActivity
| where OperationNameValue =~ 'MICROSOFT.CONTAINERREGISTRY/REGISTRIES/WRITE'
| where ActivityStatusValue in~ ('Success', 'Succeeded')
| project TimeGenerated, ResourceGroup, _ResourceId, Caller, CorrelationId'''

// A membership event names the member first in TargetResources and the group in the event's
// targets and modified properties, so a group is matched anywhere in TargetResources.
var jitAddsQuery = format(
  '''let jitGroups = {0};
AuditLogs
| where OperationName =~ 'Add member to group'
| extend Targets = tolower(tostring(TargetResources)), JitGroups = jitGroups
| mv-apply GroupId = JitGroups to typeof(string) on (where Targets contains GroupId)''',
  kqlList(jitGroupIds)
)

var jitGrantQuery = '''{0}
| project TimeGenerated, GroupId, Member = tostring(TargetResources[0].id), InitiatedBy'''

var jitSelfGrantQuery = '''{0}
| where tostring(InitiatedBy.user.id) == tostring(TargetResources[0].id)
| project TimeGenerated, GroupId, Member = tostring(TargetResources[0].id), InitiatedBy'''

// An addition 4 h 15 min to 4 h 45 min old with no removal within 4 h 15 min of it: each addition
// is checked once it is overdue, so the rule looks back 5 hours (queryTimeRange).
var jitOverdueQuery = format(
  '''let jitGroups = {0};
let jitChanges = (operation: string) {{
    AuditLogs
    | where OperationName =~ operation
    | extend Member = tostring(TargetResources[0].id), Targets = tolower(tostring(TargetResources)), JitGroups = jitGroups
    | mv-apply GroupId = JitGroups to typeof(string) on (where Targets contains GroupId)
    | project TimeGenerated, Member, GroupId
}};
jitChanges('Add member to group')
| where TimeGenerated between (ago(285m) .. ago(255m))
| join kind=leftouter (jitChanges('Remove member from group') | project RemovedAt = TimeGenerated, Member, GroupId) on Member, GroupId
| summarize Removed = countif(RemovedAt between (TimeGenerated .. (TimeGenerated + 255m))) by AddedAt = TimeGenerated, Member, GroupId
| where Removed == 0''',
  kqlList(jitGroupIds)
)

var logAlerts logAlert[] = [
  {
    name: 'docchat-alert-role-assignments'
    description: 'A47: a role-assignment delete in the DocChat groups or at the subscription, or a write naming a principal outside allowedRoleAssigneePrincipalIds. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    severity: 2
    query: roleAssignmentsQuery
  }
  {
    name: 'docchat-alert-cosmos-data-access'
    description: 'A48: a Cosmos DB data-plane role-assignment write or delete other than a re-write of a template-declared assignment. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    severity: 1
    query: cosmosDataAccessQuery
  }
  {
    name: 'docchat-alert-diagnostic-settings'
    description: 'A49: a diagnostic-setting delete, or a write outside a deploy identity\'s deployment. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    severity: 2
    query: diagnosticSettingsQuery
  }
  {
    name: 'docchat-alert-app-registrations'
    description: 'A50: a new credential, or a changed redirect URI or key description, on a DocChat registration. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    severity: 1
    query: appRegistrationsQuery
  }
  {
    name: 'docchat-alert-registry-writes'
    description: 'A51: a container-registry write. Runbook: Alert response index and runbook maps; unexplained: Suspected pipeline compromise.'
    severity: 2
    query: registryWritesQuery
  }
  {
    name: 'docchat-alert-jit-grant'
    description: 'A52: a member added to a just-in-time group. Runbook: Read a user\'s conversation; access review.'
    severity: 3
    query: format(jitGrantQuery, jitAddsQuery)
  }
  {
    name: 'docchat-alert-jit-self-grant'
    description: 'A53: a member added to a just-in-time group by themselves. Runbook: Read a user\'s conversation; access review, then Suspected pipeline compromise.'
    severity: 1
    query: format(jitSelfGrantQuery, jitAddsQuery)
  }
  {
    name: 'docchat-alert-jit-overdue'
    description: 'A54: a just-in-time membership with no removal within 4 hours 15 minutes. Runbook: Read a user\'s conversation; access review.'
    severity: 2
    query: jitOverdueQuery
    queryTimeRange: 'PT5H'
  }
]

resource logSearchAlerts 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = [
  for alert in logAlerts: {
    name: alert.name
    location: location
    tags: tags
    kind: 'LogAlert'
    properties: {
      displayName: alert.name
      description: alert.description
      severity: alert.severity
      enabled: true
      scopes: [
        securityWorkspace.id
      ]
      evaluationFrequency: 'PT15M'
      windowSize: 'PT30M'
      overrideQueryTimeRange: alert.?queryTimeRange
      muteActionsDuration: 'PT30M'
      autoMitigate: false
      // AuditLogs and AzureActivity hold no row until the Entra and Activity Log settings deliver
      // their first records, and the rules must exist from the first run of this step.
      skipQueryValidation: true
      criteria: {
        allOf: [
          {
            query: alert.query
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: 0
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      actions: {
        actionGroups: [
          securityActionGroup.id
        ]
      }
    }
  }
]
