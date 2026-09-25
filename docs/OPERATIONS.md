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
этот файл и использует production + PITR compose. Соберите/зафиксируйте собственный
PostgreSQL image и заранее проверьте S3 storage/permissions. Новые deployment-файлы
должны быть доставлены в `/opt/neironych` до запуска существующего workflow;
workflow не синхронизирует конфигурацию сервера автоматически.

```sh
export NEIRONYCH_IMAGE=ghcr.io/bambale0/ooo:COMMIT_SHA
export APP_REVISION=COMMIT_SHA
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml config -q
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml build postgres
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml up -d postgres redis
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml run --rm app alembic upgrade head
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml up -d app worker webhook_worker nginx
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml --profile telegram up -d telegram
```

Не объединяйте production compose с development compose: иначе вернутся development
credentials и открытые DB ports. Bot polling должен иметь **один** экземпляр.
При каждом release обновляйте также Telegram service, если профиль используется.
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
- `/opt/neironych` exists on the target and contains the production compose files and `.backup.env`;
- the target has the required production `.env`, TLS certificates, Docker/Compose and recovery material;
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
