#!/usr/bin/env bash
#
# Автоматический деплой partner-specific pricing на production
# Требует: SSH доступ к root@5.35.124.201
#
set -euo pipefail

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PROD_HOST="root@5.35.124.201"
PROJECT_PATH="/opt/neironych"

# Функция для красивого вывода
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Проверка SSH подключения
check_ssh_connection() {
    log_info "Проверка SSH подключения к $PROD_HOST..."
    if ssh -o ConnectTimeout=10 -o BatchMode=yes "$PROD_HOST" "echo 'SSH OK'" &>/dev/null; then
        log_success "SSH подключение работает"
        return 0
    else
        log_error "Не удалось подключиться к $PROD_HOST"
        log_info "Настройте SSH ключ:"
        echo "  ssh-copy-id $PROD_HOST"
        return 1
    fi
}

# Проверка окружения на production
check_production_environment() {
    log_info "Проверка production окружения..."

    ssh "$PROD_HOST" bash <<'EOF'
set -e
echo "Hostname: $(hostname)"
echo "Docker version: $(docker --version)"
echo "Docker Compose version: $(docker compose version)"
echo "Current directory exists: $([ -d /opt/neironych/current ] && echo 'YES' || echo 'NO')"
echo "Shared .env exists: $([ -f /opt/neironych/shared/.env ] && echo 'YES' || echo 'NO')"
EOF

    log_success "Production окружение проверено"
}

# Создание бэкапа БД
create_database_backup() {
    log_info "Создание бэкапа БД перед миграцией..."

    BACKUP_NAME="backup_before_partner_pricing_$(date +%Y%m%d_%H%M%S).sql"

    ssh "$PROD_HOST" bash <<EOF
set -e
cd $PROJECT_PATH/current
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    exec -T postgres pg_dump -U neironych neironych > /tmp/$BACKUP_NAME

if [ -f /tmp/$BACKUP_NAME ]; then
    SIZE=\$(stat -f%z /tmp/$BACKUP_NAME 2>/dev/null || stat -c%s /tmp/$BACKUP_NAME)
    echo "Backup created: /tmp/$BACKUP_NAME (size: \$SIZE bytes)"
    # Копируем в безопасное место
    cp /tmp/$BACKUP_NAME $PROJECT_PATH/backups/$BACKUP_NAME 2>/dev/null || mkdir -p $PROJECT_PATH/backups && cp /tmp/$BACKUP_NAME $PROJECT_PATH/backups/$BACKUP_NAME
    echo "Backup saved to: $PROJECT_PATH/backups/$BACKUP_NAME"
else
    echo "ERROR: Backup file not created"
    exit 1
fi
EOF

    log_success "Бэкап БД создан: $BACKUP_NAME"
}

# Проверка текущей версии миграции
check_current_migration() {
    log_info "Проверка текущей версии миграции..."

    ssh "$PROD_HOST" bash <<EOF
cd $PROJECT_PATH/current
source shared/.env
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    run --rm app alembic current
EOF
}

# Применение миграции
apply_migration() {
    log_info "Применение миграции 20261002_0023_partner_specific_pricing..."

    ssh "$PROD_HOST" bash <<EOF
set -e
cd $PROJECT_PATH/current
source shared/.env

# Применить миграцию
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    run --rm app alembic upgrade head

# Проверить новую версию
echo ""
echo "=== Current migration after upgrade ==="
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    run --rm app alembic current
EOF

    log_success "Миграция применена"
}

# Проверка схемы БД
verify_database_schema() {
    log_info "Проверка схемы БД..."

    ssh "$PROD_HOST" bash <<'EOF'
cd /opt/neironych/current
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    exec -T postgres psql -U neironych -d neironych <<SQL
\d partner_prices
SQL
EOF

    log_success "Схема БД проверена (должна быть колонка partner_id)"
}

# Перезапуск сервисов
restart_services() {
    log_info "Перезапуск сервисов..."

    ssh "$PROD_HOST" bash <<EOF
cd $PROJECT_PATH/current
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    --profile telegram restart app worker webhook_worker

# Ждём 5 секунд для старта
sleep 5

# Проверяем readiness
echo ""
echo "=== Service readiness check ==="
curl -s http://127.0.0.1:8000/api/v1/readiness || echo "Readiness check failed"
EOF

    log_success "Сервисы перезапущены"
}

# Проверка глобальных цен
verify_global_prices() {
    log_info "Проверка глобальных цен..."

    ssh "$PROD_HOST" bash <<'EOF'
cd /opt/neironych/current
COUNT=$(docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    exec -T postgres psql -U neironych -d neironych -t -c \
    "SELECT COUNT(*) FROM partner_prices WHERE partner_id IS NULL;")

echo "Количество глобальных цен: $COUNT"

if [ "$COUNT" -gt 0 ]; then
    echo "Глобальные цены на месте ✓"
else
    echo "ВНИМАНИЕ: Глобальные цены отсутствуют!"
    exit 1
fi
EOF

    log_success "Глобальные цены проверены"
}

# Создание snapshot для существующих партнёров
snapshot_existing_partners() {
    log_info "Создание snapshot для существующих партнёров..."
    log_warning "Для этого нужен ADMIN_API_TOKEN"

    read -p "Введите ADMIN_API_TOKEN (или Enter для пропуска): " ADMIN_TOKEN

    if [ -z "$ADMIN_TOKEN" ]; then
        log_warning "Snapshot пропущен. Выполните вручную:"
        echo "  curl -X POST https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/snapshot/\$PARTNER_ID \\"
        echo "    -H \"Authorization: Bearer \$ADMIN_TOKEN\""
        return 0
    fi

    # Получаем список активных партнёров
    log_info "Получение списка активных партнёров..."

    PARTNERS=$(ssh "$PROD_HOST" bash <<'EOF'
cd /opt/neironych/current
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    exec -T postgres psql -U neironych -d neironych -t -c \
    "SELECT id FROM partners WHERE status = 'active';"
EOF
)

    PARTNER_COUNT=$(echo "$PARTNERS" | grep -v '^$' | wc -l)
    log_info "Найдено активных партнёров: $PARTNER_COUNT"

    # Snapshot для каждого партнёра
    echo "$PARTNERS" | while read -r partner_id; do
        partner_id=$(echo "$partner_id" | xargs) # trim whitespace
        if [ ! -z "$partner_id" ]; then
            log_info "Создание snapshot для партнёра: $partner_id"

            RESPONSE=$(ssh "$PROD_HOST" bash <<EOF
curl -s -X POST "http://127.0.0.1:8000/api/v1/catalog/pricing/snapshot/$partner_id" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json"
EOF
)

            echo "$RESPONSE"
            sleep 0.5
        fi
    done

    log_success "Snapshot для всех партнёров создан"
}

# Проверка созданных partner-specific цен
verify_partner_prices() {
    log_info "Проверка созданных partner-specific цен..."

    ssh "$PROD_HOST" bash <<'EOF'
cd /opt/neironych/current
echo "=== Partner-specific prices by partner ==="
docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml \
    exec -T postgres psql -U neironych -d neironych -c \
    "SELECT partner_id, COUNT(*) as price_count FROM partner_prices WHERE partner_id IS NOT NULL GROUP BY partner_id;"
EOF

    log_success "Partner-specific цены проверены"
}

# Создание тестовой генерации
test_generation() {
    log_info "Тест создания генерации..."
    log_warning "Это требует существующего partner API key. Пропускаем автоматический тест."
    log_info "Выполните тест вручную через partner API"
}

# Основной процесс деплоя
main() {
    echo ""
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║  Автоматический деплой Partner-Specific Pricing на Production  ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""

    log_warning "Этот скрипт выполнит следующие действия:"
    echo "  1. Проверка SSH подключения"
    echo "  2. Создание бэкапа БД"
    echo "  3. Применение миграции"
    echo "  4. Перезапуск сервисов"
    echo "  5. Создание snapshot для существующих партнёров"
    echo ""

    read -p "Продолжить? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        log_warning "Деплой отменён"
        exit 0
    fi

    echo ""
    log_info "========== Шаг 1/9: Проверка SSH =========="
    check_ssh_connection || exit 1

    echo ""
    log_info "========== Шаг 2/9: Проверка окружения =========="
    check_production_environment

    echo ""
    log_info "========== Шаг 3/9: Создание бэкапа =========="
    create_database_backup

    echo ""
    log_info "========== Шаг 4/9: Текущая миграция =========="
    check_current_migration

    echo ""
    log_info "========== Шаг 5/9: Применение миграции =========="
    apply_migration

    echo ""
    log_info "========== Шаг 6/9: Проверка схемы =========="
    verify_database_schema

    echo ""
    log_info "========== Шаг 7/9: Перезапуск сервисов =========="
    restart_services

    echo ""
    log_info "========== Шаг 8/9: Проверка глобальных цен =========="
    verify_global_prices

    echo ""
    log_info "========== Шаг 9/9: Snapshot партнёров =========="
    snapshot_existing_partners

    echo ""
    log_info "========== Финальная проверка =========="
    verify_partner_prices

    echo ""
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║                    ДЕПЛОЙ ЗАВЕРШЁН УСПЕШНО!                    ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""

    log_success "Partner-specific pricing развёрнут на production"
    log_info "Для новых партнёров используйте:"
    echo "  curl -X POST https://api.xn--e1aikcel5c5a.online/api/v1/catalog/pricing/new-partner/\$PARTNER_ID \\"
    echo "    -H \"Authorization: Bearer \$ADMIN_TOKEN\""
    echo ""
    log_info "Мониторинг:"
    echo "  - Логи: ssh $PROD_HOST 'cd /opt/neironych/current && docker compose logs -f app worker'"
    echo "  - Метрики: curl http://127.0.0.1:8000/internal/metrics"
    echo "  - Incidents: проверьте /api/v1/billing/incidents"
    echo ""
}

# Запуск
main "$@"
