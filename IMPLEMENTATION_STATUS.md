# Нейроныч — implementation status

> Обновление 2026-09-23: текущее состояние и ограничения — в [docs/PRODUCTION_REVIEW.md](docs/PRODUCTION_REVIEW.md). Нижележащие исторические записи не являются доказательством текущей готовности.


> Этот файл отражает текущее состояние runtime-реализации и не заменяет `PRODUCTION_LAUNCH_CHECKLIST.md`.
> Production launch по-прежнему возможен только после полного checklist PASS.

## Текущий инкремент

### EPIC 01 — Foundation

Status: `DEV PASS`

- FastAPI application factory.
- Pydantic Settings.
- async SQLAlchemy session layer.
- Alembic migrations.
- Redis client abstraction.
- structured JSON logging.
- health/readiness endpoints.
- Dockerfile, Docker Compose, Nginx.
- pytest + ruff baseline.

Evidence:

```bash
python -m pytest -q
python -m ruff check .
alembic upgrade head
```

### EPIC 02 — Partner lifecycle

Status: `PARTIAL DEV PASS`

Implemented:

- partner application submit;
- consent acceptance snapshots for terms/privacy versions;
- duplicate pending application guard;
- reject + reapply path;
- approval gate requiring active ArgoLink provider credential;
- partner account creation;
- account delete with negative-balance block;
- delete invalidates partner API keys;
- account state history for approval/delete.

Still open:

- Telegram cabinet flow;
- Telegram ID transfer flow;
- persistent notice after terms update;
- lifetime free-test eligibility.

### EPIC 03 — API keys, auth, versioning

Status: `PARTIAL DEV PASS`

Implemented:

- partner API keys are generated once and stored hashed;
- Bearer partner auth;
- revoked key cannot authenticate;
- `/api/v0/*` returns `410 api_version_expired`;
- one admin token via settings/env.

Still open:

- admin key DB-backed rotation;
- per-version usage reporting;
- webhook URL/secret per API key;
- old-version countdown surfaces.

### EPIC 04 — Catalog, capabilities, pricing

Status: `PARTIAL DEV PASS`

Implemented:

- model catalog;
- production enable gates for provider integration, docs and smoke;
- price gate before production enable;
- partner pricing by model/mode/resolution;
- partner pricing supports `billing_unit=generation|second`;
- price floor against provider cost using RUB/USDT settings snapshot;
- generation reserve multiplies per-second price by requested duration;
- append-only partner price history;
- `/pricing` exposes only production models.

Still open:

- full capability metadata;
- margin threshold resolver;
- public changelog/docs publication gate;
- image/LLM-specific pricing contracts.

### EPIC 05 — Provider abstraction and ArgoLink boundary

Status: `PARTIAL DEV PASS`

Implemented:

- provider adapter protocol;
- ArgoLink HTTP boundary adapter for documented async video API;
- ArgoLink video submit maps to `POST /v1/videos/generations` and stores returned `request_id` internally;
- video submit now forwards duration, resolution, aspect ratio and reference image URLs;
- ArgoLink video polling maps to `GET /v1/videos/{request_id}` and content URL uses `GET /v1/videos/{request_id}/content`;
- normalized provider HTTP errors;
- 429/408/5xx retry scheduling respects upstream `Retry-After` when present and falls back to capped exponential backoff;
- ambiguous submit read/write timeouts are not automatically replayed, preventing duplicate upstream generation jobs when submit outcome is unknown;
- provider credentials have format/connectivity checks before activation; protected live key validation remains open because `/v1/models` is public;
- active credential replacement deactivates previous active provider credential;
- provider capabilities by provider/model/mode/resolution;
- generation creation checks capability mismatch before accepting job;
- provider attempts store provider task id and raw errors internally only;
- admin dispatch endpoint sends accepted generation through adapter boundary;
- duplicate dispatch returns existing attempt and does not create duplicate provider task.
- Live Seedance 2.5 protected smoke with uploaded reference image reached media upload, media PUT and generation submit; provider accepted two jobs but both ended in retryable `generation_failed`.
- Partner-facing result URL is now separated from provider content URL through `media_assets`.
- ArgoLink content URL is internal only; partner responses expose our `result_url`.
- Provider result content can be ingested through a storage abstraction: local dev backend or S3-compatible production backend.
- Partner media route can serve local stored assets in dev; production should use `PUBLIC_MEDIA_BASE_URL` backed by CDN/object storage.
- Basic generation worker loop dispatches queued jobs, polls provider status and ingests provider-ready media assets.
- Docker Compose includes a separate `worker` service using the same app image.
- Provider attempts and media assets have bounded retry state with capped backoff metadata.
- Worker skips not-yet-due media ingest retries and keeps retry data durable across restart.
- Worker selects due rows with `FOR UPDATE SKIP LOCKED` to support Postgres multi-worker safety.
- Stale provider attempts are marked as generation `timeout`.
- Worker loop handles SIGTERM/SIGINT by stopping new cycles.

Still open:

- encrypted provider secret storage or external secret manager;
- protected ArgoLink key validation flow;
- provider quota discovery / remaining-quota reads when ArgoLink exposes them;
- circuit breaker incident model and recovery staging;
- queue depth/age metrics and real multi-worker/load validation;
- real Cloudflare R2/MinIO bucket provisioning and 24h lifecycle rule;
- cancellation;
- real provider balance/quota/cost reads;
- partner-level provider key lifecycle.

## Launch checklist alignment

Current dev evidence covers parts of:

- 3. Partner onboarding;
- 4. API keys / Auth;
- 5. Models / ArgoLink compatibility;
- 7. Idempotency;
- 8. Billing / Ledger;
- 9. Provider cost / Margin.
- 15. Queue / Reliability (basic single-worker dev path only).

Everything requiring real infrastructure, Telegram, ArgoLink, Crypto Bot, webhooks, hardened queue/retry semantics, backups, DR, production security review, load testing, deployment and 152-ФЗ remains `FAIL / NOT IMPLEMENTED` until implemented and tested against real dependencies.
