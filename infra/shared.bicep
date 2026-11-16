// The shared resources of rg-docchat-shared, deployed by the bootstrap's shared step only
// (83-infrastructure.md sections 2.16, 2.17 and 4.2; ADR 0122): the container registry, the
// test-credential vault and, under the private-networking option only, the CI network with the
// vault's private endpoint and its DNS zone.

targetScope = 'resourceGroup'

@description('DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST: the registry\'s and the test vault\'s IP rules.')
@maxLength(20)
param sharedOperatorIpAllowlist string[]

@description('The bootstrap key ciPrivateNetworking: true declares the CI network and closes the vault to every public client.')
param ciPrivateNetworking bool

param sharedRegistryLocation string

@description('Region of vnet-docchat-ci; read only when ciPrivateNetworking is true.')
param ciNetworkLocation string

@description('Resource ID of log-docchat-security, the destination of the shared resources\' diagnostic settings.')
param securityWorkspaceId string

@description('Region of the test-credential vault: that of rg-docchat-shared.')
param location string = resourceGroup().location

var sharedToken = take(uniqueString(subscription().id, 'shared'), 6)
var vaultName = 'kv-docchat-tst-${sharedToken}'
var ciVnetName = 'vnet-docchat-ci'
var vaultZoneName = 'privatelink.vaultcore.azure.net'
var runnersPrefix = '10.60.0.0/26'
var privateEndpointsPrefix = '10.60.0.64/27'

var tags = {
  'docchat-env': 'shared'
}

// By default the vault keeps public access with no standing rule, so only a test job's transient
// runner rule admits a runner; under the private-networking option an empty allowlist closes it.
var vaultPublicAccess = !empty(sharedOperatorIpAllowlist) || !ciPrivateNetworking ? 'Enabled' : 'Disabled'

module registry 'modules/registry/registry.bicep' = {
  name: 'shared-registry'
  params: {
    location: sharedRegistryLocation
    tags: union(tags, { 'docchat-component': 'registry' })
    sharedToken: sharedToken
    sharedOperatorIpAllowlist: sharedOperatorIpAllowlist
    securityWorkspaceId: securityWorkspaceId
  }
}

module testVault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: vaultName
  params: {
    name: vaultName
    location: location
    tags: union(tags, { 'docchat-component': 'security' })
    sku: 'standard'
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    enableVaultForDeployment: false
    enableVaultForTemplateDeployment: false
    enableVaultForDiskEncryption: false
    publicNetworkAccess: vaultPublicAccess
    // Each deployment writes ipRules from the shared allowlist alone, removing any live runner rule.
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [for address in sharedOperatorIpAllowlist: { value: address }]
    }
    diagnosticSettings: [
      {
        name: 'to-log-analytics'
        workspaceResourceId: securityWorkspaceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          { category: 'AuditEvent' }
        ]
        metricCategories: [
          { category: 'AllMetrics' }
        ]
      }
    ]
    enableTelemetry: false
  }
}

resource runnersNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = if (ciPrivateNetworking) {
  name: 'nsg-docchat-ci-runners'
  location: ciNetworkLocation
  tags: union(tags, { 'docchat-component': 'network' })
  properties: {
    // The platform's internet-outbound rule stays, so runners reach GitHub, Microsoft Entra ID,
    // Azure Resource Manager, the environments' API host names and package indexes.
    securityRules: [
      {
        name: 'allow-vault-endpoint'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointsPrefix
          destinationPortRange: '443'
        }
      }
      {
        name: 'deny-vnet-outbound'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-vnet-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource privateEndpointsNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = if (ciPrivateNetworking) {
  name: 'nsg-docchat-ci-pe'
  location: ciNetworkLocation
  tags: union(tags, { 'docchat-component': 'network' })
  properties: {
    securityRules: [
      {
        name: 'allow-runners-https'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: runnersPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: privateEndpointsPrefix
          destinationPortRange: '443'
        }
      }
      // The vault's endpoint originates no connection and replies to admitted flows pass
      // statefully, so this changes no path; it states the deny-by-default outbound.
      {
        name: 'deny-vnet-outbound'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'VirtualNetwork'
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-vnet-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

module ciNetwork 'br/public:avm/res/network/virtual-network:0.10.2' = if (ciPrivateNetworking) {
  name: ciVnetName
  params: {
    name: ciVnetName
    location: ciNetworkLocation
    tags: union(tags, { 'docchat-component': 'network' })
    addressPrefixes: ['10.60.0.0/24']
    subnets: [
      {
        name: 'snet-ci-runners'
        addressPrefix: runnersPrefix
        delegation: 'GitHub.Network/networkSettings'
        networkSecurityGroupResourceId: runnersNsg.id
        // Set explicitly so the value can't move with an API version's default.
        defaultOutboundAccess: true
      }
      {
        name: 'snet-ci-private-endpoints'
        addressPrefix: privateEndpointsPrefix
        networkSecurityGroupResourceId: privateEndpointsNsg.id
        privateEndpointNetworkPolicies: 'Enabled'
        defaultOutboundAccess: false
      }
    ]
    enableTelemetry: false
  }
}

module vaultZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = if (ciPrivateNetworking) {
  name: 'zone-${vaultZoneName}'
  params: {
    name: vaultZoneName
    location: 'global'
    tags: union(tags, { 'docchat-component': 'network' })
    // Linked to the CI network only: no environment network reaches the test vault.
    virtualNetworkLinks: [
      {
        name: 'link-${ciVnetName}'
        virtualNetworkResourceId: ciNetwork!.outputs.resourceId
        registrationEnabled: false
      }
    ]
    enableTelemetry: false
  }
}

module vaultEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = if (ciPrivateNetworking) {
  name: 'pe-${vaultName}-vault'
  params: {
    name: 'pe-${vaultName}-vault'
    location: ciNetworkLocation
    tags: union(tags, { 'docchat-component': 'network' })
    subnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', ciVnetName, 'snet-ci-private-endpoints')
    customNetworkInterfaceName: 'nic-pe-${vaultName}-vault'
    privateLinkServiceConnections: [
      {
        name: 'pe-${vaultName}-vault'
        properties: {
          privateLinkServiceId: testVault.outputs.resourceId
          groupIds: ['vault']
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          // The zone's own ID, not the module's output: the AVM module branches on this group, and
          // a runtime value there short-circuits what-if (what-if-short-circuiting).
          privateDnsZoneResourceId: resourceId('Microsoft.Network/privateDnsZones', vaultZoneName)
        }
      ]
    }
    enableTelemetry: false
  }
  dependsOn: [
    vaultZone
  ]
}

output registryName string = registry.outputs.name
output registryLoginServer string = registry.outputs.loginServer
output testVaultName string = vaultName
output testVaultUri string = testVault.outputs.uri
