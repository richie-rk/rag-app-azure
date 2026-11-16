// The environment network and its subnets (83-infrastructure.md section 4.1; ADR 0098): Azure-
// provided DNS, no peering, no DDoS plan. Subnets are derived from network.addressSpace, so the
// environment files hold one /22 and nothing else about the network.

param environmentName string
param location string
param tags object

@description('The environment\'s /22, network.addressSpace of the environment file.')
param addressSpace string

@description('Subnet prefixes derived from addressSpace, keyed api, ingest, privateEndpoints, stub.')
param subnetPrefixes object

param privateEndpointsNsgId string
param apiNsgId string
param ingestNsgId string

@description('nsg-docchat-load-stub in load; empty elsewhere.')
param stubNsgId string

var vnetName = 'vnet-docchat-${environmentName}'
var hasStub = environmentName == 'load'
var webDelegation = 'Microsoft.Web/serverFarms'

// App Service manages a delegated subnet's outbound path, so the integration subnets leave
// defaultOutboundAccess unset.
var appSubnets = [
  {
    name: 'snet-api-integration'
    addressPrefix: subnetPrefixes.api
    delegation: webDelegation
    networkSecurityGroupResourceId: apiNsgId
  }
  {
    name: 'snet-ingest-integration'
    addressPrefix: subnetPrefixes.ingest
    delegation: webDelegation
    networkSecurityGroupResourceId: ingestNsgId
  }
  {
    name: 'snet-private-endpoints'
    addressPrefix: subnetPrefixes.privateEndpoints
    networkSecurityGroupResourceId: privateEndpointsNsgId
    privateEndpointNetworkPolicies: 'Enabled'
    defaultOutboundAccess: false
  }
]

var stubSubnets = hasStub
  ? [
      {
        name: 'snet-stub-integration'
        addressPrefix: subnetPrefixes.stub
        delegation: webDelegation
        networkSecurityGroupResourceId: stubNsgId
      }
    ]
  : []

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'vnet-${environmentName}'
  params: {
    name: vnetName
    location: location
    tags: tags
    addressPrefixes: [addressSpace]
    subnets: concat(appSubnets, stubSubnets)
    enableTelemetry: false
  }
}

output vnetId string = vnet.outputs.resourceId
output vnetName string = vnetName
output apiSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-api-integration')
output ingestSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-ingest-integration')
output privateEndpointsSubnetId string = resourceId(
  'Microsoft.Network/virtualNetworks/subnets',
  vnetName,
  'snet-private-endpoints'
)

@description('snet-stub-integration in load; empty elsewhere.')
output stubSubnetId string = hasStub
  ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-stub-integration')
  : ''
