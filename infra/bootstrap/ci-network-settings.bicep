// The GitHub network settings `ghns-docchat-ci`, which link snet-ci-runners to the GitHub
// organization (83-infrastructure.md sections 4.2 and 11.3; ADR 0122). Deployed into
// rg-docchat-shared only by the bootstrap's `ci-runner` step, only under the private-networking
// option and only while the resource doesn't exist, so no redeploy rewrites the GitHubId tag the
// service sets on it.
targetScope = 'resourceGroup'

@description('The region of vnet-docchat-ci (bootstrap.config.json ciNetworkLocation).')
param location string = resourceGroup().location

@description('The GitHub organization\'s databaseId, read by the ci-runner step.')
param githubOrganizationDatabaseId int = 0

@description('The resource ID of snet-ci-runners, delegated to GitHub.Network/networkSettings.')
param ciRunnersSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', 'vnet-docchat-ci', 'snet-ci-runners')

resource networkSettings 'GitHub.Network/networkSettings@2024-04-02' = {
  name: 'ghns-docchat-ci'
  location: location
  tags: {
    'docchat-env': 'shared'
    'docchat-component': 'network'
  }
  properties: {
    // The organization's databaseId as a string, not a resource ID, so the resource-ID rule doesn't apply.
    #disable-next-line use-resource-id-functions
    businessId: string(githubOrganizationDatabaseId)
    subnetId: ciRunnersSubnetId
  }
}

output networkSettingsId string = networkSettings.id
