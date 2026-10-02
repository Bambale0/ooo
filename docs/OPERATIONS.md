# Эксплуатация

Сначала прочтите [проверку готовности](PRODUCTION_REVIEW.md) и
[live-матрицу ArgoLink](LIVE_VERIFICATION_2026-09-23.md). Не включайте модель по одному
факту её присутствия в каталоге.

## Подготовка окружения

Создайте защищённый `.env` по `.env.example`: PostgreSQL URL с согласованными
POSTGRES_USER/PASSWORD/DB, Redis, HTTPS PUBLIC_API_BASE_URL, независимые случайные
ADMIN_API_TOKEN и PROVIDER_CREDENTIALS_MASTER_KEY >=32 символов. Сохраните master key
отдельно: он нужен для credentials и webhook secrets после восстановления.

Задайте Crypto Pay credentials, проверенный RUB/USDT fallback, сверенный opening
capital и provider float. Без доступного подтверждённого wallet новые платные
запросы закрываются нейтральной 503. Для кабинета задайте TELEGRAM_BOT_TOKEN,
ADMIN_TELEGRAM_ID и HTTPS TERMS_URL/PRIVACY_POLICY_URL с реальными опубликованными
документами. Эти значения нельзя брать из тестов.

Создайте `.backup.env` по [backup runbook](../ops/backup/README.md). Release требует
этот файл и использует production + PITR compose. Заранее проверьте S3
storage/permissions.

Production host использует разделение:

```text
/opt/neironych/
  shared/.env
  shared/.backup.env
  shared/nginx/ssl/fullchain.pem
  shared/nginx/ssl/privkey.pem
  releases/<commit-sha>/...
  current -> releases/<live-commit-sha>
  REVISION
```

GitHub Actions сам доставляет в новый versioned release directory только
несекретную конфигурацию конкретного проверенного commit: production/PITR compose,
Nginx config и build-context PostgreSQL PITR image. Secrets, TLS private key и
backup credentials остаются только в `shared/` на production host.

```sh
export NEIRONYCH_IMAGE=ghcr.io/bambale0/ooo:COMMIT_SHA
export APP_REVISION=COMMIT_SHA
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml config -q
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml build postgres
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml up -d postgres redis
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml run --rm app alembic upgrade head
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml --profile telegram up -d \
  app worker webhook_worker telegram nginx
```

Не объединяйте production compose с development compose: иначе вернутся development
credentials и открытые DB ports. Bot polling должен иметь **один** экземпляр.
Release workflow всегда обновляет API, generation worker, webhook worker, Telegram
polling и Nginx одной проверенной revision. Bot polling должен оставаться в одном
экземпляре.
GitHub environment `production` требует DEPLOY_HOST, DEPLOY_SSH_KEY,
DEPLOY_KNOWN_HOSTS, registry auth и CI для точного SHA; `latest` не используется.

## Включение моделей

1. Admin `GET /api/v1/catalog/contracts/drift`: проверьте изменения поставщика.
2. `POST /api/v1/catalog/contracts/import`: создаст draft-модели и capabilities,
   не придумает retail prices и не поставит smoke PASS. При drift импорт закрыт.
3. Задайте полный прайс нужных тарифов через `PUT /api/v1/catalog/pricing`.
   Grok image edits требуют ручной сверки procurement с реальными списаниями.
4. Привяжите зашифрованный ArgoLink credential к заявке, одобрите партнёра.
5. Выполните smoke каждого продаваемого протокола/режима под его credential,
   установите enable gates и включите прошедшие модели. Непроверенные остаются draft.

Native requests: partner Bearer + Idempotency-Key. Guide: `/guide?lang=ru|en`,
retail prices: `/prices`, schema: `/docs`.

## Корреляция UUID

Для любой генерации основной correlation ID - полный generation_id; в native API он же
возвращается как request_id и используется как trace_id в structured logs.

Партнёр может получить tenant-scoped цепочку собственных UUID через
GET /api/v1/generations/{generation_id}/trace: partner/api-key/model, provider-attempt UUID,
ledger/coverage UUID, media UUID и webhook event/delivery UUID. Provider-specific credential/task/raw
error/upstream identifiers партнёру не раскрываются.

Admin может получить расширенную цепочку через
GET /api/v1/generations/admin/{generation_id}/trace; там дополнительно доступны credential ID,
provider task ID и allowlisted upstream request UUID. По generation_id следует искать structured
events generation_reserved, native_inference_*, generation_dispatched, generation_polled,
webhook_delivery_started и webhook_delivery_finished. UUID пишутся полностью, без сокращения.

## Неопределённая отправка

`GET /api/v1/providers/reconciliation` показывает неопределённые submit intents.
Проверьте задачу у поставщика под исходным credential, **не делайте новый submit**.
`POST /api/v1/providers/reconciliation/{generation_id}` требует reason и outcome:
`attach_video` с подтверждённым upstream task ID, `not_accepted` при доказанном
непринятии либо `completed` с проверенными units по сохранённым тарифам.
Это admin-only; нельзя напрямую править balance или удалять ledger entries.

Неопределённый платёжный invoice имеет отдельную процедуру
`POST /api/v1/payments/invoices/{payment_id}/reconcile`; подробности в
[финансовой документации](TREASURY.md).

## Мониторинг и rollback

Liveness `/api/v1/health`; readiness `/api/v1/readiness` проверяет DB + Redis и
показывает APP_REVISION. API доступен только на 127.0.0.1:8000; внешний вход — TLS nginx.
`/internal/metrics` закрыт на внешнем nginx; Prometheus должен иметь внутренний доступ.
Queue/business gauges читаются из общей БД. HTTP counters относятся к одному API
process; при масштабировании потребуется общая агрегация. Alert rules лежат в
`ops/prometheus/alerts.yml`; настройте реальную доставку мониторинга.

`ops/deploy/release.sh` сверяет revision/readiness и возвращает предыдущий app image
при сбое. Схема БД автоматически не откатывается: применяйте expand/deploy/contract.
TLS, резервирование диска, рестарт VPS, пределы RAM/CPU и RTO на реальном объёме —
отдельные проверки целевой инфраструктуры.

Сгенерированные результаты проксируются потоком и не сохраняются в application
storage. Вложения поддержки сохраняются в `support_data` и включаются в backup.
Разместите этот volume и независимый backup storage в согласованном РФ-контуре.

## Повторяемые проверки

```sh
python -m pytest -q
alembic upgrade head
alembic check
python ops/smoke/argolink.py --secret-file /secure/test.json --report /secure/read-only-report.json
```

ArgoLink CLI по умолчанию read-only. Платный запуск требует `--execute`, `--reference`,
`--budget-usd`; секреты читаются из private JSON, а не из argv. Известные upstream
ошибки не запускают бесконечные платные retries. Полный boundary-test исчерпания квоты
в этой проверке не проводился.

TEST_POSTGRES_DATABASE_URL направляйте только в отдельную мигрированную БД.
Не запускайте pytest и queue workers на одной тестовой БД. WAL drill:
`docker build -t ooo-pitr-check ops/backup/postgres` и `python ops/smoke/pitr.py`.
Он создаёт только собственные временные контейнеры/volumes без сетевого доступа.

Поведение кабинета, lifetime trials, provider circuit и low-margin настройки: [cabinet/recovery](CABINET_AND_RECOVERY.md). Для automatic recovery нужен generation worker; для legal/financial notices — Telegram process.


## Production deploy arming gate

Production deploys are intentionally disabled by default even when CI on `main` is green.

The deploy job runs only when the **repository Actions variable**
`PRODUCTION_DEPLOY_ENABLED` is exactly `true`.

Before arming it, verify all of the following:

- GitHub Environment `production` exists and its required approvals/policies are configured as intended;
- `DEPLOY_SSH_KEY` is installed as an Actions secret;
- `DEPLOY_KNOWN_HOSTS` contains the pinned production host key;
- `DEPLOY_HOST` points to the intended production host;
- `/opt/neironych/shared/.env` and `/opt/neironych/shared/.backup.env` exist on the target;
- TLS certificate/key exist under `/opt/neironych/shared/nginx/ssl/`;
- the target has Docker/Compose, GHCR pull access and recovery material;
- the previous revision/rollback path is known;
- production readiness checks and external launch gates have been explicitly approved.

Only then set:

```text
PRODUCTION_DEPLOY_ENABLED=true
```

With the gate armed, a successful CI run for `main` triggers the existing immutable-image deploy,
readiness revision verification and automatic rollback flow.

To freeze automated production deploys without editing workflow code, set the variable to any value
other than `true` or remove it. CI continues to run while deployment remains skipped.

This gate prevents an unconfigured repository from repeatedly attempting production SSH deploys or
publishing a green-CI change as if infrastructure were already ready.


## Main-branch release provenance

The repository currently cannot rely on GitHub branch protection as the only release control.
CI therefore fails closed on every `push` to `main` unless the pushed commit is associated with
a **merged pull request whose base is `main`**.

This is a release safeguard, not a replacement for server-side branch protection:

- a direct push can still alter Git history if GitHub allows it;
- the resulting main CI fails;
- because production deployment only follows a successful main CI, that direct push cannot become an automated production release;
- normal merged PRs continue through CI and may deploy only when the separate production arming gate is enabled.

The check uses GitHub's commit-to-pull-request API with the workflow's read-only
`GITHUB_TOKEN`. It does not require repository write permission and does not expose secrets.

If GitHub plan/settings later allow mandatory branch protection, enable it as well; keep this
check as defense in depth for release provenance.


## Автоматическое зачисление Crypto Pay

С 2026-09-30 подтверждённая оплата счёта автоматически зачисляет его
`requested_rub` в баланс партнёра. Проверка Crypto Pay, retail/coverage ledger,
снимок курса и уведомления завершаются одной транзакцией. Повторы безопасны.
Ручной credit endpoint и Telegram-кнопка остаются резервными действиями.

Generation worker запускает независимую фоновую сверку при заданном
`CRYPTO_PAY_API_TOKEN`. Параметры: `PAYMENT_RECONCILIATION_INTERVAL_SECONDS=60`
и `PAYMENT_RECONCILIATION_BATCH_SIZE=20`. Один проход обрабатывает не более
20 счетов; курсор циклически проходит все незачисленные счета. При большом
backlog подтверждение может занять несколько проходов. Сверка не создаёт
счета и не делает платежи. Восстановление потерянного ответа создания через
payload ограничено последними 1000 счетами, доступными существующему поиску
Crypto Pay; для более старых неизвестных invoice id доступна admin reconcile.

`payment_reconciliation_started` подтверждает запуск сверки.
`payment_reconciliation_failed` содержит локальный payment id и тип ошибки;
такие счета повторно проверяются при следующем обходе. Не проверяйте выпуск
созданием фиктивной оплаты в production: используйте изолированные тесты и
наблюдайте следующий настоящий платёж. Provider float и ограничения treasury
продолжают проверяться отдельно при генерациях.

## Native upstream diagnostics

Native inference emits `native_inference_submit_started`,
`native_inference_upstream_headers` and, on transport/decode failures,
`native_inference_transport_or_decode_failed`. Each event links the local
`generation_id`, `attempt_id`, `trace_id`, model and protocol. Failures also retain
this bounded diagnostic JSON in the existing internal `ProviderAttempt.raw_error`.

`upstream_status` distinguishes an upstream HTTP 524/5xx from a local transport
exception. `phase` distinguishes `awaiting_headers`, `response_headers` and
`response_body_or_usage`. `submit_elapsed_ms` includes adapter rate-limiter wait;
it must not be reported as provider rendering time. Well-formed `cf_ray` and UUID
`upstream_request_id` headers are retained only internally for escalation.

Prompts, reference URLs, auth/cookie headers, response bodies and exception messages
are excluded. The public API/error envelope and no-blind-replay billing semantics
are unchanged. These diagnostics do not remove an upstream timeout or prove whether
an uncertain image was accepted. Do not resubmit it with a new key without reconciliation.
