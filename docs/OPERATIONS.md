# Эксплуатация release candidate

Сначала прочтите [review и блокеры](PRODUCTION_REVIEW.md). Эти команды не разрешают
запуск реальных продаж до закрытия обязательных launch gates.

## Конфигурация

Создайте `.env` по `.env.example`. Production читает его целиком в каждом процессе.
Укажите PostgreSQL URL с теми же POSTGRES_USER/PASSWORD/DB, Redis URL, PUBLIC_API_BASE_URL
с HTTPS, ADMIN_API_TOKEN и PROVIDER_CREDENTIALS_MASTER_KEY с независимыми случайными
значениями >=32 символов. Сохраните master key отдельно: без него старые credentials
и webhook secrets нельзя расшифровать. Глобальный ARGOLINK_API_KEY для per-partner
генераций не обязателен. Не включайте TELEGRAM профиль без токена и ADMIN_TELEGRAM_ID.

Production compose теперь **самостоятельный**. Не объединяйте его с development compose:
так снова унаследуются development credentials и опубликованные DB ports.
Прежде чем включить GitHub environment `production`, установите актуальный compose
в `/opt/neironych`, `.env`, TLS certificates и registry auth для deploy account.
Workflow требует DEPLOY_HOST, DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS и успешный CI точного SHA.
Registry repository path приводится к нижнему регистру. Не используйте `latest` для app.

```sh
export NEIRONYCH_IMAGE=ghcr.io/bambale0/ooo:COMMIT_SHA
export APP_REVISION=COMMIT_SHA
docker compose -f docker-compose.prod.yml config -q
docker compose -f docker-compose.prod.yml up -d postgres redis
docker compose -f docker-compose.prod.yml run --rm app alembic upgrade head
docker compose -f docker-compose.prod.yml up -d app worker webhook_worker nginx
```

Health endpoints: `/api/v1/health` (liveness), `/api/v1/readiness` (DB + Redis).
API bind host: 127.0.0.1:8000; внешний вход — TLS nginx. Сгенерированные файлы
проксируются потоком, не сохраняются в MinIO. `media/storage.py` — не активный result path.
Один API worker сохраняет корректность текущих in-process Prometheus counters;
дальнейшее масштабирование требует multiprocess/central aggregation.

Для ручного release можно использовать `ops/deploy/release.sh` с PREVIOUS_IMAGE и
PREVIOUS_REVISION. При failed readiness возвращается предыдущий image, но DB schema
не откатывается автоматически. Применяйте expand/deploy/contract migrations.

## Неопределённая отправка

Внутренний запрос для поиска зависших submit intents:

```sql
SELECT generation_id, provider, created_at
FROM provider_attempts
WHERE status = 'submitting' AND provider_task_id IS NULL
ORDER BY created_at;
```

Сверьте эти запросы с провайдером под исходным аккаунтом, не делайте повторный submit.
Если task найден, восстановите ID и продолжите polling под исходным credential_id
через проверенную административную процедуру. Если доказано отсутствие задачи,
освободите оба резерва через ledger services отдельными компенсирующими операциями.
Нельзя напрямую редактировать balances или удалять ledger entries.

## Проверки изоляции

Unit tests используют SQLite fixture. TEST_POSTGRES_DATABASE_URL должен указывать
на отдельную мигрированную БД: fault tests создают/удаляют синтетические записи.
Не направляйте тесты в production. API/workers smoke должны иметь отдельную DB от
конкурентно выполняющегося pytest, чтобы воркеры не забирали test fixtures.

Нельзя считать mock-generated media доказательством live ArgoLink render. Для
каждой включаемой конфигурации отдельно подтверждаются input contract, charge,
terminal webhook и полный MP4 download.
