// The library self-test (84-testing.md section 7.1; T-581), run by the lint job against
// scripts/ci/k6_selftest_server.py on 127.0.0.1, whose URL arrives in K6_SELFTEST_URL:
//   K6_SELFTEST_URL=http://127.0.0.1:<port> k6 run --quiet tests/load/k6/selftest.js
// It proves the request helper's rule: one retry after retryAfterSeconds on 429
// too_many_requests and 429 write_rate_limited, counted in limit_retries and absent from
// http_req_failed; no retry on any other 429, which fails its request. It also runs the stream
// validator and the run-file parser over fixed inputs. k6 exits 0 only when every threshold holds.
import http from 'k6/http';
import { check } from 'k6';
import { apiRequest, parseRun, readStream, retryWait, summaryMarker } from './lib.js';

const RETRIED = ['too_many_requests', 'write_rate_limited'];
const REFUSED = [
  'rate_limited',
  'too_many_streams',
  'token_quota_exceeded',
  'too_many_downloads',
  'upload_busy',
];
const CALL = { selftest: 'call' };

function single(exec) {
  return { executor: 'per-vu-iterations', vus: 1, iterations: 1, exec, maxDuration: '60s' };
}

export const options = {
  scenarios: {
    retried: single('retried'),
    refused: Object.assign(single('refused'), { startTime: '5s' }),
    twice: Object.assign(single('twice'), { startTime: '10s' }),
    streams: single('streams'),
  },
  thresholds: {
    checks: ['rate==1'],
    'http_req_failed{scenario:retried,selftest:call}': ['rate==0'],
    'limit_retries{scenario:retried}': ['count==2'],
    'http_req_failed{scenario:refused,selftest:call}': ['rate>0'],
    'limit_retries{scenario:refused}': ['count==0'],
    'http_req_failed{scenario:twice,selftest:call}': ['rate>0'],
    'limit_retries{scenario:twice}': ['count==1'],
  },
};

function baseUrl() {
  const url = __ENV.K6_SELFTEST_URL;
  if (!url) {
    throw new Error('K6_SELFTEST_URL is not set');
  }
  return url.replace(/\/+$/, '');
}

function stats(key) {
  const response = http.get(`${baseUrl()}/stats/${key}`, { tags: { selftest: 'stats' } });
  return JSON.parse(response.body);
}

function key(label) {
  return `${label}-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

export function retried() {
  for (const code of RETRIED) {
    const name = key(code);
    const result = apiRequest('GET', `${baseUrl()}/limit/${code}/${name}?times=1&retryAfter=1`, {
      tags: CALL,
    });
    const seen = stats(name);
    check(
      { result, seen },
      {
        [`${code}: retried once and answered 200`]: (x) =>
          x.result.response.status === 200 && x.seen.requests === 2,
        [`${code}: the retry waited retryAfterSeconds`]: (x) => x.seen.secondsSinceFirst[1] >= 0.95,
      },
    );
  }
}

export function refused() {
  for (const code of REFUSED) {
    const name = key(code);
    const result = apiRequest('POST', `${baseUrl()}/limit/${code}/${name}?times=1&retryAfter=1`, {
      tags: CALL,
      body: {},
    });
    const seen = stats(name);
    check(
      { result, seen },
      {
        [`${code}: not retried, the 429 is the outcome`]: (x) =>
          x.result.response.status === 429 && x.seen.requests === 1,
      },
    );
  }
}

export function twice() {
  const name = key('twice');
  const result = apiRequest('GET', `${baseUrl()}/limit/too_many_requests/${name}?times=2&retryAfter=1`, {
    tags: CALL,
  });
  const seen = stats(name);
  check(
    { result, seen },
    {
      'a second 429 is returned unretried': (x) =>
        x.result.response.status === 429 && x.seen.requests === 2,
    },
  );
}

function stream(lines, status = 200) {
  return { status, body: lines.map((line) => (typeof line === 'string' ? line : JSON.stringify(line))).join('\n') + '\n' };
}

const META = { type: 'meta', sessionId: '0b7f2c4e-8a51-4c3e-9d6f-1f2a3b4c5d6e', turnId: 't' };
const DELTA = { type: 'delta', text: 'x' };
const CITATIONS = { type: 'citations', citations: [] };
const DONE = { type: 'done', turnId: 't', truncated: false };

export function streams() {
  const cases = [
    ['a valid stream', stream([META, { type: 'heartbeat' }, DELTA, CITATIONS, { type: 'followups' }, DONE]), null],
    ['a non-200 status', stream([META], 503), 'status_503'],
    ['an invalid JSON line', stream([META, '{"type": ', CITATIONS, DONE]), 'invalid_line'],
    ['an unknown type', stream([META, { type: 'surprise' }, CITATIONS, DONE]), 'invalid_line'],
    ['a first event other than meta', stream([DELTA, META, CITATIONS, DONE]), 'first_event_not_meta'],
    ['an error event', stream([META, { type: 'error', code: 'x' }, CITATIONS, DONE]), 'error_event'],
    ['two done events', stream([META, CITATIONS, DONE, DONE]), 'done_not_terminal'],
    ['done not last', stream([META, CITATIONS, DONE, DELTA]), 'done_not_terminal'],
    ['no done', stream([META, DELTA, CITATIONS]), 'done_not_terminal'],
    ['citations after done', stream([META, DELTA, DONE]), 'citations_missing'],
  ];
  for (const [label, response, reason] of cases) {
    const result = readStream(response);
    check(result, { [`stream: ${label}`]: (r) => r.reason === reason });
  }
  check(readStream(cases[0][1]), { 'stream: the session ID comes from meta': (r) => r.sessionId === META.sessionId });

  const run = parseRun(JSON.stringify({ apiBaseUrl: 'https://api.example/', tokens: { 'load-01': 'x' }, questions: ['q'] }));
  check(run, { 'run file: the base URL loses its trailing slash': (r) => r.apiBaseUrl === 'https://api.example' });
  let refusedRun = false;
  try {
    parseRun(JSON.stringify({ tokens: {}, questions: [] }));
  } catch (error) {
    refusedRun = true;
  }
  check(refusedRun, { 'run file: one without apiBaseUrl is refused': (x) => x === true });

  const limited = (body) => ({ status: 429, body: JSON.stringify(body) });
  check(null, {
    'retry wait: capped at 120 s': () => retryWait(limited({ code: 'too_many_requests', retryAfterSeconds: 500 })) === 120,
    'retry wait: none without retryAfterSeconds': () => retryWait(limited({ code: 'write_rate_limited' })) === null,
    'retry wait: none for rate_limited': () => retryWait(limited({ code: 'rate_limited', retryAfterSeconds: 1 })) === null,
    'retry wait: none for a 503': () => retryWait({ status: 503, body: '{}' }) === null,
  });
}

export function handleSummary(data) {
  return summaryMarker(data);
}
