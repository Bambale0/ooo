# 🚀 Production Deployment - Ready to Execute

## Current Status

**Main Branch:** SHA `7f47dec`  
**Code Ready:** ✅ Partner pricing + Asale provider (SHA `3985694`)  
**CI Status:** ✅ Passing  
**Deploy Status:** ⚠️ Blocked (missing secrets)  
**Blocker:** GitHub secrets not configured

---

## Quick Start (Для тебя)

### 1. Склонируй/обнови репозиторий

```bash
cd /d/code/dev/ooo
git pull origin main
# Теперь main на SHA 7f47dec с setup скриптом
```

### 2. Запусти setup скрипт

```bash
./ops/deploy/setup_production_secrets.sh
```

Скрипт спросит:
- **Production host address** (IP или hostname продакшн-сервера)
- **SSH private key path** (путь к приватному ключу для деплоя)
- Автоматически получит host fingerprint через `ssh-keyscan`

Скрипт установит через `gh` CLI:
- `DEPLOY_HOST`
- `DEPLOY_SSH_KEY`
- `DEPLOY_KNOWN_HOSTS`

### 3. Добавь ASALE_API_KEY на продакшн-сервер

```bash
ssh deploy@PRODUCTION_HOST
cd /opt/neironych/shared
echo 'ASALE_API_KEY=your-actual-asale-key-here' >> .env
```

### 4. Запусти deployment

```bash
# Вариант 1: Перезапусти failed workflow
gh run rerun 37054982482 --repo Bambale0/ooo

# Вариант 2: Триггерни новый CI (авто-запустит deploy)
git commit --allow-empty -m "trigger: production deployment"
git push origin main
```

### 5. Мониторь deployment

```bash
gh run watch --repo Bambale0/ooo
# или через веб
# https://github.com/Bambale0/ooo/actions
```

---

## Если продакшн-хост не готов

### Отключи автодеплой пока настраиваешь:

```bash
gh api --method PATCH repos/Bambale0/ooo/actions/variables/PRODUCTION_DEPLOY_ENABLED \
  -f value="false"
```

### Подготовь production host:

Смотри полную инструкцию в [docs/OPERATIONS.md](docs/OPERATIONS.md)

**Минимум что должно быть:**

```bash
# На продакшн-сервере
sudo useradd -m -s /bin/bash deploy
sudo mkdir -p /opt/neironych/shared/{nginx/ssl,releases}
sudo chown -R deploy:deploy /opt/neironych

# Как deploy пользователь
cd /opt/neironych/shared

# Создай .env из .env.example (с production значениями!)
# Создай .backup.env (смотри ops/backup/README.md)
# Положи TLS сертификаты в nginx/ssl/

# Установи Docker + Docker Compose
# Логин в GHCR: docker login ghcr.io (с GitHub PAT)
```

---

## Файлы для тебя

### Основной скрипт
```bash
./ops/deploy/setup_production_secrets.sh
```
Интерактивный, через `gh secret set`

### Документация
- `ops/deploy/README.md` - полная документация setup
- `DEPLOYMENT_SUMMARY_20261002.md` - что будет задеплоено
- `docs/OPERATIONS.md` - production setup guide
- `docs/HOST_AUTODEPLOY.md` - как работает deploy workflow

### Quickstart (всё в одном)
```bash
./QUICKSTART_DEPLOYMENT.sh
```
Проверит gh CLI, авторизацию, и запустит setup

---

## Что будет задеплоено

**SHA `3985694`** (уже в main, прошёл CI):

1. ✅ **Partner-specific pricing** с frozen snapshots
2. ✅ **Asale provider** адаптер и конфигурация
3. ✅ **3 миграции базы:**
   - `20261002_0023` - FX policy invoice snapshot
   - `20261002_0024` - Provider capability fallback cost
   - `20261002_0025` - Partner price snapshots
4. ✅ **UUID correlation traces** для generation lifecycle
5. ✅ **Native API diagnostics** улучшения

---

## После успешного deployment

### Проверь:

```bash
# 1. API readiness
curl https://PRODUCTION_HOST/api/v1/readiness
# Ожидаем: {"status":"ok","revision":"3985694..."}

# 2. Миграции применены
ssh deploy@PRODUCTION_HOST
cd /opt/neironych/current
docker compose -f docker-compose.prod.yml run --rm app alembic current
# Ожидаем: 20261002_0025

# 3. Partner snapshots созданы
docker compose -f docker-compose.prod.yml run --rm app python -c "
from app.infrastructure.database import get_session
from app.catalog.models import PartnerPriceSnapshot
async def check():
    async with get_session() as s:
        count = await s.scalar(select(func.count(PartnerPriceSnapshot.id)))
        print(f'Snapshots: {count}')
import asyncio
asyncio.run(check())
"

# 4. Financial incidents
docker compose -f docker-compose.prod.yml run --rm app python -c "
# Check for partner-economics incidents
"
```

---

## Rollback (если что-то пошло не так)

Deployment workflow имеет **автоматический rollback** при health check failure.

Ручной rollback:

```bash
ssh deploy@PRODUCTION_HOST
cd /opt/neironych
cat release-backups/<previous-sha>/rollback.json  # Там предыдущий image ID
# Восстанови предыдущий образ из manifest
```

---

## Контакты и помощь

- **Логи deployment:** https://github.com/Bambale0/ooo/actions/runs/37054982482
- **Issues:** https://github.com/Bambale0/ooo/issues
- **Документация:** `docs/` в репозитории

---

## Чеклист

- [ ] `gh` CLI установлен и авторизован
- [ ] Production host доступен по SSH
- [ ] `/opt/neironych/shared/.env` существует
- [ ] `/opt/neironych/shared/.backup.env` настроен
- [ ] TLS сертификаты в `/opt/neironych/shared/nginx/ssl/`
- [ ] Docker + Compose установлены
- [ ] GHCR login выполнен
- [ ] Запущен `./ops/deploy/setup_production_secrets.sh`
- [ ] ASALE_API_KEY добавлен в production `.env`
- [ ] Deployment триггернут
- [ ] Health check passed
- [ ] Migrations applied
- [ ] Snapshots verified

---

**Prepared by:** AI Agent (Claude Sonnet 4.6)  
**Date:** 2026-10-02  
**Main SHA:** 7f47dec  
**Deploy SHA:** 3985694
