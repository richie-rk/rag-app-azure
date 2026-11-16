// Parameters of roles.bicep, deployed by `scripts/bootstrap/bootstrap.py --step roles` into the
// subscription of `dev` (the subscription of rg-docchat-identity and rg-docchat-shared).
using 'roles.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

// Every subscription in use, each listed once; the placeholder configuration lists none yet.
param subscriptionIds = filter(
  union([config.subscriptions.dev], [config.subscriptions.prod], [config.subscriptions.load]),
  id => !empty(id)
)
