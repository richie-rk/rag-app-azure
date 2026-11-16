// Parameters of identity.bicep, deployed by `scripts/bootstrap/bootstrap.py --step identity` into
// rg-docchat-identity. The step sets DOCCHAT_BOOTSTRAP_FEDERATED_CREDENTIALS to the credential plan
// it checked, so the template creates exactly the subjects the check compared.
using 'identity.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

param federatedCredentials = json(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_FEDERATED_CREDENTIALS', '[]'))

param securityAlertEmails = config.securityAlertEmails
