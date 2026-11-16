// The subscription Activity Log diagnostic setting `activity-to-security` (83-infrastructure.md
// section 12.4), deployed only by the bootstrap's `security` step into each subscription in use, so
// every Administrative, Security, Policy and Alert event reaches log-docchat-security, where no
// pipeline identity can write, and the log search alerts of section 12.6 read it there.
targetScope = 'subscription'

@description('The subscription of rg-docchat-identity, which holds log-docchat-security (the subscription of `dev`).')
param homeSubscriptionId string = subscription().subscriptionId

// The preview register's one entry (section 8.7): the design pins this version for every diagnostic
// setting, so the linter's newer-version rule is silenced here and nowhere else in this template.
#disable-next-line use-recent-api-versions
resource activityToSecurity 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'activity-to-security'
  properties: {
    workspaceId: resourceId(
      homeSubscriptionId,
      'rg-docchat-identity',
      'Microsoft.OperationalInsights/workspaces',
      'log-docchat-security'
    )
    logs: [
      for category in ['Administrative', 'Security', 'Policy', 'Alert']: {
        category: category
        enabled: true
      }
    ]
  }
}
