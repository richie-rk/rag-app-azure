// The five pipeline identities and their GitHub federated credentials (83-infrastructure.md
// section 6.1), the security workspace and the security action group (sections 12.4 and 12.8),
// deployed only by the bootstrap's `identity` step into rg-docchat-identity, where no pipeline
// identity holds any role (ADR 0114, ADR 0120).
targetScope = 'resourceGroup'

type pipelineIdentityName =
  | 'id-docchat-deploy-dev'
  | 'id-docchat-deploy-prod'
  | 'id-docchat-deploy-load'
  | 'id-docchat-pr'
  | 'id-docchat-citest'

@sealed()
@description('One federated credential of the plan the bootstrap checked for shared subjects.')
type federatedCredential = {
  identity: pipelineIdentityName
  name: string
  subject: string
}

@description('The region of rg-docchat-identity.')
param location string = resourceGroup().location

@description('The federated credentials, exactly as the bootstrap planned them and compared them for a subject shared by two identities (`oidc_subject_shared`).')
param federatedCredentials federatedCredential[] = []

@description('Receivers of ag-docchat-security: shared mailboxes from bootstrap.config.json.')
param securityAlertEmails string[] = []

var identityNames pipelineIdentityName[] = [
  'id-docchat-deploy-dev'
  'id-docchat-deploy-prod'
  'id-docchat-deploy-load'
  'id-docchat-pr'
  'id-docchat-citest'
]

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = [
  for name in identityNames: {
    name: name
    location: location
    tags: {
      'docchat-env': 'shared'
      'docchat-component': 'security'
    }
  }
]

// Several credentials written under one identity at the same time fail with 409, so they are
// created one at a time; the bootstrap retries a remaining 409 (section 11.2 step 5).
@batchSize(1)
resource credentials 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = [
  for credential in federatedCredentials: {
    parent: identities[indexOf(identityNames, credential.identity)]
    name: credential.name
    properties: {
      issuer: 'https://token.actions.githubusercontent.com'
      subject: credential.subject
      audiences: [
        'api://AzureADTokenExchange'
      ]
    }
  }
]

resource securityWorkspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'log-docchat-security'
  location: location
  tags: {
    'docchat-env': 'shared'
    'docchat-component': 'monitoring'
  }
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 365
    features: {
      disableLocalAuth: true
    }
  }
}

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

output securityWorkspaceId string = securityWorkspace.id
output securityActionGroupId string = securityActionGroup.id
