# Нейроныч — текущий контекст разработки

> Обновление 2026-09-23: текущее состояние и ограничения — в [docs/PRODUCTION_REVIEW.md](docs/PRODUCTION_REVIEW.md). Нижележащие исторические записи не являются доказательством текущей готовности.


Дата обновления: 2026-09-20

## Рабочее правило

Реализация идёт строго по `IMPLEMENTATION_EPICS.md`.
Каждый инкремент сверяется с `PRODUCTION_LAUNCH_CHECKLIST.md`.
`AGENTS.md` — обязательный процессный источник истины.

## Текущее состояние runtime

Репозиторий перешёл от specification-only к backend runtime skeleton.

Реализованы:

- FastAPI application factory;
- Pydantic Settings;
- async SQLAlchemy;
- Alembic migrations;
- Redis abstraction;
- JSON logging;
- health/readiness;
- Dockerfile, Docker Compose, Nginx;
- partner applications, consent snapshots, approval/reject/delete;
- partner API keys, hashed secrets, revoke;
- model catalog, enable gates, pricing, price history;
- provider credentials, provider capabilities, provider attempts;
- provider adapter boundary for ArgoLink with normalized errors and internal provider task IDs;
- idempotent generation creation with partner price snapshot and ledger reserve;
- video generation request params: duration, aspect ratio, reference images;
- pricing units: per-generation and per-second billing, including Seedance-style duration multiplication;
- media asset layer for partner-facing result URLs: provider content URL is stored internally, partner API receives only our media/CDN URL;
- media ingest service with local dev storage and S3-compatible production backend for Cloudflare R2 / MinIO-style object storage;
- generation worker loop for queued dispatch, provider polling and media ingest;
- bounded retry state for provider attempts and media ingest with persisted `retry_count`, `next_attempt_at` and `last_error`;
- Postgres `FOR UPDATE SKIP LOCKED` worker selection for multi-worker safety;
- provider processing timeout policy.

## Проверочные команды

Использовать Python 3.14 на этой машине:

```powershell
& 'C:\Users\happy\AppData\Local\Python\bin\python.exe' -m pytest -q
& 'C:\Users\happy\AppData\Local\Python\bin\python.exe' -m ruff check .
```

Alembic smoke:

```powershell
$env:DATABASE_URL = 'sqlite+aiosqlite:///./alembic_epic_smoke.db'
& 'C:\Users\happy\AppData\Local\Python\bin\python.exe' -m alembic upgrade head
```

## Важные ограничения

- На Windows есть case-collision `AGENTS.md` / `Agents.md`; правильный файл — `AGENTS.md`.
- ArgoLink docs are captured from the user/docs page: base URL `https://argolink.io`, Bearer auth, OpenAI Responses `/v1/responses`, Images `/v1/images/generations` and `/v1/images/edits`, Videos async lifecycle `/v1/videos/generations` -> `/v1/videos/{request_id}` -> `/v1/videos/{request_id}/content`.
- Seedance local reference flow: `POST /v1/media/uploads`, PUT bytes to `upload_url`, then pass `media_url` in `reference_images`. Data URIs/multipart are not the intended Seedance path.
- Live protected smoke with the test partner key reached media upload `201`, media PUT `200`, video submit `202` twice. Both Seedance 2.5 jobs moved from `pending` to retryable `failed/generation_failed`; this validates the key/lifecycle path but not successful render output.
- Text-only Seedance 2.5 job without references reached `done`; content download may be slow/hanging, so production ingest must use background retry/timeout and then expose our stored asset URL.
- Do not redirect partners to ArgoLink `/content`. Download/ingest internally, store in object storage/CDN, expose only our `media_assets.public_url`.
- Runtime media env now includes `MEDIA_STORAGE_BACKEND`, `MEDIA_LOCAL_STORAGE_DIR`, `PUBLIC_MEDIA_BASE_URL`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
- Worker env includes `WORKER_POLL_INTERVAL_SECONDS` and `WORKER_BATCH_SIZE`. Docker Compose has a separate `worker` service running `python -m app.workers.generation_worker`.
- Retry env includes `WORKER_MAX_RETRIES`, `WORKER_RETRY_BASE_SECONDS`, `WORKER_RETRY_MAX_SECONDS`.
- Timeout/shutdown env includes `WORKER_PROVIDER_PROCESSING_TIMEOUT_SECONDS` and `WORKER_SHUTDOWN_GRACE_SECONDS`.
- Cloudflare R2 deployment path: set `MEDIA_STORAGE_BACKEND=s3`, configure R2 S3 endpoint/credentials/bucket, set `PUBLIC_MEDIA_BASE_URL` to the CDN/custom-domain prefix, add 24h lifecycle expiration on the bucket.
- Provider secrets pasted through admin API are validated on input and stored only as hash. Runtime ArgoLink calls use `ARGOLINK_API_KEY` from env until encrypted secret storage exists.
- ArgoLink `/v1/models` is public; use it for catalog/connectivity checks, not as proof that a pasted Bearer key is valid for protected media/inference.
- Production checklist остаётся FAIL по всем пунктам, требующим реальной инфраструктуры, Telegram, Crypto Bot, ArgoLink, backups, DR, workers, webhooks и load/security tests.

## Следующий маршрут

1. EPIC 07:
   - strict generation state transitions;
   - terminal flows;
   - no duplicate provider submit after retry/restart;
   - late success handling later, after polling worker.
2. EPIC 18:
   - Retry-After-aware provider delay extraction if ArgoLink exposes it in normalized errors.
   - explicit circuit breaker incident model;
   - queue age/depth metrics.
3. Потом EPIC 06/08 economics:
   - negative economics pre-creation 503 without burning idempotency key;
   - actual provider cost snapshots;
   - reproducible margin.
