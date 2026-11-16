// The content filter policies and the model deployments of the Azure OpenAI account, on their GA
// APIs (83-infrastructure.md sections 5.1, 5.2 and 5.6; ADRs 0066, 0067, 0099 and 0100). Both
// policies are declared before any deployment, and the deployments are written one at a time,
// each after the policies, because the service refuses concurrent deployment writes on an account.
//
// Every deployment takes its SKU from its own type field: no deployment inherits another's type and
// none is derived. Text deployments name their content filter policy; embedding deployments name
// none (content filtering doesn't apply to embedding models) and never upgrade in place, because a
// changed embedding model needs a new physical index.

import { deploymentType, openAiDeployment } from '../../types.bicep'

@sealed()
@description('One embedding deployment, embed-query-<key> or embed-ingest-<key>, from an openAi.embeddingModels entry.')
type embeddingDeployment = {
  @minLength(2)
  @maxLength(64)
  name: string
  model: string
  @minLength(1)
  version: string
  type: deploymentType
  @minValue(1)
  @maxValue(10000)
  capacity: int
}

@description('Name of the oai-docchat-<env>-<token> account.')
param accountName string

@description('Chat model deployments (chat, chat-small, ingest-summary), each under the content filter policy its object names.')
param textDeployments openAiDeployment[]

@description('The embed-query-<key> and embed-ingest-<key> pair of every openAi.embeddingModels entry.')
param embeddingDeployments embeddingDeployment[]

var harmFilters = [
  { name: 'Hate', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Hate', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Sexual', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Sexual', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Violence', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Violence', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Selfharm', source: 'Prompt', enabled: true, blocking: true, severityThreshold: 'Medium' }
  { name: 'Selfharm', source: 'Completion', enabled: true, blocking: true, severityThreshold: 'Medium' }
]

var shieldFilters = {
  jailbreak: { name: 'Jailbreak', source: 'Prompt', enabled: true, blocking: true }
  protectedText: { name: 'Protected Material Text', source: 'Completion', enabled: true, blocking: true }
  protectedCode: { name: 'Protected Material Code', source: 'Completion', enabled: true, blocking: true }
  profanityPrompt: { name: 'Profanity', source: 'Prompt', enabled: false, blocking: false }
  profanityCompletion: { name: 'Profanity', source: 'Completion', enabled: false, blocking: false }
}

// The two policies differ only in the indirect-attack shield: chat sources are screened per
// source by Prompt Shields before generation, while the table-summary call wraps each table's
// sanitised HTML in a documents block for this shield to inspect.
var policies = [
  {
    name: 'docchat-text-guard'
    indirectAttack: { name: 'Indirect Attack', source: 'Prompt', enabled: false, blocking: false }
  }
  {
    name: 'docchat-table-summary-guard'
    indirectAttack: { name: 'Indirect Attack', source: 'Prompt', enabled: true, blocking: true }
  }
]

var deployments = concat(
  map(textDeployments, deployment => {
    name: deployment.name
    sku: {
      name: deployment.type
      capacity: deployment.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: deployment.model
        version: deployment.version
      }
      versionUpgradeOption: deployment.versionUpgradeOption
      raiPolicyName: deployment.raiPolicyName
    }
  }),
  map(embeddingDeployments, deployment => {
    name: deployment.name
    sku: {
      name: deployment.type
      capacity: deployment.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: deployment.model
        version: deployment.version
      }
      versionUpgradeOption: 'NoAutoUpgrade'
    }
  })
)

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource raiPolicies 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01' = [
  for policy in policies: {
    parent: account
    name: policy.name
    properties: {
      basePolicyName: 'Microsoft.DefaultV2'
      // Synchronous filtering, streamed output included; never Asynchronous_filter or Deferred.
      mode: 'Default'
      contentFilters: concat(harmFilters, [
        shieldFilters.jailbreak
        policy.indirectAttack
        shieldFilters.protectedText
        shieldFilters.protectedCode
        shieldFilters.profanityPrompt
        shieldFilters.profanityCompletion
      ])
      customBlocklists: []
    }
  }
]

@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = [
  for deployment in deployments: {
    parent: account
    name: deployment.name
    sku: deployment.sku
    properties: deployment.properties
    dependsOn: [raiPolicies]
  }
]
