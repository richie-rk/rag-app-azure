// The SQL server and database docchat with both audit specifications (83-infrastructure.md
// sections 2.8 and 2.9; ADRs 0104 and 0108). Entra-only authentication; the Entra administrator is
// the group sg-docchat-<env>-sql-admins, whose only member is the deploy identity.

import { environmentConfig } from '../../types.bicep'

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string
param location string
param tags object
param config environmentConfig

@description('Operator allowlist: one standing firewall rule operator-<n> per address.')
param operatorIpAllowlist string[]

param workspaceId string

var serverName = 'sql-docchat-${environmentName}-${resourceToken}'
var apiPrincipal = 'id-docchat-api-${environmentName}'
var ingestionPrincipal = 'id-docchat-ingest-${environmentName}'
var serverless = startsWith(config.sql.sku, 'GP_S_')

// The SKU names carry their vCore count: GP_S_Gen5_2 is family Gen5, 2 vCores, serverless.
var skuParts = split(config.sql.sku, '_')
var databaseSku = {
  name: join(take(skuParts, length(skuParts) - 1), '_')
  tier: 'GeneralPurpose'
  family: 'Gen5'
  capacity: int(last(skuParts))
}

var serverlessSettings = serverless
  ? {
      autoPauseDelay: config.sql.autoPauseDelayMinutes
      minCapacity: string(config.sql.minCapacity!)
    }
  : {}

// Only prod reads sql.zoneRedundant; dev and load are never zone-redundant (ADR 0102).
var licenseSettings = environmentName == 'prod' ? { licenseType: 'LicenseIncluded' } : {}

// The workload audit's action groups are chosen by the stage-0 audit sentinel check (SP-5,
// ADR 0108). Until its outcome is recorded the template uses the change and authentication
// groups, which record no batch text, so no bound value of a workload identity can reach a log
// before the check proves it can't.
var workloadAuditActionsAndGroups = [
  'SCHEMA_OBJECT_CHANGE_GROUP'
  'DATABASE_OBJECT_CHANGE_GROUP'
  'DATABASE_PRINCIPAL_CHANGE_GROUP'
  'DATABASE_ROLE_MEMBER_CHANGE_GROUP'
  'DATABASE_PERMISSION_CHANGE_GROUP'
  'DATABASE_OBJECT_PERMISSION_CHANGE_GROUP'
  'SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP'
  'FAILED_DATABASE_AUTHENTICATION_GROUP'
]

module server 'br/public:avm/res/sql/server:0.22.1' = {
  name: serverName
  params: {
    name: serverName
    location: location
    tags: tags
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: 'sg-docchat-${environmentName}-sql-admins'
      principalType: 'Group'
      sid: config.groups.sqlAdmins
      tenantId: config.tenantId
    }
    minimalTlsVersion: '1.3'
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: 'Disabled'
    firewallRules: [
      for (address, index) in operatorIpAllowlist: {
        name: 'operator-${index + 1}'
        startIpAddress: address
        endIpAddress: address
      }
    ]
    // The two audit specifications are declared below on the GA extendedAuditingSettings API;
    // an empty object keeps the module from writing a second, predicate-less policy.
    auditSettings: {}
    databases: [
      union(
        {
          name: 'docchat'
          availabilityZone: -1
          sku: databaseSku
          collation: 'SQL_Latin1_General_CP1_CI_AS'
          maxSizeBytes: config.sql.maxSizeGb * 1073741824
          zoneRedundant: environmentName == 'prod' ? config.sql.zoneRedundant : false
          requestedBackupStorageRedundancy: config.sql.backupRedundancy
          backupShortTermRetentionPolicy: {
            retentionDays: config.sql.pitrDays
            diffBackupIntervalInHours: 12
          }
          diagnosticSettings: [
            {
              name: 'to-log-analytics'
              workspaceResourceId: workspaceId
              logAnalyticsDestinationType: 'Dedicated'
              logCategoriesAndGroups: [
                { category: 'SQLSecurityAuditEvents' }
                { category: 'Errors' }
                { category: 'Timeouts' }
                { category: 'Blocks' }
                { category: 'Deadlocks' }
              ]
              metricCategories: [
                { category: 'Basic' }
                { category: 'InstanceAndAppAdvanced' }
              ]
            }
          ]
        },
        serverlessSettings,
        licenseSettings
      )
    ]
    enableTelemetry: false
  }
}

resource sqlServer 'Microsoft.Sql/servers@2025-01-01' existing = {
  name: serverName
  dependsOn: [server]
}

resource masterDatabase 'Microsoft.Sql/servers/databases@2025-01-01' existing = {
  parent: sqlServer
  name: 'master'
}

resource docchatDatabase 'Microsoft.Sql/servers/databases@2025-01-01' existing = {
  parent: sqlServer
  name: 'docchat'
}

// Auditing to Log Analytics works only with this setting on master; the AVM module doesn't create
// it. Its twin sql-audit-security, to log-docchat-security, belongs to the tightening run, so no
// template change can remove it.
// The preview register's one entry (ADR 0009): only this version selects resource-specific tables.
#disable-next-line use-recent-api-versions
resource sqlAuditDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: masterDatabase
  name: 'sql-audit'
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'SQLSecurityAuditEvents'
        enabled: true
      }
      {
        category: 'DevOpsOperationsAudit'
        enabled: true
      }
    ]
  }
}

// Privileged audit: every principal except the two workload identities, with full batch text.
resource privilegedAudit 'Microsoft.Sql/servers/extendedAuditingSettings@2025-01-01' = {
  parent: sqlServer
  name: 'default'
  properties: {
    state: 'Enabled'
    isAzureMonitorTargetEnabled: true
    isDevopsAuditEnabled: true
    queueDelayMs: 1000
    auditActionsAndGroups: [
      'BATCH_COMPLETED_GROUP'
      'SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP'
      'FAILED_DATABASE_AUTHENTICATION_GROUP'
    ]
    predicateExpression: 'database_principal_name <> \'${apiPrincipal}\' AND database_principal_name <> \'${ingestionPrincipal}\''
  }
  dependsOn: [sqlAuditDiagnostics]
}

// Workload audit: the complement of the privileged predicate, so each event is recorded once.
resource workloadAudit 'Microsoft.Sql/servers/databases/extendedAuditingSettings@2025-01-01' = {
  parent: docchatDatabase
  name: 'default'
  properties: {
    state: 'Enabled'
    isAzureMonitorTargetEnabled: true
    queueDelayMs: 1000
    predicateExpression: 'database_principal_name = \'${apiPrincipal}\' OR database_principal_name = \'${ingestionPrincipal}\''
    auditActionsAndGroups: workloadAuditActionsAndGroups
  }
}

output id string = sqlServer.id
output name string = serverName
output fullyQualifiedDomainName string = sqlServer.properties.fullyQualifiedDomainName
