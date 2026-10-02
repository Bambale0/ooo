# Инструкция по ручному развертыванию partner-specific pricing

## Контекст

- **PR**: #73 https://github.com/Bambale0/ooo/pull/73
- **Ветка**: `feature/partner-specific-pricing-35pct-margin`
- **Commit**: `9a07037`
- **Миграция**: `20261002_0023_partner_specific_pricing.py`

## Что реализовано

✅ Per-partner pricing с nullable `partner_id` FK  
✅ Резолюция цен: сначала partner-specific, потом global fallback  
✅ 35% валовая маржа для новых партнёров (`price_rub = cost_rub / 0.65`)  
✅ Заморозка текущих цен для существующих партнёров  
✅ Три admin API эндпоинта для управления  

## Шаги развертывания на production

### 1. Merge PR и дождаться CI

```bash
# После review и approval
gh pr merge 73 --merge
```

### 2. Подключиться к production хосту

```bash
ssh root@DEPLOY_HOST
cd /opt/neironych
```

### 3. Сделать бэкап БД (обязательно!)

```bash
# Через штатный PITR или ручной dump
docker compose exec postgres pg_dump -U neironych neironych > backup_before_partner_pricing_$(date +%Y%m%d_%H%M%S).sql
# Скопировать бэкап в безопасное место
```

### 4. Запустить migration

```bash
cd /opt/neironych/current
source shared/.env  # или export переменные вручную

# Проверить текущую версию
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  run --rm app alembic current

# Применить миграцию
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  run --rm app alembic upgrade head

# Проверить, что применилась 20261002_0023
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  run --rm app alembic current
```

### 5. Проверить схему БД

```bash
docker compose exec postgres psql -U neironych -d neironych -c "\d partner_prices"
# Должны быть колонки: id, model_id, partner_id (nullable), mode, resolution, ...
# Должен быть индекс ix_partner_prices_partner_id
# Должна быть FK constraint fk_partner_prices_partner_id
```

### 6. Перезапустить сервисы

```bash
cd /opt/neironych/current

# Перезапуск API и воркеров
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  --profile telegram restart app worker webhook_worker

# Проверить readiness
curl http://127.0.0.1:8000/api/v1/readiness
# Должен вернуть {"status":"ok","revision":"..."}
```

### 7. Проверить, что глобальные цены на месте

```bash
# Через psql
docker compose exec postgres psql -U neironych -d neironych -c \
  "SELECT COUNT(*) FROM partner_prices WHERE partner_id IS NULL;"
# Должно вернуть количество существующих глобальных цен

# Через API (если доступен извне)
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing
```

### 8. Заморозить цены для существующих партнёров

```bash
# Получить список всех активных партнёров
docker compose exec postgres psql -U neironych -d neironych -t -c \
  "SELECT id, telegram_id, company_name FROM partners WHERE status = 'active';"

# Для КАЖДОГО существующего партнёра создать snapshot
# Пример для одного:
PARTNER_ID="uuid-здесь"
curl -X POST "https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/snapshot/$PARTNER_ID" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json"

# Bulk-скрипт (выполнять аккуратно!):
docker compose exec postgres psql -U neironych -d neironych -t -c \
  "SELECT id FROM partners WHERE status = 'active';" | while read partner_id; do
  if [ ! -z "$partner_id" ]; then
    echo "Snapshotting partner: $partner_id"
    curl -X POST "https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/snapshot/$partner_id" \
      -H "Authorization: Bearer $ADMIN_API_TOKEN" \
      -H "Content-Type: application/json"
    echo ""
    sleep 0.5
  fi
done
```

### 9. Проверить, что snapshot создались

```bash
docker compose exec postgres psql -U neironych -d neironych -c \
  "SELECT partner_id, COUNT(*) FROM partner_prices WHERE partner_id IS NOT NULL GROUP BY partner_id;"
# Должны увидеть строки для каждого партнёра
```

### 10. Тестовая генерация

```bash
# Выбрать одного партнёра, у которого есть API ключ
# Создать тестовую генерацию через API
# Проверить в БД, что partner_price_rub совпадает с partner-specific ценой

docker compose exec postgres psql -U neironych -d neironych -c \
  "SELECT g.id, g.partner_id, g.model_slug, g.partner_price_rub, pp.price_rub as partner_specific_price 
   FROM generations g 
   LEFT JOIN partner_prices pp ON pp.model_id = g.model_id 
     AND pp.partner_id = g.partner_id 
     AND pp.mode = g.mode 
     AND pp.resolution = g.resolution 
   WHERE g.created_at > NOW() - INTERVAL '5 minutes' 
   ORDER BY g.created_at DESC LIMIT 5;"
```

## Для новых партнёров (начиная с этого момента)

При одобрении новой заявки партнёра:

```bash
# Получить partner_id нового партнёра
NEW_PARTNER_ID="uuid-здесь"

# Создать цены с 35% маржой
curl -X POST "https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/new-partner/$NEW_PARTNER_ID" \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json"

# Проверить
docker compose exec postgres psql -U neironych -d neironych -c \
  "SELECT mode, resolution, price_rub, provider_cost_usdt FROM partner_prices WHERE partner_id = '$NEW_PARTNER_ID' LIMIT 5;"
```

## Rollback (если что-то пошло не так)

```bash
# Остановить приём новых запросов (опционально)
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml stop app

# Откатить миграцию
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  run --rm app alembic downgrade -1

# Перезапустить
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  --profile telegram up -d app worker webhook_worker

# Или полный rollback через restore из бэкапа
# (следуйте ops/backup/README.md)
```

## Мониторинг после деплоя

1. **Логи приложения**:
   ```bash
   docker compose logs -f app worker
   # Следить за ошибками, связанными с PartnerPrice
   ```

2. **Margin incidents**:
   ```bash
   curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
     https://api.xn--e1aikcel5c5a.online/api/v1/billing/incidents
   # Проверить, нет ли новых negative_margin или low_margin инцидентов
   ```

3. **Метрики генераций**:
   ```bash
   docker compose exec postgres psql -U neironych -d neironych -c \
     "SELECT status, COUNT(*) FROM generations WHERE created_at > NOW() - INTERVAL '1 hour' GROUP BY status;"
   ```

4. **Проверить, что партнёры могут создавать генерации**:
   - Выбрать 2-3 активных партнёра
   - Создать тестовые генерации от их имени
   - Убедиться, что цены списываются корректно

## Ожидаемое поведение

- **Существующие партнёры** (после snapshot): цены заморожены на текущих значениях
- **Новые партнёры** (после создания через /new-partner): цены с 35% маржой
- **Партнёры без snapshot**: продолжают работать на глобальных ценах (как было)
- **Изменение глобального прайса**: не влияет на партнёров с partner-specific ценами

## Контакты при проблемах

- **Database issues**: проверить миграцию, constraint, индексы
- **API errors**: проверить логи app/worker, partner_id resolution
- **Financial issues**: сверить partner_price_rub в generations с partner_prices
- **Rollback**: использовать `alembic downgrade -1` + restore из бэкапа

---

**Подготовил**: Claude Sonnet 4.6  
**Дата**: 2026-10-02  
**PR**: #73  
**Документация**: DEPLOYMENT_PARTNER_PRICING.md
