// Parameters of assignments.bicep, deployed by `scripts/bootstrap/bootstrap.py --step assignments`
// once per subscription in use. The step reads the pipeline identities' principal IDs, the two
// group IDs and whether log-docchat-dev exists, and passes them in the DOCCHAT_BOOTSTRAP_*
// variables below.
using 'assignments.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

var unset = '00000000-0000-0000-0000-000000000000'

param homeSubscriptionId = config.subscriptions.dev

param environmentNames = filter(
  split(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_ENVIRONMENTS', 'dev,prod,load'), ','),
  name => !empty(name)
)

param pipelinePrincipalIds = json(readEnvironmentVariable(
  'DOCCHAT_BOOTSTRAP_PIPELINE_PRINCIPAL_IDS',
  '{"deployDev": "${unset}", "deployProd": "${unset}", "deployLoad": "${unset}", "pr": "${unset}", "citest": "${unset}"}'
))

param operatorsGroupId = readEnvironmentVariable('DOCCHAT_BOOTSTRAP_OPERATORS_GROUP_ID', unset)

param devConfigOperatorsGroupId = readEnvironmentVariable('DOCCHAT_BOOTSTRAP_DEV_CONFIG_OPERATORS_GROUP_ID', unset)

param ciPrivateNetworking = config.ciPrivateNetworking

param devWorkspaceExists = bool(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_DEV_WORKSPACE_EXISTS', 'false'))
