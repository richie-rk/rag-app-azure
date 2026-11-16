// The load generator of one scenario (84-testing.md section 7.2; ADR 0135): a container group in
// rg-docchat-load with no virtual network, no inbound address and no identity, created and deleted
// by the scenario's own job as id-docchat-citest. Its one container runs the bootstrap run.py in
// place of the image's entrypoint, so the Functions host of the image never starts; run.py fetches
// the locked k6 release and runs the scenario. Everything the run needs arrives in the read-only,
// tmpfs-backed secret volume `work`, so the tokens never reach an environment variable or a disk.
// `docchat-load aci-params` writes the parameters file.

targetScope = 'resourceGroup'

@description('ci-docchat-loadgen-<scenario>-<run number>')
@minLength(1)
@maxLength(63)
param name string

@description('The load environment\'s region, so the client-side p95 measures the API, not the internet path.')
param location string

@description('The final FROM reference of apps/ingestion/Dockerfile: mcr.microsoft.com/azure-functions/python:<tag>@sha256:<digest>.')
param image string

@description('K6_URL: the url of tests/load/k6.lock.json.')
param k6Url string

@description('K6_SHA256: the sha256 of tests/load/k6.lock.json.')
param k6Sha256 string

@description('K6_MEMBER: the member of tests/load/k6.lock.json.')
param k6Member string

@description('The scenario\'s non-secret variables (DOCCHAT_LOAD_ANSWER_TOKENS, DOCCHAT_LOAD_TOKEN_DELAY_MS, DOCCHAT_LOAD_RATE), name to value.')
param scenarioEnv object = {}

@secure()
@description('tests/load/aci/run.py, Base64.')
param runPyBase64 string

@secure()
@description('The scenario script, Base64; mounted as /work/scenario.js.')
param scenarioJsBase64 string

@secure()
@description('tests/load/k6/lib.js, Base64.')
param libJsBase64 string

@secure()
@description('The run file (API base URL, the load users\' tokens and the load questions), Base64.')
param tokensJsonBase64 string

var workMount = '/work'

var environmentVariables = concat(
  [
    { name: 'K6_URL', value: k6Url }
    { name: 'K6_SHA256', value: k6Sha256 }
    { name: 'K6_MEMBER', value: k6Member }
    { name: 'DOCCHAT_LOAD_TOKENS_FILE', value: '${workMount}/tokens.json' }
  ],
  map(items(scenarioEnv), variable => { name: variable.key, value: string(variable.value) })
)

resource loadgen 'Microsoft.ContainerInstance/containerGroups@2026-07-01' = {
  name: name
  location: location
  tags: {
    'docchat-env': 'load'
  }
  properties: {
    osType: 'Linux'
    sku: 'Standard'
    // The bootstrap always exits 0, because the platform may restart a container that exits
    // nonzero even under Never, which would run the scenario twice.
    restartPolicy: 'Never'
    containers: [
      {
        name: 'k6'
        properties: {
          image: image
          command: [
            'python'
            '${workMount}/run.py'
          ]
          resources: {
            requests: {
              cpu: 4
              memoryInGB: 8
            }
          }
          environmentVariables: environmentVariables
          volumeMounts: [
            {
              name: 'work'
              mountPath: workMount
              readOnly: true
            }
          ]
        }
      }
    ]
    volumes: [
      {
        name: 'work'
        secret: {
          'run.py': runPyBase64
          'scenario.js': scenarioJsBase64
          'lib.js': libJsBase64
          'tokens.json': tokensJsonBase64
        }
      }
    ]
  }
}

output name string = loadgen.name
