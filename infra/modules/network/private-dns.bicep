// The private DNS zones of the environment, each linked to its network only and with no
// auto-registration (83-infrastructure.md section 4.4). Every service is called on its public
// host name, which these zones resolve to the private endpoint inside the network.

param tags object

@description('Zone names, one module call per zone.')
param zoneNames string[]

param vnetId string
param vnetName string

module zones 'br/public:avm/res/network/private-dns-zone:0.8.1' = [
  for zone in zoneNames: {
    name: 'zone-${zone}'
    params: {
      name: zone
      location: 'global'
      tags: tags
      virtualNetworkLinks: [
        {
          name: 'link-${vnetName}'
          virtualNetworkResourceId: vnetId
          registrationEnabled: false
        }
      ]
      enableTelemetry: false
    }
  }
]

output zoneIds object = toObject(zoneNames, zone => zone, zone => resourceId('Microsoft.Network/privateDnsZones', zone))
