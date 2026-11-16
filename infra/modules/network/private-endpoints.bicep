// Private endpoints in snet-private-endpoints, each approved automatically and with one DNS zone
// group named default (83-infrastructure.md section 4.4).

param location string
param tags object
param subnetId string

@description('One entry per endpoint: {targetName, groupId, targetId, zoneId}.')
param endpoints object[]

module privateEndpoints 'br/public:avm/res/network/private-endpoint:0.12.1' = [
  for endpoint in endpoints: {
    name: 'pe-${endpoint.targetName}-${endpoint.groupId}'
    params: {
      name: 'pe-${endpoint.targetName}-${endpoint.groupId}'
      location: location
      tags: tags
      subnetResourceId: subnetId
      customNetworkInterfaceName: 'nic-pe-${endpoint.targetName}-${endpoint.groupId}'
      privateLinkServiceConnections: [
        {
          name: 'pe-${endpoint.targetName}-${endpoint.groupId}'
          properties: {
            privateLinkServiceId: endpoint.targetId
            groupIds: [endpoint.groupId]
          }
        }
      ]
      privateDnsZoneGroup: {
        name: 'default'
        privateDnsZoneGroupConfigs: [
          {
            privateDnsZoneResourceId: endpoint.zoneId
          }
        ]
      }
      enableTelemetry: false
    }
  }
]
