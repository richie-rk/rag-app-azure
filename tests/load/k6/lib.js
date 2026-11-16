// The one library of the load scenarios (84-testing.md sections 6.4 and 7.1; ADR 0135): the run
// file loader, the request helper with its single retry of a rate-limited request, the NDJSON
// stream reader and validator, the chat user that runs sessions of five turns, and the marker
// lines the verdict reads. Every scenario script imports only this file, so a scenario runs from
// two flat files in the load generator's work volume.
//
// Markers go to standard output and never carry a header or a token:
//   K6_SUMMARY_JSON <json>      from handleSummary: every threshold's ok and limit_retries
//   K6_FAILED_REQUEST <json>    {traceId, startedAt, endedAt, status, reason}
//   K6_SESSION <json>           {sessionId, traceIds}
import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate } from 'k6/metrics';

export const LOAD_USERS = Array.from(
  { length: 20 },
  (_, index) => `load-${String(index + 1).padStart(2, '0')}`,
);
export const SESSION_USER = 'load-01';
export const PROJECT_NAME = 'Load test';
export const TURNS_PER_SESSION = 5;
export const STREAM_TIMEOUT = '240s';
export const REQUEST_TIMEOUT = '60s';
export const NDJSON = 'application/x-ndjson';

const RETRIED_CODES = ['too_many_requests', 'write_rate_limited'];
const MAX_RETRY_WAIT_SECONDS = 120;
const EVENT_TYPES = ['meta', 'status', 'delta', 'citations', 'followups', 'heartbeat', 'done', 'error'];

export const limitRetries = new Counter('limit_retries');
// The built-in metric. A first 429 is marked expected so that a retried one never counts; one
// that isn't retried still fails its request, so it adds its own failed sample here.
const requestFailed = new Rate('http_req_failed');

const FIRST_ATTEMPT = http.expectedStatuses({ min: 200, max: 399 }, 429);
const RETRY_ATTEMPT = http.expectedStatuses({ min: 200, max: 399 });

// --- The run file ------------------------------------------------------------------------------

// The run file `docchat-load aci-params` packs as tokens.json: the API's base URL, the load
// users' access tokens and the load questions. Read in the init stage only.
export function parseRun(text) {
  const run = JSON.parse(text);
  if (run === null || typeof run !== 'object') {
    throw new Error('the run file is not a JSON object');
  }
  if (typeof run.apiBaseUrl !== 'string' || run.apiBaseUrl === '') {
    throw new Error('the run file names no apiBaseUrl');
  }
  if (run.tokens === null || typeof run.tokens !== 'object') {
    throw new Error('the run file holds no tokens');
  }
  if (!Array.isArray(run.questions)) {
    throw new Error('the run file holds no questions');
  }
  return {
    apiBaseUrl: run.apiBaseUrl.replace(/\/+$/, ''),
    tokens: run.tokens,
    questions: run.questions,
  };
}

// `k6 inspect` passes no system environment, so the init stage reads the file only when the
// variable is set; a run without it fails in setup (requireRun), and an unreadable file fails
// the init stage. Either way k6 exits non-zero and the verdict fails.
export function loadRun() {
  const path = __ENV.DOCCHAT_LOAD_TOKENS_FILE;
  return path ? parseRun(open(path)) : null;
}

export function requireRun(run) {
  if (run === null) {
    throw new Error('DOCCHAT_LOAD_TOKENS_FILE is not set');
  }
  return run;
}

// The scenario's own variables, checked in setup for the same reason.
export function requireVariables(names) {
  const missing = names.filter((name) => !__ENV[name]);
  if (missing.length > 0) {
    throw new Error(`not set: ${missing.join(', ')}`);
  }
}

export function tokenOf(run, alias) {
  const token = run.tokens[alias];
  if (typeof token !== 'string' || token === '') {
    throw new Error(`the run file holds no token for ${alias}`);
  }
  return token;
}

// `[[stub:tokens=<L>]] [[stub:delay_ms=<d>]]` when the S3 jobs set the answer length, else empty.
export function stubSuffix() {
  const tokens = __ENV.DOCCHAT_LOAD_ANSWER_TOKENS;
  const delay = __ENV.DOCCHAT_LOAD_TOKEN_DELAY_MS;
  const parts = [];
  if (tokens) {
    parts.push(`[[stub:tokens=${tokens}]]`);
  }
  if (delay) {
    parts.push(`[[stub:delay_ms=${delay}]]`);
  }
  return parts.join(' ');
}

// --- Markers -----------------------------------------------------------------------------------

export function printMarker(name, payload) {
  console.log(`${name} ${JSON.stringify(payload)}`);
}

export function reportFailure(result, reason) {
  printMarker('K6_FAILED_REQUEST', {
    traceId: result.traceId,
    startedAt: result.startedAt,
    endedAt: result.endedAt,
    status: result.response.status,
    reason,
  });
}

// handleSummary's output: every threshold's ok by metric and expression, each thresholded
// metric's values, and the limit_retries count. Nothing else is printed at the end of a run.
export function summaryMarker(data) {
  const thresholds = {};
  const values = {};
  for (const [name, metric] of Object.entries(data.metrics)) {
    if (metric.thresholds) {
      thresholds[name] = {};
      for (const [expression, outcome] of Object.entries(metric.thresholds)) {
        thresholds[name][expression] = outcome.ok === true;
      }
      values[name] = metric.values;
    }
  }
  const retries = data.metrics.limit_retries;
  const summary = {
    thresholds,
    values,
    limitRetries: retries ? retries.values.count : 0,
  };
  return { stdout: `K6_SUMMARY_JSON ${JSON.stringify(summary)}\n` };
}

// --- Requests ----------------------------------------------------------------------------------

function randomHex(bytes) {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return Array.from(buffer, (value) => value.toString(16).padStart(2, '0')).join('');
}

// A fresh sampled W3C traceparent; the trace ID is what the verdict maps to AppRequests rows.
export function newTraceparent() {
  const traceId = randomHex(16);
  return { traceId, header: `00-${traceId}-${randomHex(8)}-01` };
}

// The seconds before the one retry of `response`, or null when it isn't retried: only a 429
// problem whose code is too_many_requests or write_rate_limited with a positive integer
// retryAfterSeconds, waiting at most 120 s.
export function retryWait(response) {
  if (response.status !== 429) {
    return null;
  }
  let problem;
  try {
    problem = JSON.parse(response.body);
  } catch (error) {
    return null;
  }
  if (problem === null || typeof problem !== 'object' || !RETRIED_CODES.includes(problem.code)) {
    return null;
  }
  const seconds = problem.retryAfterSeconds;
  if (!Number.isInteger(seconds) || seconds < 1) {
    return null;
  }
  return Math.min(seconds, MAX_RETRY_WAIT_SECONDS);
}

function attempt(method, url, options, responseCallback) {
  const trace = newTraceparent();
  const headers = Object.assign({ traceparent: trace.header }, options.headers || {});
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  let body = null;
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.body);
  }
  const startedAt = new Date().toISOString();
  const response = http.request(method, url, body, {
    headers,
    responseCallback,
    redirects: 0,
    timeout: options.timeout || REQUEST_TIMEOUT,
    tags: Object.assign({ name: options.name || url }, options.tags || {}),
  });
  return { response, traceId: trace.traceId, startedAt, endedAt: new Date().toISOString() };
}

// One API call with the bearer token and a fresh traceparent (84-testing.md section 6.4 item 7):
// a 429 too_many_requests or write_rate_limited is retried once after retryAfterSeconds and
// counted in limit_retries; a second 429, or a 429 with any other code, is the call's outcome.
export function apiRequest(method, url, options = {}) {
  const first = attempt(method, url, options, FIRST_ATTEMPT);
  if (first.response.status !== 429) {
    return first;
  }
  const wait = retryWait(first.response);
  if (wait === null) {
    requestFailed.add(true, options.tags || {});
    return first;
  }
  limitRetries.add(1, options.tags || {});
  sleep(wait);
  return attempt(method, url, options, RETRY_ATTEMPT);
}

// --- Streams -----------------------------------------------------------------------------------

// Reads a buffered NDJSON chat stream. It fails when the status isn't 200, a line isn't valid
// JSON or has an unknown type, the first event isn't meta, the stream holds an error event, it
// doesn't end with exactly one done, or citations don't precede done.
export function readStream(response) {
  const result = {
    status: response.status === 200,
    lines: false,
    meta: false,
    noError: false,
    done: false,
    citations: false,
    sessionId: null,
    reason: null,
  };
  if (!result.status) {
    result.reason = `status_${response.status}`;
    return result;
  }
  const text = typeof response.body === 'string' ? response.body : '';
  const lines = text.split('\n');
  if (lines.length > 0 && lines[lines.length - 1] === '') {
    lines.pop();
  }
  const events = [];
  result.lines = lines.length > 0;
  for (const line of lines) {
    let event;
    try {
      event = JSON.parse(line);
    } catch (error) {
      result.lines = false;
      break;
    }
    if (event === null || typeof event !== 'object' || !EVENT_TYPES.includes(event.type)) {
      result.lines = false;
      break;
    }
    events.push(event);
  }
  const types = events.map((event) => event.type);
  const doneAt = types.indexOf('done');
  const citationsAt = types.indexOf('citations');
  result.meta = types[0] === 'meta';
  result.noError = !types.includes('error');
  result.done = doneAt >= 0 && doneAt === types.length - 1 && types.lastIndexOf('done') === doneAt;
  result.citations = citationsAt >= 0 && doneAt >= 0 && citationsAt < doneAt;
  if (result.meta && typeof events[0].sessionId === 'string') {
    result.sessionId = events[0].sessionId;
  }
  if (!result.lines) {
    result.reason = 'invalid_line';
  } else if (!result.meta) {
    result.reason = 'first_event_not_meta';
  } else if (!result.noError) {
    result.reason = 'error_event';
  } else if (!result.done) {
    result.reason = 'done_not_terminal';
  } else if (!result.citations) {
    result.reason = 'citations_missing';
  }
  return result;
}

export function streamChecks(stream) {
  return check(stream, {
    'status is 200': (s) => s.status,
    'every line is JSON with a known type': (s) => s.lines,
    'first event is meta': (s) => s.meta,
    'no error event': (s) => s.noError,
    'exactly one terminal event and it is done': (s) => s.done,
    'citations precede done': (s) => s.citations,
  });
}

// --- Operations --------------------------------------------------------------------------------

// The project `Load test` that `docchat-load seed` created, as load-01 sees it.
export function findProject(loaded) {
  const run = requireRun(loaded);
  const result = apiRequest('GET', `${run.apiBaseUrl}/v1/projects?limit=100`, {
    token: tokenOf(run, SESSION_USER),
    name: 'listMyProjects',
  });
  if (result.response.status !== 200) {
    throw new Error(`listMyProjects answered ${result.response.status}`);
  }
  const project = JSON.parse(result.response.body).items.find((item) => item.name === PROJECT_NAME);
  if (!project) {
    throw new Error(`project ${PROJECT_NAME} is missing; run docchat-load seed first`);
  }
  return { projectId: project.projectId };
}

// getCurrentUser as the next load user, round-robin over the 20, so no user sends more than
// one request a second at the cheap rates.
export function cheapRequest(run) {
  const alias = LOAD_USERS[exec.scenario.iterationInTest % LOAD_USERS.length];
  const result = apiRequest('GET', `${run.apiBaseUrl}/v1/me`, {
    token: tokenOf(run, alias),
    name: 'getCurrentUser',
  });
  if (result.response.status !== 200) {
    reportFailure(result, `status_${result.response.status}`);
  }
  return result;
}

// listSessions as load-01, which seeding gave 500 sessions.
export function sessionsRequest(run, projectId) {
  const result = apiRequest(
    'GET',
    `${run.apiBaseUrl}/v1/sessions?projectId=${projectId}&limit=50`,
    { token: tokenOf(run, SESSION_USER), name: 'listSessions' },
  );
  if (result.response.status !== 200) {
    reportFailure(result, `status_${result.response.status}`);
  }
  return result;
}

// A virtual user's chat: one turn per iteration, five turns to a session. A failed turn ends its
// session, because the turn may still be active on the server and a further turn would be
// refused for a reason the scenario isn't measuring.
export class ChatUser {
  constructor(run, projectId, options = {}) {
    const vu = exec.vu.idInTest;
    this.run = run;
    this.projectId = projectId;
    this.alias = LOAD_USERS[(vu - 1) % LOAD_USERS.length];
    this.token = tokenOf(run, this.alias);
    this.next = (vu * 7) % Math.max(run.questions.length, 1);
    this.suffix = options.suffix || '';
    this.recordSessions = options.recordSessions === true;
    this.resetSession();
  }

  resetSession() {
    this.sessionId = null;
    this.turns = 0;
    this.traceIds = [];
  }

  endSession() {
    if (this.recordSessions && this.traceIds.length > 0) {
      printMarker('K6_SESSION', { sessionId: this.sessionId, traceIds: this.traceIds });
    }
    this.resetSession();
  }

  question() {
    const text = this.run.questions[this.next % this.run.questions.length];
    this.next += 1;
    return this.suffix ? `${text} ${this.suffix}` : text;
  }

  turn() {
    const result = apiRequest('POST', `${this.run.apiBaseUrl}/v1/chat/turns`, {
      token: this.token,
      name: 'createChatTurn',
      timeout: STREAM_TIMEOUT,
      headers: { Accept: NDJSON },
      body: {
        projectId: this.projectId,
        sessionId: this.sessionId,
        clientTurnId: crypto.randomUUID(),
        question: this.question(),
      },
    });
    const stream = readStream(result.response);
    streamChecks(stream);
    this.traceIds.push(result.traceId);
    if (this.sessionId === null && stream.sessionId !== null) {
      this.sessionId = stream.sessionId;
    }
    this.turns += 1;
    if (stream.reason !== null) {
      reportFailure(result, stream.reason);
      this.endSession();
    } else if (this.turns >= TURNS_PER_SESSION) {
      this.endSession();
    }
    return { result, stream };
  }
}
