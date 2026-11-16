// One network security group per subnet of the environment network, deny by default inside the
// network (83-infrastructure.md section 4.3; ADR 0098). The platform's default rules stay, so
// load-balancer probes and outbound internet keep working.

param environmentName string
param location string
param tags object

@description('Address prefixes of the subnets, keyed api, ingest, privateEndpoints and, in load, stub.')
param subnetPrefixes object

var hasStub = environmentName == 'load'
var appPrefixes = [subnetPrefixes.api, subnetPrefixes.ingest]
var sqlPorts = ['1433', '11000-11999']

var denyVnetInbound = {
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

var denyVnetOutbound = {
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

var integrationRules = [
  {
    name: 'allow-private-endpoints'
    properties: {
      priority: 100
      direction: 'Outbound'
      access: 'Allow'
      protocol: 'Tcp'
      sourceAddressPrefix: '*'
      sourcePortRange: '*'
      destinationAddressPrefix: subnetPrefixes.privateEndpoints
      destinationPortRanges: concat(['443'], sqlPorts)
    }
  }
  denyVnetOutbound
  denyVnetInbound
]

resource privateEndpointsNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = {
  name: 'nsg-docchat-${environmentName}-pe'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-apps-https'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: hasStub ? concat(appPrefixes, [subnetPrefixes.stub]) : appPrefixes
          sourcePortRange: '*'
          destinationAddressPrefix: subnetPrefixes.privateEndpoints
          destinationPortRange: '443'
        }
      }
      {
        name: 'allow-apps-sql'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: appPrefixes
          sourcePortRange: '*'
          destinationAddressPrefix: subnetPrefixes.privateEndpoints
          destinationPortRanges: sqlPorts
        }
      }
      // Private endpoints originate no connection and replies to admitted flows pass statefully,
      // so this changes no path; it states the deny-by-default outbound like every other group.
      denyVnetOutbound
      denyVnetInbound
    ]
  }
}

resource apiNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = {
  name: 'nsg-docchat-${environmentName}-api'
  location: location
  tags: tags
  properties: {
    securityRules: integrationRules
  }
}

resource ingestNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = {
  name: 'nsg-docchat-${environmentName}-ingest'
  location: location
  tags: tags
  properties: {
    securityRules: integrationRules
  }
}

resource stubNsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = if (hasStub) {
  name: 'nsg-docchat-load-stub'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-private-endpoints-https'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: subnetPrefixes.privateEndpoints
          destinationPortRange: '443'
        }
      }
      denyVnetOutbound
    ]
  }
}

output privateEndpointsNsgId string = privateEndpointsNsg.id
output apiNsgId string = apiNsg.id
output ingestNsgId string = ingestNsg.id
output stubNsgId string = hasStub ? stubNsg.id : ''
