// The environment budget on the GA Budgets API (83-infrastructure.md section 12.7; ADR 0097).
// The AVM budget module isn't used because it recomputes its start date on every deployment,
// while a budget keeps the period it was created with.

@allowed(['dev', 'prod', 'load'])
param environmentName string

@description('budget.amount of the environment file, in the billing currency.')
param amount int

@description('yyyy-MM-01: the existing budget\'s start date, which the preprovision hook reads, or the current month.')
param startDate string

param actionGroupId string

var thresholds = [50, 80, 100]

resource budget 'Microsoft.Consumption/budgets@2026-06-01' = {
  name: 'budget-docchat-${environmentName}'
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: toObject(
      thresholds,
      threshold => 'actual-${threshold}',
      threshold => {
        enabled: true
        operator: 'GreaterThan'
        threshold: threshold
        thresholdType: 'Actual'
        contactGroups: [actionGroupId]
        contactEmails: []
        locale: 'en-us'
      }
    )
  }
}

output id string = budget.id
