// S2, statelessness (84-testing.md section 7.6; T-592): any instance serves any request, and
// stopping one instance fails only the streams it held. The workflow lowers the API's capacity
// from 3 to 2 at T0 + 240 s; the verdict maps every failed request and every session's turns to
// the instances that served them, so the script records sessions and sets no thresholds.
import { ChatUser, cheapRequest, findProject, loadRun, summaryMarker } from './lib.js';

const run = loadRun();

export const options = {
  scenarios: {
    streams: {
      executor: 'ramping-vus',
      exec: 'streams',
      startVUs: 0,
      stages: [
        { duration: '60s', target: 60 },
        { duration: '8m', target: 60 },
      ],
      gracefulRampDown: '30s',
      gracefulStop: '30s',
    },
    cheap: {
      executor: 'constant-arrival-rate',
      exec: 'cheap',
      rate: 10,
      timeUnit: '1s',
      duration: '9m',
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
  },
};

export function setup() {
  return findProject(run);
}

let user = null;

export function streams(data) {
  if (user === null) {
    user = new ChatUser(run, data.projectId, { recordSessions: true });
  }
  user.turn();
}

export function cheap() {
  cheapRequest(run);
}

export function handleSummary(data) {
  return summaryMarker(data);
}
