# Проверка готовности — 2026-09-23

**Результат: исправления для release candidate; полный production/resale PASS не выдан.**
Область согласована: репозиторий и изолированные проверки, без production-деплоя и
платных операций. Исходный commit 3253fa0; дополнительно интегрированы изменения main до 20e098e.

## Исправлено

- Контрактная валидация видео по модели до резервирования денег; точное отображение кадров;
  отказ от молчаливого игнорирования unsupported request fields.
- Fail-closed key validation, нормализация повреждённых ответов, запрет автоматического
  повтора неоднозначного submit и безопасные content paths без произвольных redirects.
- Submit intent durable до обращения к провайдеру; regression test падения после acceptance.
- Исходный provider credential закреплён за задачей (additive migration 0014).
- Терминальная задача не опрашивается повторно; запросы admin dispatch/poll блокируют строку.
- Отложенные retries не занимают очередь перед готовыми задачами.
- Отклонение убыточного запроса при изменившемся FX до создания generation/reserve.
- Telegram admin callbacks защищены ADMIN_TELEGRAM_ID; кабинету разрешены только private chats;
  некорректная пагинация отклоняется, DB session живёт до завершения обработчика.
- Недостоверный safe-to-withdraw не публикуется как сумма в USDT.
- Production startup проверяет конфигурацию; readiness видит Redis outage и актуальную revision.
- Docker CMD запускает нужный процесс; hashes/versions зависимостей закреплены;
  production compose самостоятельный, без опубликованных PostgreSQL/Redis/MinIO портов,
  с read-only приложением, non-root, cap_drop и graceful shutdown.
- Изменения main по pool settings, late-success и CI-gated rollback сохранены.
- ORM согласован с PostgreSQL migrations, `alembic check` добавлен в CI.

## Доказательства

- До изменений: 47 tests passed, 3 PostgreSQL tests skipped.
- После интеграции: 111 tests passed, без skips, включая PostgreSQL; Ruff clean.
  CI проверяет конкретный SHA PR.
- Собран реальный Docker image, API и два worker запущены в отдельной Docker network.
- Readiness вернул ready/database=ok/redis=ok и нужную revision.
- После restart API/workers readiness восстановился.
- При остановке Redis API вернул 503.
- Миграции 0001–0014 применены к отдельной PostgreSQL 16; ORM schema check clean; migration 0014 downgrade/upgrade round-trip PASS.
- pip-audit: no known vulnerabilities на проверенном окружении; Bandit high/medium gate clean;
  detect-secrets не обнаружил секретов в runtime/ops/CI/Compose.
- SQLite используется для unit tests через metadata.create_all. Исторические миграции
  до 0014 не полностью совместимы с SQLite (0007 меняет constraints); migration gate — PostgreSQL.

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
DATABASE_URL="$ISOLATED_POSTGRES_URL" alembic upgrade head
DATABASE_URL="$ISOLATED_POSTGRES_URL" alembic check
TEST_POSTGRES_DATABASE_URL="$ISOLATED_POSTGRES_URL" python -m pytest -q
python -m ruff check .
docker build -t neironych:candidate .
```

## Оставшиеся блокеры resale

Это конкретные незакрытые требования, а не разрешение запускать частичный продукт:

1. Полный заявленный Seedance contract: video/audio references, uploads, edit и точный
   учёт input-video seconds. См. ARGOLINK_CONTRACT.md. Image/LLM остаются не включаемыми.
2. Реальный success smoke на оплаченных моделях и успешная загрузка результата отсутствуют
   в этой проверке. Нужны защищённые credentials и согласованный бюджет в staging.
3. Actual upstream cost, automatic FX/fallback, реальный wallet/provider balance,
   safe-to-withdraw и полноценные incident/funding/circuit-breaker сценарии не закончены.
4. Кабинет Telegram неполный: onboarding/consent, top-up, управление ключами, UUID search,
   support tickets/attachments, transfer/delete и admin confirmations требуют завершения.
5. `submitting` без task ID после crash требует сверки у провайдера. Не переводить его
   обратно в queued вслепую. Автоматическое обнаружение/уведомление и UI reconciliation ещё нужны.
6. Для webhook DNS rebinding нужна проверка egress на уровне соединения либо сетевой
   egress proxy. Текущая проверка DNS до HTTP-запроса сама по себе не закрывает TOCTOU.
7. Требуются восстановление encrypted backup в чистую БД, WAL/PITR, RTO drill,
   полноценный load test 20–50 запросов и проверка rollback на staging.
8. Внешние launch gates (домен/TLS, инфраструктура, branch protection, production secrets,
   согласованные тексты и правила обработки данных) в этой области работы не проверялись.

Пункты PRODUCTION_LAUNCH_CHECKLIST.md не отмечались PASS без соответствующих доказательств.
Изолированный success не доказывает operational readiness или полный product brief.

Применённые skills: team-lead, security-audit, devops, aiogram-codegen, bot-tester;
проектные api-security-best-practices и verification-before-completion.
