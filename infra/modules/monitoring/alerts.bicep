// The application alerts A01 to A43 and A58 of 85-observability.md section 6: 41 rules in dev, 44
// in prod, none in load. A01 and A02 are metric rules on the API site; every other rule is a log
// search rule over the environment's workspace. Each is named docchat-<env>-<slug>, with the ops
// action group as its only action. No rule exists where alerts.enabled is false (load, where the
// tests cause alert conditions on purpose); A11 and A40 exist only in prod, and A15 only where
// storage.malwareScanning is true.
//
// A rule with a mute is an event alert: it doesn't resolve itself and stays quiet for the mute
// after firing. A rule without one is a state alert and resolves itself.

@allowed(['dev', 'prod', 'load'])
param environmentName string
param location string
param tags object
param workspaceId string
param actionGroupId string

@description('alerts.enabled of the environment file.')
param alertsEnabled bool

@description('storage.malwareScanning of the environment file: the scan-gate alert A15 exists only while scanning is on.')
param malwareScanning bool

@description('Resource ID of the API site, the metric rules\' scope.')
param apiSiteId string

@description('Resource ID of the API plan, whose CPU A34 reads.')
param apiPlanId string

@description('api.maxInstances of the environment file: A34 fires only at the plan\'s ceiling.')
param apiMaxInstances int

@description('Principal ID of the API identity (the azd output DOCCHAT_API_IDENTITY_PRINCIPAL_ID): A29 fires on every other Cosmos DB caller.')
param apiPrincipalId string

// The only environment thresholds: time to first token and model latency depend on the models
// chosen, so each environment file sets them.
@description('alerts.ttftP95Ms of the environment file, A06\'s threshold.')
param ttftP95Ms int

@description('alerts.openAiFirstChunkP95Ms of the environment file, A41\'s threshold.')
param openAiFirstChunkP95Ms int

@description('alerts.openAiCallMeanMs of the environment file, A42\'s threshold.')
param openAiCallMeanMs int

var ingestionRunbook = 'Ingestion failures and stuck runs'
var incidentRunbook = 'Incident response'
var modelRunbook = 'Model version upgrade'
var opsConfigRunbook = 'Change an ops-config value'
var conversationRunbook = 'Read a user\'s conversation; access review'

// HealthCheckStatus is the share of healthy instances: below 1 none is healthy, below 100 at least
// one isn't.
var metricRules = [
  {
    id: 'A01'
    slug: 'api-down'
    severity: 1
    windowSize: 'PT5M'
    evaluationFrequency: 'PT1M'
    threshold: 1
    runbook: incidentRunbook
  }
  {
    id: 'A02'
    slug: 'api-instance-unhealthy'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    threshold: 100
    runbook: incidentRunbook
  }
]

// Rate rules divide only after their minimum-volume guard, so a quiet window never fires on one
// failure.
var apiRules = [
  {
    id: 'A03'
    slug: 'api-5xx-high'
    severity: 1
    windowSize: 'PT5M'
    evaluationFrequency: 'PT1M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppRequests | where AppRoleName == "docchat-api" | summarize total = sum(ItemCount), failed = sumif(ItemCount, toint(ResultCode) >= 500) | where total >= 20 and failed * 10 > total'
  }
  {
    id: 'A04'
    slug: 'api-5xx-elevated'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppRequests | where AppRoleName == "docchat-api" | summarize total = sum(ItemCount), failed = sumif(ItemCount, toint(ResultCode) >= 500) | where total >= 50 and failed * 100 > total'
  }
  {
    id: 'A05'
    slug: 'user-provisioning-failures'
    severity: 2
    windowSize: 'PT5M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'let total = toscalar(AppRequests | where AppRoleName == "docchat-api" and Name != "GET /healthz" | summarize sum(ItemCount)); AppDependencies | where AppRoleName == "docchat-api" and Name == "docchat.users.ensure" and tostring(Properties["docchat.users.outcome"]) == "failed" | summarize failed = sum(ItemCount) | where total >= 50 and failed * 100 > total'
  }
  {
    id: 'A23'
    slug: 'telemetry-silent'
    severity: 2
    windowSize: 'PT30M'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: incidentRunbook
    query: 'datatable(role: string) ["docchat-api", "docchat-ingestion"] | join kind=leftouter (AppMetrics | summarize n = count() by role = AppRoleName) on role | where isnull(n) or n == 0'
  }
  {
    id: 'A24'
    slug: 'audit-write-failures'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.audit.write_failures" | summarize n = sum(Sum) | where n > 0'
  }
  {
    id: 'A34'
    slug: 'api-capacity-exhausted'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'let instances = toscalar(AppRequests | where AppRoleName == "docchat-api" | summarize dcount(AppRoleInstance)); AzureMetrics | where ResourceId =~ "${apiPlanId}" and MetricName == "CpuPercentage" | summarize cpu = avg(Average) | where cpu > 85 and instances >= ${apiMaxInstances}'
  }
  {
    id: 'A35'
    slug: 'signing-key-fetch-failures'
    severity: 2
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppEvents | where Name == "signing_keys_refresh_failed" | summarize n = count() | where n >= 3'
  }
  {
    id: 'A36'
    slug: 'signing-key-age'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.auth.signing_key_age" | summarize age = max(Sum) by AppRoleInstance | where age > 14400'
  }
  {
    id: 'A37'
    slug: 'role-not-assigned'
    severity: 3
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: 'First admins and test identities'
    query: 'AppMetrics | where Name == "docchat.access.denials" and tostring(Properties["kind"]) == "role_not_assigned" | summarize n = sum(Sum) | where n > 30'
  }
  {
    id: 'A38'
    slug: 'invalid-token-share'
    severity: 3
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.auth.validations" | summarize total = sum(Sum), invalid = sumif(Sum, tostring(Properties["outcome"]) == "invalid") | where total >= 100 and invalid * 20 > total'
  }
  {
    id: 'A39'
    slug: 'ops-config-invalid'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: opsConfigRunbook
    query: 'AppMetrics | where Name == "docchat.config.invalid" | summarize n = sum(Sum) by area = tostring(Properties["area"]), key = tostring(Properties["key"]) | where n > 0'
  }
  {
    id: 'A43'
    slug: 'request-limit-rejections'
    severity: 3
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.ratelimit.rejections" and tostring(Properties["reason"]) in ("api", "write") | summarize n = sum(Sum) | where n > 200'
  }
]

// The chat path. A06 and A41 read span attributes, so they see sampled traces only, and their
// volume guards count sampled turns.
var chatRules = [
  {
    id: 'A06'
    slug: 'ttft-p95'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: modelRunbook
    query: 'AppDependencies | where AppRoleName == "docchat-api" and Name == "docchat.chat.turn" | extend ttft = todouble(Properties["docchat.ttft_ms"]) | where isnotnull(ttft) | summarize turns = count(), p95 = percentile(ttft, 95) | where turns >= 10 and p95 > ${ttftP95Ms}'
  }
  {
    id: 'A07'
    slug: 'chat-failed-turns'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.chat.turns" | summarize total = sum(Sum), failed = sumif(Sum, tostring(Properties["outcome"]) == "failed") | where total >= 20 and failed * 20 > total'
  }
  {
    id: 'A08'
    slug: 'chat-openai-throttling'
    severity: 2
    windowSize: 'PT10M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: modelRunbook
    query: 'let calls = toscalar(AppMetrics | where Name == "docchat.openai.duration" | summarize sum(ItemCount)); AppMetrics | where Name == "docchat.openai.throttled" | summarize throttled = sum(Sum) | where calls >= 20 and throttled * 20 > calls'
  }
  {
    id: 'A09'
    slug: 'screening-failures'
    severity: 2
    windowSize: 'PT10M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'let turns = toscalar(AppMetrics | where Name == "docchat.chat.turns" | summarize sum(Sum)); AppMetrics | where Name == "docchat.chat.screening.failures" | summarize failures = sum(Sum) | where turns >= 20 and failures * 50 > turns'
  }
  {
    id: 'A10'
    slug: 'usage-recording-failures'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.usage.record_failures" | summarize n = sum(Sum) | where n > 0'
  }
  {
    // More than a few quota rejections are a sign of a misconfigured quota.
    id: 'A12'
    slug: 'quota-rejections'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: opsConfigRunbook
    query: 'AppMetrics | where Name == "docchat.ratelimit.rejections" and tostring(Properties["reason"]) in ("quota_day", "quota_month") | summarize n = sum(Sum) | where n > 20'
  }
  {
    id: 'A13'
    slug: 'rate-rejections-share'
    severity: 3
    windowSize: 'PT30M'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: opsConfigRunbook
    query: 'let chats = toscalar(AppRequests | where AppRoleName == "docchat-api" and Name == "POST /v1/chat/turns" | summarize sum(ItemCount)); AppMetrics | where Name == "docchat.ratelimit.rejections" and tostring(Properties["reason"]) in ("rate", "concurrency") | summarize n = sum(Sum) | where chats >= 50 and n * 10 > chats'
  }
  {
    id: 'A14'
    slug: 'content-filter-volume'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT1H'
    mute: 'PT1H'
    runbook: incidentRunbook
    query: 'AppEvents | where Name == "content_filter_triggered" | summarize n = count() | where n > 20'
  }
  {
    // The answer call's own wait for its first chunk, which A06's end-to-end time to first token
    // can't separate from search, screening and assembly.
    id: 'A41'
    slug: 'openai-first-chunk-latency'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: modelRunbook
    query: 'AppDependencies | where AppRoleName == "docchat-api" and Name == "docchat.chat.generate" | extend firstChunk = todouble(Properties["docchat.first_chunk_ms"]) | where isnotnull(firstChunk) | summarize calls = count(), p95 = percentile(firstChunk, 95) by deployment = tostring(Properties["docchat.deployment"]) | where calls >= 20 and p95 > ${openAiFirstChunkP95Ms}'
  }
  {
    // Every model call other than the streamed answer: rewrite, search passage, follow-ups,
    // embeddings, summary and memory.
    id: 'A42'
    slug: 'openai-call-latency'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: modelRunbook
    query: 'AppMetrics | where Name == "docchat.openai.duration" and tostring(Properties["outcome"]) == "ok" and tostring(Properties["call"]) != "answer" | summarize calls = sum(ItemCount), meanMs = sum(Sum) / sum(ItemCount) by deployment = tostring(Properties["deployment"]), call = tostring(Properties["call"]) | where calls >= 20 and meanMs > ${openAiCallMeanMs}'
  }
]

// The conversation store: the work after the response, the turn path and human access to
// conversation data.
var conversationRules = [
  {
    id: 'A25'
    slug: 'summary-failures'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: conversationRunbook
    query: 'AppMetrics | where Name == "docchat.summary.rebuilds" | summarize total = sum(Sum), failed = sumif(Sum, tostring(Properties["outcome"]) == "failed") | where total >= 20 and failed * 5 > total'
  }
  {
    id: 'A26'
    slug: 'extraction-failures'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: conversationRunbook
    query: 'AppMetrics | where Name == "docchat.memory.extractions" | summarize total = sum(Sum), failed = sumif(Sum, tostring(Properties["outcome"]) == "failed") | where total >= 20 and failed * 5 > total'
  }
  {
    id: 'A27'
    slug: 'abandoned-turns'
    severity: 2
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: conversationRunbook
    query: 'let finalized = toscalar(AppMetrics | where Name == "docchat.conversations.turns_finalized" | summarize sum(Sum)); AppMetrics | where Name == "docchat.conversations.abandoned" | summarize abandoned = sum(Sum) | where finalized >= 20 and abandoned * 20 > finalized'
  }
  {
    id: 'A28'
    slug: 'client-id-conflicts'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: conversationRunbook
    query: 'AppMetrics | where Name == "docchat.conversations.duplicates" and tostring(Properties["outcome"]) == "conflict" | summarize n = sum(Sum) | where n > 10'
  }
  {
    // Every Cosmos DB caller other than the API identity raises it, an operator's just-in-time read
    // included; the query sets its own 15-minute filter, the rule's window.
    id: 'A29'
    slug: 'human-conversation-access'
    severity: 3
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
    mute: 'PT15M'
    runbook: conversationRunbook
    query: 'CDBDataPlaneRequests | where TimeGenerated > ago(15m) | where DatabaseName == "docchat" | where AadPrincipalId != "${apiPrincipalId}" | summarize requests = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by AadPrincipalId, CollectionName, OperationName'
  }
]

// prod only: the models without a price, before users reach them, and a daily summary of the
// per-user index overrides in use, for matching each to its ticket.
var prodRules = [
  {
    id: 'A11'
    slug: 'prices-missing'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT1H'
    mute: 'P1D'
    runbook: modelRunbook
    query: 'AppMetrics | where Name == "docchat.pricing.rate_missing" | summarize n = sum(Sum) by model = tostring(Properties["model"]) | where n > 0'
  }
  {
    id: 'A40'
    slug: 'index-override-used'
    severity: 3
    windowSize: 'P1D'
    evaluationFrequency: 'P1D'
    mute: 'P1D'
    runbook: 'Per-user index override'
    query: 'AppEvents | where Name == "index_override_used" | summarize uses = count(), users = dcount(tostring(Properties["oid"])) by projectId = tostring(Properties["projectId"]), indexId = tostring(Properties["indexId"])'
  }
]

var rules = [
  {
    id: 'A16'
    slug: 'ingestion-item-failures'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: 'PT15M'
    runbook: ingestionRunbook
    query: 'AppEvents | where Name == "ingestion_item_failed" | summarize n = count() | where n > 5'
  }
  {
    id: 'A17'
    slug: 'ingestion-queue-age'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: ingestionRunbook
    query: 'AppMetrics | where Name == "docchat.ingestion.queue.oldest_age" | summarize maxAge = max(Sum) | where maxAge > 1800'
  }
  {
    id: 'A18'
    slug: 'ingestion-dispatcher-stale'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: ingestionRunbook
    query: 'let queued = toscalar(AppMetrics | where Name == "docchat.ingestion.items.queued" | summarize max(Sum)); AppMetrics | where Name == "docchat.ingestion.dispatcher.heartbeat_age" | summarize age = max(Sum) | where queued > 0 and age > 900'
  }
  {
    id: 'A19'
    slug: 'ingestion-poison-messages'
    severity: 2
    windowSize: 'PT2H'
    evaluationFrequency: 'PT1H'
    mute: ''
    runbook: ingestionRunbook
    query: 'AppMetrics | where Name == "docchat.ingestion.poison_queue.length" | summarize n = max(Sum) | where n > 0'
  }
  {
    id: 'A20'
    slug: 'malware-detected'
    severity: 2
    windowSize: 'PT5M'
    evaluationFrequency: 'PT5M'
    mute: 'PT5M'
    runbook: '${ingestionRunbook}, then Incident response'
    query: 'AppEvents | where Name == "malware_detected" | project TimeGenerated, indexId = tostring(Properties["indexId"]), documentId = tostring(Properties["documentId"]), versionId = tostring(Properties["versionId"])'
  }
  {
    id: 'A21'
    slug: 'ingestion-throttling'
    severity: 3
    windowSize: 'PT5M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: ingestionRunbook
    query: 'AppMetrics | where Name == "docchat.ingestion.throttled" | summarize n = sum(Sum) | where n > 50'
  }
  {
    id: 'A22'
    slug: 'ingestion-run-failed'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
    mute: 'PT15M'
    runbook: ingestionRunbook
    query: 'AppEvents | where Name == "ingestion_run_finished" and tostring(Properties["state"]) == "failed" | project TimeGenerated, runId = tostring(Properties["runId"]), kind = tostring(Properties["kind"])'
  }
  {
    id: 'A30'
    slug: 'event-loop-lag'
    severity: 3
    windowSize: 'PT15M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.runtime.event_loop_lag" | summarize maxLag = max(Max) by AppRoleName, AppRoleInstance, bin(TimeGenerated, 1m) | where maxLag > 500 | summarize blockedMinutes = count() by AppRoleName, AppRoleInstance | where blockedMinutes >= 5'
  }
  {
    id: 'A31'
    slug: 'sql-failures'
    severity: 2
    windowSize: 'PT5M'
    evaluationFrequency: 'PT5M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.sql.failures" | summarize n = sum(Sum) by AppRoleName | where n > 5'
  }
  {
    id: 'A32'
    slug: 'telemetry-scrub-errors'
    severity: 2
    windowSize: 'PT15M'
    evaluationFrequency: 'PT15M'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.telemetry.scrub_errors" | summarize n = sum(Sum) | where n > 0'
  }
  {
    id: 'A33'
    slug: 'telemetry-dropped-attributes'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT1H'
    mute: ''
    runbook: incidentRunbook
    query: 'AppMetrics | where Name == "docchat.telemetry.dropped_attributes" | summarize n = sum(Sum) by AppRoleName | where n > 0'
  }
  {
    id: 'A58'
    slug: 'rerender-failed'
    severity: 3
    windowSize: 'PT1H'
    evaluationFrequency: 'PT1H'
    mute: 'PT1H'
    runbook: ingestionRunbook
    query: 'AppEvents | where Name == "ingestion_item_failed" and tostring(Properties["operation"]) == "rerender" and tostring(Properties["outcomeCode"]) in ("render_failed", "render_too_large") | project TimeGenerated, runId = tostring(Properties["runId"]), itemId = tostring(Properties["itemId"]), outcomeCode = tostring(Properties["outcomeCode"])'
  }
]

// A query that sets its own time filter, as A29's does: no clean canary result in the last 48 hours
// means the upload gate has closed.
var scanningRules = [
  {
    id: 'A15'
    slug: 'uploads-scan-gate-closed'
    severity: 2
    windowSize: 'P2D'
    evaluationFrequency: 'PT1H'
    mute: ''
    runbook: 'Defender acceptance check'
    query: 'AppEvents | where TimeGenerated > ago(2d) and Name == "scanner_canary_result" and tostring(Properties["result"]) == "clean" | summarize lastClean = max(TimeGenerated) | where isnull(lastClean) or lastClean < ago(48h)'
  }
]

var deployedRules = concat(
  apiRules,
  chatRules,
  conversationRules,
  environmentName == 'prod' ? prodRules : [],
  malwareScanning ? scanningRules : [],
  rules
)

resource metricAlerts 'Microsoft.Insights/metricAlerts@2026-01-01' = [
  for rule in metricRules: if (alertsEnabled) {
    name: 'docchat-${environmentName}-${rule.slug}'
    location: 'global'
    tags: tags
    properties: {
      description: '${rule.id} · ${rule.runbook}'
      severity: rule.severity
      enabled: alertsEnabled
      scopes: [apiSiteId]
      evaluationFrequency: rule.evaluationFrequency
      windowSize: rule.windowSize
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allOf: [
          {
            criterionType: 'StaticThresholdCriterion'
            name: 'HealthCheckStatus'
            metricNamespace: 'Microsoft.Web/sites'
            metricName: 'HealthCheckStatus'
            timeAggregation: 'Average'
            operator: 'LessThan'
            threshold: rule.threshold
          }
        ]
      }
      autoMitigate: true
      actions: [
        {
          actionGroupId: actionGroupId
        }
      ]
    }
  }
]

resource logAlerts 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = [
  for rule in deployedRules: if (alertsEnabled) {
    name: 'docchat-${environmentName}-${rule.slug}'
    location: location
    tags: tags
    kind: 'LogAlert'
    properties: union(
      {
        description: '${rule.id} · ${rule.runbook}'
        severity: rule.severity
        enabled: alertsEnabled
        scopes: [workspaceId]
        evaluationFrequency: rule.evaluationFrequency
        windowSize: rule.windowSize
        criteria: {
          allOf: [
            {
              query: rule.query
              timeAggregation: 'Count'
              operator: 'GreaterThan'
              threshold: 0
              failingPeriods: {
                numberOfEvaluationPeriods: 1
                minFailingPeriodsToAlert: 1
              }
            }
          ]
        }
        autoMitigate: empty(rule.mute)
        actions: {
          actionGroups: [actionGroupId]
        }
      },
      empty(rule.mute) ? {} : { muteActionsDuration: rule.mute }
    )
  }
]
