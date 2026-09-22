import http from 'k6/http';
import { check, fail } from 'k6';

const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const rawKeys = __ENV.LOAD_TEST_API_KEYS || '';
const apiKeys = rawKeys.split(',').map((value) => value.trim()).filter(Boolean);
const profile = __ENV.PROFILE || 'smoke';

if (__ENV.LOAD_TEST_ACK !== 'I_UNDERSTAND') {
  fail('Set LOAD_TEST_ACK=I_UNDERSTAND to run this load test.');
}

if (baseUrl.includes('api.нейроныч.online')) {
  fail('This harness refuses to target the production API domain.');
}

if (apiKeys.length === 0) {
  fail('LOAD_TEST_API_KEYS must contain at least one test API key.');
}

const profiles = {
  smoke: {
    scenarios: {
      acceptance: {
        executor: 'shared-iterations',
        vus: 2,
        iterations: 20,
        maxDuration: '30s',
      },
    },
  },
  load: {
    scenarios: {
      acceptance: {
        executor: 'constant-arrival-rate',
        rate: Number(__ENV.RATE || 50),
        timeUnit: '1s',
        duration: __ENV.DURATION || '2m',
        preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 50),
        maxVUs: Number(__ENV.MAX_VUS || 300),
      },
    },
  },
  spike: {
    scenarios: {
      acceptance: {
        executor: 'ramping-arrival-rate',
        startRate: 10,
        timeUnit: '1s',
        preAllocatedVUs: 100,
        maxVUs: Number(__ENV.MAX_VUS || 1000),
        stages: [
          { target: Number(__ENV.SPIKE_RATE || 500), duration: '10s' },
          { target: Number(__ENV.SPIKE_RATE || 500), duration: '20s' },
          { target: 0, duration: '10s' },
        ],
      },
    },
  },
};

if (!(profile in profiles)) {
  fail(`Unknown PROFILE=${profile}; expected smoke, load or spike.`);
}

export const options = {
  ...profiles[profile],
  thresholds: {
    'http_req_duration{endpoint:create_generation}': ['p(95)<500', 'p(99)<1000'],
    'http_req_failed{endpoint:create_generation}': ['rate<0.01'],
    'checks{endpoint:create_generation}': ['rate>0.99'],
  },
  discardResponseBodies: false,
};

function apiKeyForIteration() {
  // Repeating a key in LOAD_TEST_API_KEYS intentionally gives that partner a higher share.
  return apiKeys[(__VU + __ITER) % apiKeys.length];
}

export default function () {
  const idempotencyKey = `load-${__VU}-${__ITER}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const response = http.post(
    `${baseUrl}/api/v1/generations`,
    JSON.stringify({
      model_slug: 'load-test-video',
      prompt: 'synthetic acceptance load test',
      idempotency_key: idempotencyKey,
      mode: 'default',
      resolution: 'default',
      duration_seconds: 1,
      reference_images: [],
    }),
    {
      headers: {
        Authorization: `Bearer ${apiKeyForIteration()}`,
        'Content-Type': 'application/json',
      },
      tags: { endpoint: 'create_generation' },
      timeout: '5s',
    },
  );

  check(
    response,
    {
      'accepted with 202': (res) => res.status === 202,
      'returns generation id': (res) => {
        try {
          return Boolean(res.json('id'));
        } catch (_) {
          return false;
        }
      },
      'returns queued state': (res) => {
        try {
          return res.json('status') === 'queued';
        } catch (_) {
          return false;
        }
      },
    },
    { endpoint: 'create_generation' },
  );
}
