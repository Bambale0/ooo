# Нейроныч

Закрытый B2B SaaS/API-сервис для партнёров, которые встраивают AI-генерацию в свои продукты и боты. Партнёр получает единый API Нейроныча, пополняет общий RUB-баланс, запускает генерации и сам определяет цену для своего конечного пользователя.

Первый upstream provider — ArgoLink, но ядро проектируется provider-agnostic: provider-specific ключи, task IDs, routing, закупочная стоимость и raw errors остаются внутренними.

> **Статус репозитория:** здесь зафиксирована продуктовая спецификация, implementation epics, launch checklist и начальный runtime-код EPIC 01. Реализованы базовый FastAPI-каркас, настройки, async SQLAlchemy/Alembic, Redis abstraction, health/readiness, Docker Compose, Nginx и первый партнёрский API-срез. Остальные функции из документации не считать готовыми, пока они не покрыты кодом и тестами.

## Содержание

- [Что строим](#что-строим)
- [Основные принципы](#основные-принципы)
- [Стартовые модели](#стартовые-модели)
- [Архитектура](#архитектура)
- [Финансовая модель](#финансовая-модель)
- [Generation lifecycle](#generation-lifecycle)
- [Платежи](#платежи)
- [Telegram](#telegram)
- [Webhooks](#webhooks)
- [Надёжность](#надёжность)
- [Инфраструктура и данные](#инфраструктура-и-данные)
- [Репозиторий](#репозиторий)
- [Работа с .agents](#работа-с-agents)
- [Как начать разработку](#как-начать-разработку)
- [Git workflow](#git-workflow)
- [Production launch](#production-launch)
- [Источники истины](#источники-истины)

## Что строим

Нейроныч — это закрытый партнёрский gateway поверх AI providers.

Партнёр:

1. подаёт заявку через Telegram-бот;
2. после approval создаёт один или несколько API keys;
3. пополняет RUB-баланс через Crypto Bot;
4. интегрирует API Нейроныча в свой продукт;
5. создаёт генерации;
6. получает status/result через API и terminal webhooks;
7. сам перепродаёт генерацию своим конечным пользователям по своей цене.

Наша система отвечает за:

- partner accounts и доступ;
- API keys;
- model catalog;
- partner pricing;
- billing и ledger;
- provider routing;
- ArgoLink integration;
- generation orchestration;
- idempotency;
- payments;
- webhooks;
- support;
- отчётность;
- alerts;
- safe-to-withdraw;
- provider working float;
- backups/DR;
- production readiness.

## Основные принципы

### 1. Цена для партнёра, а не конечного клиента

Термин проекта — **partner price / цена для партнёра**.

Это цена, которую Нейроныч списывает с RUB-баланса партнёра. Цена, по которой партнёр продаёт услугу своему конечному пользователю, находится вне нашей системы.

### 2. Provider details скрыты

Partner-facing API не должен раскрывать:

- название фактического provider;
- upstream API key;
- provider task ID;
- raw upstream error;
- upstream cost;
- внутренний routing;
- внутреннюю маржу.

### 3. Full capability support

Если модель включена в production, она должна поддерживать весь заявленный provider contract для этой модели: modes, resolutions, durations, references и остальные параметры.

Урезанная интеграция модели не считается готовой к production.

### 4. Идемпотентность

Все критичные side effects должны быть идемпотентны или защищены от повторного применения:

- generation creation;
- provider submit;
- balance settlement;
- manual credit;
- refunds/reversals;
- webhook delivery;
- admin actions.

Duplicate request не должен создавать duplicate generation, double charge или double credit.

### 5. PostgreSQL — источник истины

Критичные финансовые и durable-состояния хранятся в PostgreSQL.

Redis используется для очередей, coordination, locks и ускорения, но не должен быть единственным источником финансовой или бизнес-истины.

## Стартовые модели

Первый production video catalog:

- Seedance 2;
- Seedance 2.5;
- Grok Imagine.

Архитектура должна также поддерживать:

- images;
- LLM.

На старте images/LLM могут оставаться admin-only до прохождения production enable gates.

### Enable gate модели

Новая модель может быть включена партнёрам только когда одновременно готовы:

- полная provider integration;
- partner price;
- документация;
- успешный smoke test.

Enable должен атомарно публиковать:

- API availability;
- `/pricing`;
- public price/docs;
- changelog.

## Архитектура

Целевой стек:

- **Language:** Python
- **API:** FastAPI
- **Telegram:** aiogram
- **Database:** PostgreSQL
- **ORM:** SQLAlchemy 2
- **Migrations:** Alembic
- **Queue / coordination:** Redis
- **HTTP client:** httpx
- **Config:** Pydantic Settings
- **Containers:** Docker Compose
- **Reverse proxy:** Nginx
- **Object storage:** MinIO
- **Metrics:** Prometheus-compatible
- **Logs:** structured JSON

Планируемые доменные модули:

```text
accounts
auth
catalog
providers
generations
billing
payments
webhooks
telegram
support
reporting
alerts
infrastructure
```

### Provider abstraction

ArgoLink — первый adapter, но domain layer не должен зависеть от его конкретных response models.

Ожидаемый provider interface включает:

- validate key;
- read provider balance/quota;
- read provider pricing/cost where supported;
- submit generation;
- poll generation;
- cancel where supported;
- normalize errors;
- health checks;
- result retrieval/refresh capability;
- capability metadata.

### Domains

Планируемые публичные точки:

- API: `api.нейроныч.online`
- Docs: `docs.нейроныч.online`

Telegram transport может использовать отдельный stateless proxy outside RF, при этом partner API должен идти напрямую в RF production.

## Финансовая модель

### Partner balance

Partner-facing деньги и цены — в RUB.

- balance precision: `0.01 ₽`;
- manual +/- adjustments допустимы;
- balance может стать отрицательным в аварийных сценариях;
- новые generations при отрицательном/недостаточном балансе блокируются.

### Internal currency

Внутренний provider/economic контур — **USDT**.

Используем:

- PostgreSQL `NUMERIC`;
- Python `Decimal`;
- high precision на промежуточных вычислениях;
- `ROUND_HALF_UP` только на финальном partner charge до `0.01 RUB`.

Binary float для денег запрещён.

### Price snapshots

Partner price фиксируется при создании generation.

Если админ меняет цену:

- новая цена действует только на новые generations;
- queued/in-flight generations продолжают жить по старому snapshot;
- historical operations не пересчитываются.

### Margin

Partner price ниже актуальной provider cost сохранить нельзя.

Low-margin threshold:

- global default;
- override на model;
- override на model + mode + resolution.

Более точный override имеет приоритет.

Если margin положительная, но ниже threshold — generation продолжается, админ получает one-shot alert на incident.

Если provider cost >= partner price — новые generation requests не создаются и получают нейтральный `503 provider_temporarily_unavailable`.

### Safe-to-withdraw

Admin metric показывается в **USDT**.

Он должен учитывать:

- реальные partner obligations;
- active generation reserves;
- required provider float;
- paid-but-not-credited deposits;
- current required reserve;
- actual accessible wallet balance;
- recorded profit withdrawals.

Значение может быть отрицательным, например `-120 USDT`, чтобы явно показывать дефицит покрытия.

Факт вывода прибыли — только accounting record, он не инициирует реальный Crypto Bot/on-chain transfer.

## Generation lifecycle

Основные состояния:

```text
queued
  ↓
sent_to_provider
  ↓
processing
  ↓
completed

terminal alternatives:
failed
timeout
cancelled
```

### Idempotency

Каждый create request обязан содержать `idempotency_key`.

После accepted generation:

- key сохраняется forever;
- повтор той же пары partner/key + idempotency_key возвращает тот же UUID;
- duplicate provider job не создаётся;
- duplicate charge не создаётся.

Если запрос отклонён **до создания generation** по economic/availability gate:

- UUID не создаётся;
- reserve не создаётся;
- idempotency_key не считается использованным;
- тот же key можно повторить позже.

### Late provider success

Если upstream завершил generation после нашего timeout:

- generation может быть переведена в completed;
- result становится доступным;
- корректный partner charge применяется;
- balance/coverage при необходимости может уйти в минус;
- последующие generations блокируются до восстановления.

## Платежи

Crypto Bot используется как удобный способ расчёта, а не как единица partner accounting.

### Top-up

Партнёр вводит целую RUB-сумму:

- минимум: `1000 ₽`;
- максимального лимита нет.

Система конвертирует её в поддерживаемый crypto asset по текущему rate и создаёт invoice.

Invoice TTL — 1 час.

### Важное правило

**Auto-credit отсутствует.**

После фактической оплаты:

1. payment подтверждается;
2. партнёр видит статус «Оплачено, ожидает зачисления администратором»;
3. админ получает alert;
4. система показывает рекомендуемое provider funding;
5. админ вручную подтверждает зачисление;
6. только тогда увеличивается partner RUB balance.

Зачисляется ровно requested RUB amount.

### Refunds

Refund details видит только админ.

Если ранее зачисленный invoice refunded:

- создаётся append-only reverse adjustment;
- используется исходный operation snapshot;
- current FX не применяется;
- partial refund разворачивает только соответствующую долю;
- partner history показывает нейтральную «Корректировка баланса».

## Telegram

На старте Telegram UI — только на русском.

### Partner cabinet

Основные разделы:

- Баланс;
- Пополнить;
- API keys;
- История;
- Поиск generation по UUID;
- Документация;
- Support;
- Удаление аккаунта.

### Admin

Один admin Telegram ID.

Admin control plane должен включать:

- applications;
- partners;
- provider keys;
- model catalog;
- partner pricing;
- margin thresholds;
- payments waiting credit;
- provider float;
- generation search;
- incidents;
- safe-to-withdraw;
- profit withdrawal records;
- support;
- health;
- test/prod switch.

Критичные действия подтверждаются отдельной кнопкой.

## Webhooks

Webhooks используются только для generation lifecycle terminal events:

- completed;
- failed;
- timeout;
- cancelled.

### Signature

HMAC-SHA256:

```text
timestamp + "." + raw_body
```

Delivery metadata должно включать как минимум:

- timestamp;
- signature;
- event_id;
- delivery_id;
- attempt.

### Delivery

- at least once;
- retry каждые 15 минут;
- до 24 часов.

Manual resend:

- same business `event_id`;
- новый `delivery_id`;
- увеличенный `attempt`;
- fresh timestamp;
- original snapshot webhook URL/secret.

## Надёжность

### Queue

Очередь должна:

- быть durable;
- переживать deploy/restart;
- сохранять accepted generation;
- использовать fair strict-ish round-robin между партнёрами;
- по возможности сохранять порядок внутри одного партнёра.

### Provider retry

Для 429/temp5xx:

- уважать `Retry-After`;
- иначе exponential backoff + jitter.

### Acceptance timeout

Provider acceptance timeout: максимум 15 минут.

Если task не принят:

- generation завершает соответствующий timeout/fail flow;
- partner reserve освобождается;
- повтор terminal generation требует новый idempotency key.

### Circuit breaker

Provider может автоматически исключаться из routing при массовых технических сбоях.

Recovery:

1. 3 успешных free health checks с интервалом 1 минута;
2. ограниченный real traffic по одной задаче;
3. 3 успешных real generations подряд;
4. возврат в normal routing.

Financial loss сам по себе не должен открывать circuit breaker.

## Инфраструктура и данные

### Production

Начальный production:

- один VPS в РФ;
- отдельный RF storage VPS с MinIO;
- test/prod изолированы;
- production/business data хранятся в РФ.

### Backup

План:

- PostgreSQL continuous WAL archive;
- daily base/full backup;
- client-side encryption;
- MinIO storage;
- rolling retention 14 дней;
- weekly automatic restore-test;
- monthly DR drill.

### Disaster recovery

Целевой полный RTO — до 15 минут.

Автономный recovery runbook хранится у владельца **вне GitHub и серверов**. В нём не должно быть самих секретов — только инструкция, где их получить.

DR FAIL блокирует следующий production release до исправления и успешного повторного drill.

## Репозиторий

Текущая структура:

```text
.
├── .agents/                         # git submodule с библиотекой Agent Skills
├── .gitmodules
├── AGENTS.md                        # обязательные инструкции AI-агентам
├── PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md
├── IMPLEMENTATION_EPICS.md
├── PRODUCTION_LAUNCH_CHECKLIST.md
└── README.md
```

### Основные документы

| Файл | Назначение |
|---|---|
| `PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md` | Главный источник подтверждённых продуктовых решений |
| `IMPLEMENTATION_EPICS.md` | 25 implementation epics, зависимости и rollout waves |
| `IMPLEMENTATION_STATUS.md` | Текущий dev-status реализации по эпикам и launch checklist |
| `PRODUCTION_LAUNCH_CHECKLIST.md` | PASS/FAIL/WARN production launch gate |
| `AGENTS.md` | Обязательные правила для AI-агентов |
| `.agents/` | Библиотека skills, обязательная для нетривиальных задач |

## Работа с .agents

`.agents` подключён как git submodule.

Клонировать проект нужно с submodules:

```bash
git clone --recurse-submodules git@github.com:Bambale0/ooo.git
cd ooo
```

Если репозиторий уже клонирован:

```bash
git submodule update --init --recursive
```

### Обязательное правило для AI-агентов

Перед любой нетривиальной задачей агент обязан:

1. прочитать `AGENTS.md`;
2. найти релевантные skills в `.agents/SKILLS_INDEX.md`;
3. выбрать минимальный достаточный набор;
4. прочитать их `SKILL.md`;
5. следовать им при реализации и проверке.

Не нужно загружать весь skill catalog в контекст.

## Как начать разработку

Runtime skeleton уже добавлен.

Проверенный локальный Python-запуск:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

Запуск API без Docker:

```bash
python -m uvicorn app.main:app --reload
```

Production-like test contour через Docker Compose:

```bash
docker compose up --build
```

После старта API доступен на `http://localhost:8000`, Nginx proxy — на `http://localhost:8080`, OpenAPI docs — на `/docs`.

Миграции:

```bash
alembic upgrade head
```

Минимально реализованный продуктовый срез:

- partner application;
- admin approval;
- API key выпуск;
- model catalog и partner pricing;
- manual balance adjustment;
- idempotent generation creation с price snapshot и reserve ledger entry;
- health/readiness checks.

## Git workflow

Для code changes:

1. создать branch от актуального `main`;
2. сделать минимальный связный набор изменений;
3. выполнить релевантные tests/checks;
4. открыть PR;
5. дождаться обязательного CI;
6. merge только после зелёных checks.

Direct code push в `main` должен быть запрещён.

Планируемый CI:

- unit tests;
- integration tests;
- smoke tests;
- secret scanning;
- dependency vulnerability scanning;
- static analysis;
- security checks.

Планируемый deploy:

- GitHub-hosted Actions;
- private GHCR;
- immutable Docker image по commit SHA;
- SSH под отдельным deploy user;
- health-check перед traffic switch;
- automatic rollback;
- migrations по `expand -> deploy -> contract`.

## Production launch

Запуск регулируется `PRODUCTION_LAUNCH_CHECKLIST.md`.

Статусы:

- `PASS`;
- `FAIL`;
- `WARN`;
- `N/A`.

Любой `CRITICAL FAIL` блокирует production launch.

Главные launch blockers:

1. деньги и ledger;
2. idempotency;
3. generation lifecycle;
4. provider routing/fallback;
5. backup/restore;
6. security/isolation.

Перед первым платным партнёром обязателен реальный end-to-end smoke:

```text
partner application
  -> approval
  -> API key
  -> Crypto Bot payment
  -> manual credit
  -> generation request
  -> provider processing
  -> completed result
  -> webhook
  -> partner charge
  -> provider cost
  -> margin
  -> history/reporting
  -> safe-to-withdraw
```

После этого отдельно проверяется duplicate idempotency request — второй provider generation не должен появиться.

## Источники истины

При конфликте документации использовать следующий порядок:

1. актуальное прямое решение владельца;
2. `AGENTS.md` для процесса работы агентов;
3. более свежее подтверждённое решение в `PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md`;
4. `IMPLEMENTATION_EPICS.md`;
5. `PRODUCTION_LAUNCH_CHECKLIST.md`;
6. текущая реализация и её tests — для фактически уже реализованного поведения.

Implementation epics и README не должны самовольно менять продуктовые решения.

## Security note

Никогда не коммитить:

- API keys;
- provider keys;
- Telegram bot tokens;
- webhook secrets;
- private keys;
- production credentials;
- recovery secrets.

Логи не должны содержать plaintext secrets.

---

Проект развивается specification-first: сначала фиксируется поведение и acceptance criteria, затем оно реализуется небольшими проверяемыми PR.
