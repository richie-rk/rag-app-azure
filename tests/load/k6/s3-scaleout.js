// S3's main phase, scale-out (84-testing.md section 7.6; T-593): R turns per second
// (DOCCHAT_LOAD_RATE, 5 to 60, from `docchat-load s3-rate`) plus 20 cheap requests per second for
// 15 minutes, starting on one instance; autoscale must add one while the cheap endpoint's p95
// stays at or under 200 ms and no request fails.
import {
  ChatUser,
  cheapRequest,
  findProject,
  loadRun,
  requireVariables,
  stubSuffix,
  summaryMarker,
} from './lib.js';

const run = loadRun();

// k6 inspect sees no system environment, so the options fall back to the calibration rate and
// setup refuses a missing or out-of-range DOCCHAT_LOAD_RATE.
function isRate(value) {
  return Number.isInteger(value) && value >= 5 && value <= 60;
}

const RATE = isRate(Number(__ENV.DOCCHAT_LOAD_RATE)) ? Number(__ENV.DOCCHAT_LOAD_RATE) : 5;

export const options = {
  scenarios: {
    turns: {
      executor: 'constant-arrival-rate',
      exec: 'turns',
      rate: RATE,
      timeUnit: '1s',
      duration: '15m',
      preAllocatedVUs: 2 * RATE,
      maxVUs: 6 * RATE,
    },
    cheap: {
      executor: 'constant-arrival-rate',
      exec: 'cheap',
      rate: 20,
      timeUnit: '1s',
      duration: '15m',
      preAllocatedVUs: 20,
      maxVUs: 60,
    },
  },
  thresholds: {
    'http_req_duration{scenario:cheap}': ['p(95)<=200'],
    'http_req_failed{scenario:cheap}': ['rate==0'],
    'http_req_failed{scenario:turns}': ['rate==0'],
  },
};

export function setup() {
  requireVariables(['DOCCHAT_LOAD_RATE', 'DOCCHAT_LOAD_ANSWER_TOKENS', 'DOCCHAT_LOAD_TOKEN_DELAY_MS']);
  if (!isRate(Number(__ENV.DOCCHAT_LOAD_RATE))) {
    throw new Error('DOCCHAT_LOAD_RATE must be an integer from 5 to 60');
  }
  return findProject(run);
}

let user = null;

export function turns(data) {
  if (user === null) {
    user = new ChatUser(run, data.projectId, { suffix: stubSuffix() });
  }
  user.turn();
}

export function cheap() {
  cheapRequest(run);
}

export function handleSummary(data) {
  return summaryMarker(data);
}
