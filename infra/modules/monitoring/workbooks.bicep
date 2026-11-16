// The environment's nine Azure Workbooks (85-observability.md section 5.1): availability, ttft,
// openai, ingestion, quota, budget, capacity, conversations and access.
// A workbook JSON file that doesn't parse fails the Bicep build, so a broken workbook never
// deploys.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param location string
param tags object

@description('Resource ID of log-docchat-<env>, each workbook\'s source.')
param workspaceId string

@description('Resource ID of the API site, whose platform metrics the availability tiles read.')
param apiSiteId string

@description('Resource IDs the capacity tiles read; dev has no autoscale setting, and its tile stays empty there.')
param apiPlanId string
param autoscaleId string

@description('Resource ID of budget-docchat-<env>, or empty where budget.enabled is false.')
param budgetId string

@description('Principal ID of the API identity: the conversations tile of human access lists every other Cosmos DB caller.')
param apiPrincipalId string

var placeholderValues = {
  '{workspaceId}': workspaceId
  '{apiSiteId}': apiSiteId
  '{apiPlanId}': apiPlanId
  '{autoscaleId}': autoscaleId
  '{budgetId}': budgetId
  '{apiPrincipalId}': apiPrincipalId
}

var workbooks = [
  {
    key: 'availability'
    title: 'API availability and 5xx rate'
    content: string(loadJsonContent('workbooks/availability.workbook.json'))
  }
  {
    key: 'ttft'
    title: 'Time to first token'
    content: string(loadJsonContent('workbooks/ttft.workbook.json'))
  }
  {
    key: 'openai'
    title: 'Azure OpenAI 429s and latency'
    content: string(loadJsonContent('workbooks/openai.workbook.json'))
  }
  {
    key: 'ingestion'
    title: 'Ingestion failures and queue age'
    content: string(loadJsonContent('workbooks/ingestion.workbook.json'))
  }
  {
    key: 'quota'
    title: 'Quota and rate-limit rejections'
    content: string(loadJsonContent('workbooks/quota.workbook.json'))
  }
  {
    key: 'capacity'
    title: 'Instances, CPU, event loop, SQL and Cosmos DB'
    content: string(loadJsonContent('workbooks/capacity.workbook.json'))
  }
  {
    key: 'budget'
    title: 'Monthly budget and estimated model cost'
    content: string(loadJsonContent('workbooks/budget.workbook.json'))
  }
  {
    key: 'conversations'
    title: 'Conversations'
    content: string(loadJsonContent('workbooks/conversations.workbook.json'))
  }
  {
    key: 'access'
    title: 'Access'
    content: string(loadJsonContent('workbooks/access.workbook.json'))
  }
]

resource workbookResources 'Microsoft.Insights/workbooks@2023-06-01' = [
  for workbook in workbooks: {
    name: guid(resourceGroup().id, 'docchat-workbook', workbook.key)
    location: location
    tags: tags
    kind: 'shared'
    properties: {
      displayName: 'DocChat ${environmentName} - ${workbook.title}'
      category: 'workbook'
      sourceId: workspaceId
      serializedData: reduce(
        items(placeholderValues),
        workbook.content,
        (text, placeholder) => replace(text, placeholder.key, placeholder.value)
      )
    }
  }
]
