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

## Next load scenarios

After the acceptance path is measured, run separate controlled resilience scenarios for:
- 5–10 partners with unequal traffic;
- 10k+ queued/in-flight synthetic jobs;
- provider 429/500/latency using a local provider stub;
- worker restart during submit/poll/settlement;
- PostgreSQL restart;
- partner webhook endpoint unavailable for the full retry window.

Do not execute destructive or high-rate load scenarios against production.
