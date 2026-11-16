// The environment's Entra registrations through the Microsoft Graph Bicep extension
// (83-infrastructure.md section 7; ADR 0016). Declaration order is test client, web client, API:
// the API pre-authorizes the two clients by appId, so no declaration references itself or a later
// one. uniqueName makes every deployment an idempotent upsert, and every declared property is
// reset on each deployment.

extension graphV1

@allowed(['dev', 'prod', 'load'])
param environmentName string
param resourceToken string

@description('The tenant ID, lower case.')
param tenantId string

param staticWebAppHostName string
param webCustomDomains string[] = []

var isProd = environmentName == 'prod'
var notes = 'Managed by the DocChat infrastructure templates. Declared properties are reset on every deployment.'
var namePrefix = 'docchat-${environmentName}-${resourceToken}'
var envSuffix = isProd ? '' : ' (${environmentName})'

var microsoftGraphAppId = '00000003-0000-0000-c000-000000000000'
var graphOpenId = '37f7f235-527c-4136-accd-4a02d197296e'
var graphProfile = '14dad69e-099b-42c9-810b-d002981feec1'
var graphOfflineAccess = '7427e0e9-2fba-42fe-b0c0-848c9e6a8182'

// The same constant GUIDs in every environment: they are unique per application, not per tenant.
var accessAsUserScopeId = '772eca72-e66c-46c6-b40d-60f539a5f867'
var adminRoleId = '47dc73a8-b97e-4be8-9481-614f6b17a11f'
var userRoleId = '6f46cf45-f1e3-4812-8003-c51ae0f5f98d'

var apiIdentifierUri = 'api://${tenantId}/${namePrefix}-api'
var webHosts = concat([staticWebAppHostName], webCustomDomains)
var homeHost = empty(webCustomDomains) ? staticWebAppHostName : webCustomDomains[0]

// Entra ignores the port of a localhost redirect; 5173 is the web client's dev-server port.
var devRedirectUris = environmentName == 'dev' ? ['http://localhost:5173/'] : []

var noImplicitGrant = {
  enableIdTokenIssuance: false
  enableAccessTokenIssuance: false
}

resource testApp 'Microsoft.Graph/applications@v1.0' = if (!isProd) {
  uniqueName: '${namePrefix}-test'
  displayName: 'DocChat test client${envSuffix}'
  signInAudience: 'AzureADMyOrg'
  identifierUris: []
  // Public client flows on, for the test token method (ROPC without a redirect URI).
  isFallbackPublicClient: true
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: []
    preAuthorizedApplications: []
  }
  appRoles: []
  spa: {
    redirectUris: []
  }
  web: {
    redirectUris: []
    implicitGrantSettings: noImplicitGrant
  }
  publicClient: {
    redirectUris: []
  }
  requiredResourceAccess: [
    {
      resourceAppId: microsoftGraphAppId
      resourceAccess: [
        { id: graphOpenId, type: 'Scope' }
        { id: graphProfile, type: 'Scope' }
      ]
    }
  ]
  notes: notes
}

resource webApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${namePrefix}-web'
  displayName: 'DocChat${envSuffix}'
  signInAudience: 'AzureADMyOrg'
  identifierUris: []
  isFallbackPublicClient: false
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: []
    preAuthorizedApplications: []
  }
  appRoles: []
  spa: {
    redirectUris: concat(map(webHosts, host => 'https://${host}/'), devRedirectUris)
  }
  web: {
    redirectUris: []
    homePageUrl: 'https://${homeHost}/'
    implicitGrantSettings: noImplicitGrant
  }
  publicClient: {
    redirectUris: []
  }
  requiredResourceAccess: [
    {
      resourceAppId: microsoftGraphAppId
      resourceAccess: [
        { id: graphOpenId, type: 'Scope' }
        { id: graphProfile, type: 'Scope' }
        { id: graphOfflineAccess, type: 'Scope' }
      ]
    }
  ]
  notes: notes
}

var preAuthorizedClients = concat(
  [
    {
      appId: webApp.appId
      delegatedPermissionIds: [accessAsUserScopeId]
    }
  ],
  isProd
    ? []
    : [
        {
          appId: testApp.appId
          delegatedPermissionIds: [accessAsUserScopeId]
        }
      ]
)

resource apiApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: '${namePrefix}-api'
  displayName: 'DocChat API${envSuffix}'
  signInAudience: 'AzureADMyOrg'
  identifierUris: [apiIdentifierUri]
  isFallbackPublicClient: false
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: [
      {
        id: accessAsUserScopeId
        value: 'access_as_user'
        type: 'User'
        isEnabled: true
        adminConsentDisplayName: 'Use DocChat as the signed-in user'
        adminConsentDescription: 'Lets the DocChat web client call the DocChat API for the signed-in user, with only that user\'s access.'
        userConsentDisplayName: 'Use DocChat as you'
        userConsentDescription: 'Lets the DocChat web client call the DocChat API for you, with only your access.'
      }
    ]
    preAuthorizedApplications: preAuthorizedClients
  }
  appRoles: [
    {
      id: adminRoleId
      value: 'Admin'
      displayName: 'DocChat administrator'
      description: 'Manages every project, its members, index mappings, indexes and documents, and sees ingestion runs.'
      allowedMemberTypes: ['User']
      isEnabled: true
    }
    {
      id: userRoleId
      value: 'User'
      displayName: 'DocChat user'
      description: 'Chats in the projects they belong to and manages their own conversations, feedback and memory.'
      allowedMemberTypes: ['User']
      isEnabled: true
    }
  ]
  spa: {
    redirectUris: []
  }
  web: {
    redirectUris: []
    implicitGrantSettings: noImplicitGrant
  }
  publicClient: {
    redirectUris: []
  }
  requiredResourceAccess: []
  optionalClaims: {
    accessToken: [
      {
        name: 'email'
        essential: false
      }
    ]
  }
  notes: notes
}

resource testServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = if (!isProd) {
  appId: testApp.appId
  appRoleAssignmentRequired: true
  accountEnabled: true
  tags: ['WindowsAzureActiveDirectoryIntegratedApp', 'HideApp']
  notes: notes
}

resource webServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: webApp.appId
  appRoleAssignmentRequired: true
  accountEnabled: true
  tags: ['WindowsAzureActiveDirectoryIntegratedApp']
  notes: notes
}

resource apiServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: apiApp.appId
  appRoleAssignmentRequired: true
  accountEnabled: true
  tags: ['WindowsAzureActiveDirectoryIntegratedApp', 'HideApp']
  notes: notes
}

output webAppId string = webApp.appId
output apiAppId string = apiApp.appId
output testAppId string = isProd ? '' : testApp.appId
output apiIdentifierUri string = apiIdentifierUri
output apiScope string = '${apiIdentifierUri}/access_as_user'
