// S1, the event loop (84-testing.md section 7.6; T-591): 100 concurrent streams on one instance
// of the smallest production size with zero errors, while the cheap endpoint's p95 stays at or
// under 200 ms and load-01's session list answers within the same bound.
import {
  ChatUser,
  cheapRequest,
  findProject,
  loadRun,
  sessionsRequest,
  summaryMarker,
} from './lib.js';

const run = loadRun();

export const options = {
  scenarios: {
    // Each stream takes about 11 s: 800 ms to the first token, 400 tokens at 25 ms, plus the
    // pipeline's own time.
    streams: {
      executor: 'ramping-vus',
      exec: 'streams',
      startVUs: 0,
      stages: [
        { duration: '60s', target: 100 },
        { duration: '9m', target: 100 },
      ],
      gracefulRampDown: '30s',
      gracefulStop: '30s',
    },
    cheap: {
      executor: 'constant-arrival-rate',
      exec: 'cheap',
      rate: 20,
      timeUnit: '1s',
      startTime: '1m',
      duration: '9m',
      preAllocatedVUs: 20,
      maxVUs: 60,
    },
    sessions: {
      executor: 'constant-arrival-rate',
      exec: 'sessions',
      rate: 1,
      timeUnit: '1s',
      startTime: '2m',
      duration: '7m',
      preAllocatedVUs: 2,
      maxVUs: 5,
    },
  },
  thresholds: {
    'http_req_failed{scenario:streams}': ['rate==0'],
    'checks{scenario:streams}': ['rate==1'],
    'http_req_duration{scenario:cheap}': ['p(95)<=200'],
    'http_req_failed{scenario:cheap}': ['rate==0'],
    'http_req_duration{scenario:sessions}': ['p(95)<=200'],
    'http_req_failed{scenario:sessions}': ['rate==0'],
  },
};

export function setup() {
  return findProject(run);
}

let user = null;

export function streams(data) {
  if (user === null) {
    user = new ChatUser(run, data.projectId);
  }
  user.turn();
}

export function cheap() {
  cheapRequest(run);
}

export function sessions(data) {
  sessionsRequest(run, data.projectId);
}

export function handleSummary(data) {
  return summaryMarker(data);
}
