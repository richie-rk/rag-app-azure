// The environment's one action group, ag-docchat-<env>-ops (83-infrastructure.md section 12.8).
// Security alerts never come here: they go to the bootstrap's ag-docchat-security, whose receivers
// no pipeline identity can change.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param tags object

@description('Shared mailboxes from DOCCHAT_OPS_ALERT_EMAILS; the preflight refuses an empty list in prod.')
param opsAlertEmails string[]

module actionGroup 'br/public:avm/res/insights/action-group:0.8.0' = {
  name: 'ag-docchat-${environmentName}-ops'
  params: {
    name: 'ag-docchat-${environmentName}-ops'
    groupShortName: 'dc${environmentName}ops'
    location: 'global'
    tags: tags
    enabled: true
    emailReceivers: [
      for (address, index) in opsAlertEmails: {
        name: 'ops-${index + 1}'
        emailAddress: address
        useCommonAlertSchema: true
      }
    ]
    enableTelemetry: false
  }
}

output id string = actionGroup.outputs.resourceId
