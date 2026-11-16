// Parameters of activity-log.bicep, deployed by `scripts/bootstrap/bootstrap.py --step security`
// into each subscription in use.
using 'activity-log.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

param homeSubscriptionId = config.subscriptions.dev
