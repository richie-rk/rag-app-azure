// Parameters of policies.bicep, deployed by `scripts/bootstrap/bootstrap.py --step policies` once
// per subscription in use; the step sets DOCCHAT_BOOTSTRAP_ENVIRONMENTS to the environments whose
// resource group lives in that subscription.
using 'policies.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

param homeSubscriptionId = config.subscriptions.dev

param environmentNames = filter(
  split(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_ENVIRONMENTS', 'dev,prod,load'), ','),
  name => !empty(name)
)
