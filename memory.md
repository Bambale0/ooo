# Нейроныч — memory

> Обновление 2026-09-23: текущее состояние и ограничения — в [docs/PRODUCTION_REVIEW.md](docs/PRODUCTION_REVIEW.md). Нижележащие исторические записи не являются доказательством текущей готовности.


## Decisions

- Реализовывать продукт маленькими проверяемыми инкрементами по `IMPLEMENTATION_EPICS.md`.
- Каждый инкремент должен иметь явную связь с `PRODUCTION_LAUNCH_CHECKLIST.md`.
- `IMPLEMENTATION_STATUS.md` хранит dev-status; production PASS может давать только полный checklist.
- `AGENTS.md` является правильным файлом инструкций. `Agents.md` — конфликтующий по регистру legacy файл.
- Деньги: только `Decimal` / SQL `NUMERIC`; binary float запрещён.
- Partner-facing API не должен раскрывать provider name, provider task id, raw provider error, upstream cost.
- Provider key plaintext не хранить в БД. Сейчас хранится hash; для реального ArgoLink вызова нужен encrypted secret storage.
- ArgoLink docs provided by user: base URL `https://argolink.io`; Bearer auth; OpenAI Responses uses `POST /v1/responses`; Images use `POST /v1/images/generations` and `/v1/images/edits`; Videos use async `POST /v1/videos/generations`, then `GET /v1/videos/{request_id}`, then `GET /v1/videos/{request_id}/content`; submit response returns `request_id`.
- ArgoLink adapter now maps video submit/status/content to the documented endpoints. Runtime calls use env `ARGOLINK_API_KEY`; admin-pasted provider keys remain hash-only until encrypted secret storage exists.
- ArgoLink model discovery `/v1/models` is public, so it can prove catalog reachability but not cryptographically prove a Bearer key. Live protected-key validation needs either a low-cost protected endpoint or an explicit sandbox/validation flow.
- Seedance 2.5 reference media must use media upload -> PUT -> `reference_images: [{"url": media_url}]`; data URI/reference JSON is not the correct operational path.
- Video pricing can be per generation or per second. Seedance 2.5 720p should be configured with `billing_unit=second`; ledger reserve multiplies unit partner price by requested duration.
- Live smoke on 2026-09-20 with the user's test key: upload and submit path worked, two jobs were accepted with request IDs, both failed at provider generation stage with retryable `generation_failed`. Treat this as protected key and lifecycle proof, not output proof.
- Partner-facing media URLs must be ours, never ArgoLink. `media_assets` stores internal `provider_content_url` and public `public_url`; current endpoint returns `asset_not_ingested` until object storage/CDN ingest is implemented.
- Recommended media architecture: backend downloads provider content in a worker, uploads to cheap object storage/CDN with 24h TTL lifecycle, then marks asset `stored`.
- Media ingest now exists: provider adapter can fetch result content, media service stores it via local dev backend or S3-compatible backend, and partner download route serves local files or redirects to our CDN URL.
- For Cloudflare R2: use S3-compatible settings and a bucket lifecycle rule for 24h deletion. Do not use Cloudflare Stream for MVP unless transcoding/player features become required.
- Basic generation worker now exists: `app.workers.generation_worker` dispatches queued generations, polls provider tasks and ingests ready media assets. It is unit/integration-tested as one worker cycle.
- Worker now persists bounded retry state for provider attempts and media ingest (`retry_count`, `next_attempt_at`, `last_error`) using capped exponential backoff with jitter.
- Worker selection now uses Postgres `FOR UPDATE SKIP LOCKED` for queued/poll/media rows; SQLite tests ignore this as expected.
- Provider processing timeout marks stale attempts/generations as `timeout` with public code `generation_timeout`.
- Worker has SIGTERM/SIGINT stop handling for graceful loop exit.
- Worker is not production-complete yet: needs Retry-After extraction, circuit breaker, queue metrics and real multi-worker/load validation before launch.

## Current Dev Evidence

- `pytest`: зелёный на 7 тестах after worker lock/timeout hardening.
- `ruff`: зелёный after worker lock/timeout hardening.
- Alembic migrations 0001..0006 pass upgrade/downgrade on clean SQLite smoke DB.

## Open Risks

- Live protected Seedance calls can be submitted, but current reference smoke returns retryable provider `generation_failed`.
- Basic worker exists with persisted retry state, skip-locked selection and provider timeout policy, but production multi-worker/load validation is still open.
- Нет encrypted provider secret storage.
- Нет real Telegram cabinet flow.
- Нет Crypto Bot payment flow.
- Нет webhook delivery subsystem.
- Нет backup/DR implementation.

## Useful Local Paths

- Runtime app: `app/`
- Migrations: `alembic/versions/`
- Integration scenario: `tests/test_partner_generation_flow.py`
- Status: `IMPLEMENTATION_STATUS.md`
- Launch gate: `PRODUCTION_LAUNCH_CHECKLIST.md`
