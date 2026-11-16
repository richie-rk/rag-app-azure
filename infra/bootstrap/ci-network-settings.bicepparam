// Parameters of ci-network-settings.bicep, deployed by `scripts/bootstrap/bootstrap.py --step
// ci-runner` into rg-docchat-shared. The step reads the organization's databaseId with
// `gh api graphql` and passes it in DOCCHAT_BOOTSTRAP_GITHUB_DATABASE_ID.
using 'ci-network-settings.bicep'

var config = loadJsonContent('../../scripts/bootstrap/bootstrap.config.json')

param location = config.ciNetworkLocation

param githubOrganizationDatabaseId = int(readEnvironmentVariable('DOCCHAT_BOOTSTRAP_GITHUB_DATABASE_ID', '0'))
