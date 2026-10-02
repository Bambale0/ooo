# Быстрый старт: Деплой Partner-Specific Pricing

## Шаг 1: Настройка SSH ключа (один раз)

Выполните в Git Bash:

```bash
# Скопировать SSH ключ на production (вручную, один раз)
ssh-copy-id -i ~/.ssh/neironych_prod_ed25519 root@5.35.124.201
# Пароль: 25896311
```

После этого проверьте подключение:

```bash
ssh root@5.35.124.201 "hostname && whoami"
# Должно вывести: hostname и root без запроса пароля
```

## Шаг 2: Запуск автоматического деплоя

После настройки SSH ключа выполните:

```bash
cd /d/code/dev/ooo
./deploy_partner_pricing.sh
```

Скрипт выполнит:
1. ✅ Проверку SSH подключения
2. ✅ Создание бэкапа БД
3. ✅ Применение миграции `20261002_0023`
4. ✅ Перезапуск сервисов
5. ✅ Проверку схемы
6. ✅ Snapshot существующих партнёров (требует ADMIN_API_TOKEN)

## Шаг 3: Проверка результата

```bash
# Подключиться к production
ssh root@5.35.124.201

# Проверить миграцию
cd /opt/neironych/current
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
  run --rm app alembic current
# Должно быть: 20261002_0023

# Проверить partner-specific цены
docker compose exec postgres psql -U neironych -d neironych -c \
  "SELECT partner_id, COUNT(*) FROM partner_prices WHERE partner_id IS NOT NULL GROUP BY partner_id;"
```

## Альтернатива: Ручной деплой (если SSH ключ не настроен)

Следуйте инструкциям в [MANUAL_DEPLOY_INSTRUCTIONS.md](MANUAL_DEPLOY_INSTRUCTIONS.md)

## После деплоя

Для новых партнёров используйте эндпоинт с 35% маржой:

```bash
curl -X POST https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/new-partner/$PARTNER_ID \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Troubleshooting

**SSH не подключается:**
```bash
# Проверьте права на ключ
chmod 600 ~/.ssh/neironych_prod_ed25519

# Попробуйте явно указать ключ
ssh -i ~/.ssh/neironych_prod_ed25519 root@5.35.124.201
```

**Миграция не применяется:**
```bash
# Проверьте текущую версию
alembic current

# Откат при необходимости
alembic downgrade -1
```

**Сервисы не стартуют:**
```bash
# Проверьте логи
docker compose logs -f app worker

# Проверьте readiness
curl http://127.0.0.1:8000/api/v1/readiness
```
