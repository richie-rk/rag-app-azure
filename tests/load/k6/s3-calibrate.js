// S3's calibration (84-testing.md section 7.5): 4 minutes at a constant 5 turns per second plus
// 10 cheap requests per second on one instance. `docchat-load s3-rate` reads its CPU, screening
// calls and request charges and sets the main phase's rate. Every question carries
// `[[stub:tokens=<L>]] [[stub:delay_ms=<d>]]` from DOCCHAT_LOAD_ANSWER_TOKENS and
// DOCCHAT_LOAD_TOKEN_DELAY_MS.
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
const RATE = 5;

export const options = {
  scenarios: {
    turns: {
      executor: 'constant-arrival-rate',
      exec: 'turns',
      rate: RATE,
      timeUnit: '1s',
      duration: '4m',
      preAllocatedVUs: 2 * RATE,
      maxVUs: 6 * RATE,
    },
    cheap: {
      executor: 'constant-arrival-rate',
      exec: 'cheap',
      rate: 10,
      timeUnit: '1s',
      duration: '4m',
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
  },
};

export function setup() {
  requireVariables(['DOCCHAT_LOAD_ANSWER_TOKENS', 'DOCCHAT_LOAD_TOKEN_DELAY_MS']);
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
