// Parameters of security.bicep, deployed by `scripts/bootstrap/bootstrap.py --step security` into
// rg-docchat-identity (section 12.4). The configuration values come from bootstrap.config.json, which
// the step and tighten_conditions.py update before each deployment; the step reads the deploy
// identities' IDs and the just-in-time groups' IDs and passes them in DOCCHAT_BOOTSTRAP_* variables.
using 'security.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

param securityAlertEmails = config.securityAlertEmails

param subscriptionIds = filter(
  union([config.subscriptions.dev], [config.subscriptions.prod], [config.subscriptions.load]),
  id => !empty(id)
)

param deployIdentityIds = json(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_DEPLOY_IDENTITY_IDS', '[]'))

param allowedRoleAssigneePrincipalIds = config.allowedRoleAssigneePrincipalIds

param templateCosmosAssignmentIds = config.templateCosmosAssignmentIds

param jitGroupIds = filter(split(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_JIT_GROUP_IDS', ''), ','), id => !empty(id))
