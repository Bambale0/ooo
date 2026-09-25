# Generation acceptance load tests

This harness measures the fast client-facing generation acceptance path:

`auth -> validation -> capability/price lookup -> atomic reserve -> durable generation -> HTTP 202`.

It intentionally does **not** test paid ArgoLink generation throughput. The generation worker must be stopped while running this harness.

## Safety

- The seed command refuses `APP_ENV=production`.
- The k6 script refuses `api.нейроныч.online`.
- Seeded provider credentials contain deliberately invalid ciphertext. If a generation worker is accidentally started, provider credential decryption fails closed instead of sending upstream traffic.
- Use a disposable test database or isolated test environment.
- Clean the fixture after the run.

## 1. Prepare the test environment

Apply migrations and run only the API + PostgreSQL/Redis. Do **not** start `generation_worker` or `webhook_worker`.

Choose one or more long random test API keys:

```bash
export APP_ENV=test
export LOAD_TEST_API_KEYS='nrn_load_test_partner_one_123456789,nrn_load_test_partner_two_123456789' # pragma: allowlist secret
python -m ops.load.seed seed
```

The seeder creates:
- isolated load-test partners;
- large synthetic RUB balances;
- API keys;
- a `load-test-video` production catalog entry;
- a minimal partner price/capability gate;
- fail-closed dummy provider credentials.

## 2. Smoke first

```bash
export BASE_URL='http://127.0.0.1:8000'
export LOAD_TEST_ACK='I_UNDERSTAND'
k6 run -e PROFILE=smoke ops/load/generation_acceptance.js
```

Expected thresholds:
- create generation p95 < 500 ms;
- p99 < 1 s;
- <1% HTTP failures;
- >99% acceptance checks.

## 3. Normal load

```bash
k6 run \
  -e PROFILE=load \
  -e RATE=50 \
  -e DURATION=2m \
  -e BASE_URL="$BASE_URL" \
  -e LOAD_TEST_API_KEYS="$LOAD_TEST_API_KEYS" \
  -e LOAD_TEST_ACK=I_UNDERSTAND \
  ops/load/generation_acceptance.js
```

Raise `RATE` gradually. Do not jump directly to the desired maximum before a smoke and moderate run are clean.

## 4. Spike

```bash
k6 run \
  -e PROFILE=spike \
  -e SPIKE_RATE=500 \
  -e BASE_URL="$BASE_URL" \
  -e LOAD_TEST_API_KEYS="$LOAD_TEST_API_KEYS" \
  -e LOAD_TEST_ACK=I_UNDERSTAND \
  ops/load/generation_acceptance.js
```

For uneven partner traffic, repeat a partner API key in `LOAD_TEST_API_KEYS`. The seeder de-duplicates keys; k6 keeps repetitions as traffic weights.

## 5. Observe during the run

Watch:
- `neironych_http_request_duration_seconds`;
- `neironych_generation_queue_depth{status="queued"}`;
- `neironych_generation_queue_oldest_age_seconds{status="queued"}`;
- DB pool checked-out/size/overflow;
- CPU, RAM and PostgreSQL connections.

Since workers are intentionally stopped, queued depth should grow. This test answers whether the API can safely and quickly accept durable work, not how fast the provider completes it.

## 6. Cleanup

```bash
python -m ops.load.seed clean
```

Cleanup removes only records belonging to the dedicated `load-test-*` partners and `load-test-video` catalog fixture.

## Worker/provider resilience harness

The repository also contains a **local-only ArgoLink-compatible provider stub** and a
direct worker-backlog seeder. This exercises worker concurrency, fair scheduling,
adaptive polling, provider connection pooling, Retry-After handling and restart
behavior without spending provider funds.

### Safety gate

The worker harness refuses to seed, inspect or clean unless:

- `APP_ENV` is not production;
- `ARGOLINK_BASE_URL` resolves to `localhost`, `127.0.0.1` or `::1`;
- `LOAD_WORKER_TEST_ACK=I_UNDERSTAND_LOCAL_STUB_ONLY`;
- `PROVIDER_CREDENTIALS_MASTER_KEY` is configured.

Use an isolated disposable PostgreSQL database. Never point this harness at production.

### 1. Start the local provider stub

```bash
export LOAD_STUB_PROCESSING_POLLS=2
uvicorn ops.load.provider_stub:app --host 127.0.0.1 --port 18080
```

Useful fault controls:

```bash
# Every 3rd submit returns 429 with Retry-After.
export LOAD_STUB_SUBMIT_429_EVERY=3
export LOAD_STUB_RETRY_AFTER_SECONDS=2

# Every 5th poll returns 429.
export LOAD_STUB_POLL_429_EVERY=5

# Inject provider latency.
export LOAD_STUB_LATENCY_MS=250

# Inject deterministic 500s. Submit 500s are expected to exercise
# submission-outcome reconciliation rather than blind paid replay.
export LOAD_STUB_SUBMIT_500_EVERY=0
export LOAD_STUB_POLL_500_EVERY=0
```

Stub counters are available at `GET http://127.0.0.1:18080/__load__/stats`.
Reset them with `POST /__load__/reset` and header
`X-Load-Test-Ack: I_UNDERSTAND`.

### 2. Seed a synthetic worker backlog

```bash
export APP_ENV=test
export ARGOLINK_BASE_URL='http://127.0.0.1:18080'
export PROVIDER_CREDENTIALS_MASTER_KEY='replace-with-local-test-master-key-at-least-32-characters'
export LOAD_WORKER_TEST_ACK='I_UNDERSTAND_LOCAL_STUB_ONLY'
export LOAD_WORKER_PARTNERS=5
export LOAD_WORKER_GENERATIONS_PER_PARTNER=2000

python -m ops.load.worker_seed seed
```

This directly creates queued synthetic Seedance jobs and one unique local stub
credential per synthetic partner. It does not touch the public generation endpoint;
the separate k6 acceptance harness already measures that path.

### 3. Run the real generation worker

Use short poll/retry timings for local tests, then start the same worker module used
by production:

```bash
export WORKER_BATCH_SIZE=100
export WORKER_SUBMIT_CONCURRENCY=20
export WORKER_POLL_CONCURRENCY=40
export WORKER_INITIAL_POLL_DELAY_SECONDS=1
export WORKER_POLL_BACKOFF_BASE_SECONDS=1
export WORKER_POLL_BACKOFF_MAX_SECONDS=5
export WORKER_RETRY_BASE_SECONDS=1
export WORKER_RETRY_MAX_SECONDS=10

python -m app.workers.generation_worker
```

Inspect progress from another shell:

```bash
python -m ops.load.worker_seed status
curl -s http://127.0.0.1:18080/__load__/stats
```

### 4. Restart/fault scenarios

Run these separately so the cause is measurable:

1. baseline with 5–10 partners and uneven backlog sizes;
2. 10k+ queued jobs with normal stub behavior;
3. submit/poll 429 plus Retry-After;
4. 250–1000 ms upstream latency;
5. kill/restart the generation worker while jobs are queued and processing;
6. restart PostgreSQL while the worker is active, then verify durable recovery;
7. submit 500/outcome-unknown scenario and verify no blind duplicate paid submit;
8. keep a partner webhook endpoint unavailable for the full retry window in a
   separate webhook-worker scenario.

For each run capture:

- total and per-partner generation status counts;
- queue depth and oldest queue age;
- provider submit/poll calls and injected failures;
- DB pool usage;
- worker CPU/RAM;
- provider request latency/error metrics;
- any `reconciliation_required` rows.

### 5. Cleanup

```bash
python -m ops.load.worker_seed clean
```

## Remaining load scenarios

The local harness makes the worker/provider scenarios reproducible, but real
production SLO still requires running the acceptance and worker tests on hardware
close to the target VPS and measuring the actual PostgreSQL/network limits.

Do not execute destructive or high-rate load scenarios against production.
