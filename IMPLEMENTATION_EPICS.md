# Нейроныч SaaS — implementation epics

> Основано на `PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md`.
> Цель документа — разбить реализацию на независимые, проверяемые эпики с чёткими границами, зависимостями и критериями готовности.
> При конфликте этот документ не заменяет product brief: источник продуктовой истины — актуальная спецификация.

## Общие правила реализации

- Python / FastAPI / aiogram / PostgreSQL / Redis / SQLAlchemy 2 / Alembic / httpx.
- Критичные финансовые и durable-состояния — PostgreSQL. Redis не является единственным источником истины.
- Любая production-модель должна поддерживать полный upstream contract.
- Partner-facing деньги и цены — RUB. Внутренний финансовый контур — USDT.
- Любая денежная операция должна быть воспроизводима из immutable snapshots и append-only истории.
- Partner-facing ошибки не раскрывают upstream provider, закупочную цену, маржу и внутренний routing.
- Все code changes — через PR; CI должен блокировать критические ошибки.
- Production и test изолированы по DB/Redis/secrets/config.
- Все внешние side effects должны быть идемпотентны или защищены от повторного применения.

---

# EPIC 01. Базовый каркас приложения и архитектурные границы

## Цель

Создать минимальный production-grade каркас, на котором можно безопасно развивать все остальные эпики без последующего переписывания структуры проекта.

## Scope

- FastAPI application factory.
- aiogram bot application.
- единый settings layer через Pydantic Settings.
- SQLAlchemy 2 async engine/session.
- Alembic.
- Redis client abstraction.
- structured JSON logging.
- request correlation id / trace id.
- базовый health/readiness endpoint.
- Dockerfile + Docker Compose для app/postgres/redis/nginx.
- разделение модулей:
  - accounts
  - auth
  - catalog
  - providers
  - generations
  - billing
  - payments
  - webhooks
  - telegram
  - support
  - reporting
  - alerts
  - infrastructure

## Важные решения

- доменная логика не должна зависеть напрямую от FastAPI/aiogram handlers;
- provider adapters реализуются через интерфейс/протокол;
- деньги храним через `NUMERIC`, не float;
- UTC внутри БД; отображение локализуется на границе интерфейса;
- все ключевые IDs — UUID, кроме внешних provider IDs.

## Acceptance criteria

- сервис поднимается одной командой в test-контуре;
- migrations применяются с нуля;
- readiness падает при недоступной DB;
- Redis outage не повреждает финансовое состояние;
- минимальный test suite проходит в CI;
- секреты не зашиты в repo.

## Зависимости

Нет. Это стартовый эпик.

---

# EPIC 02. Партнёрские аккаунты, заявки, согласия и lifecycle

## Цель

Реализовать полный lifecycle партнёра от заявки до удаления/повторной регистрации.

## Scope

### Заявка

Поля:
- Telegram ID;
- имя/название компании;
- название проекта/бота;
- статус application: pending / approved / rejected.

### Consent

Хранить:
- document type;
- version;
- timestamp;
- Telegram ID;
- факт согласия.

### Правила

- duplicate pending application по одному TG ID не создаётся;
- после reject можно подать новую заявку сразу;
- approval возможен только после валидных upstream keys для всех routing providers;
- после approval production account существует даже с нулевым балансом;
- один account = один Telegram ID;
- transfer на новый TG ID делается вручную админом после проверки;
- старый TG доступ при transfer отзывается сразу;
- delete account немедленный после подтверждения;
- delete запрещён при отрицательном балансе;
- положительный остаток не возвращается;
- после delete возможна новая заявка с тем же TG ID как новый account;
- lifetime free-test eligibility считается по TG ID и не восстанавливается.

## Data model

- partner_accounts
- partner_applications
- consent_acceptances
- partner_account_state_history
- telegram_identity_transfers

## Acceptance criteria

- невозможно одобрить account без всех required provider keys;
- повторный submit pending заявки не создаёт дубликат;
- reject + new application создаёт новую запись;
- delete корректно инвалидирует keys/access;
- повторная регистрация не восстанавливает старый баланс/ключи/историю;
- обновление terms создаёт persistent notice до принятия новой версии.

## Зависимости

EPIC 01, EPIC 05.

---

# EPIC 03. API keys, authentication и API versioning

## Цель

Дать партнёрам безопасную аутентификацию, управление ключами и предсказуемую эволюцию API.

## Scope

### Partner API keys

- unlimited keys per partner;
- label;
- secret показывается один раз;
- hash в БД;
- masked representation после создания;
- shared partner balance;
- без per-key spend limits;
- без IP allowlist;
- immediate revoke для новых requests;
- webhook URL + webhook secret на ключ.

### Admin API key

- ровно один active;
- rotation немедленно инвалидирует старый;
- usage считается по actual provider cost без markup.

### Versioning

- current major: `/v1`;
- будущий `/v2`;
- previous major живёт 90 дней;
- countdown в docs/cabinet;
- Telegram reminder за 7 дней;
- после expiration: HTTP 410 + `api_version_expired`;
- admin видит, кто ещё использует старую major version.

## Acceptance criteria

- украденный revoked key не может создать новый request;
- старый major после deadline возвращает 410;
- секрет невозможно восстановить из БД;
- rotation admin key атомарна;
- usage по API version виден админу.

## Зависимости

EPIC 01, EPIC 02.

---

# EPIC 04. Каталог моделей, capabilities и partner pricing

## Цель

Создать единый каталог моделей, конфигураций, partner prices и upstream costs.

## Scope

### Catalog

Старт:
- Seedance 2
- Seedance 2.5
- Grok Imagine

Архитектурно:
- video;
- image;
- LLM.

### Model state

- disabled;
- admin-only;
- enabled for all partners.

### Production enable gate

Перед enable обязательны:
- full provider integration;
- partner price;
- docs;
- successful smoke test.

Enable публикует атомарно:
- API availability;
- `/pricing`;
- public price/docs;
- changelog.

### Pricing

Video:
- model + mode + resolution;
- RUB/sec.

Image:
- per generation/resolution.

LLM:
- per 1M input;
- cached input;
- output;
- optional cache write.

### Price lifecycle

- partner price задаёт админ;
- одна глобальная partner price для всех партнёров;
- изменение действует сразу после confirmation;
- только для новых generations;
- in-flight использует price snapshot;
- historical не пересчитывается;
- цена ниже provider cost сохранить нельзя;
- price history бессрочная append-only.

### Margin thresholds

- global default 30%;
- override на model;
- override на model+mode+resolution;
- priority: configuration > model > global;
- low-margin alert не блокирует positive-economics generation;
- negative economics блокирует новые requests.

## Acceptance criteria

- нельзя включить модель без всех enable gates;
- нельзя сохранить partner price ниже себестоимости;
- price update атомарно отражается во всех pricing surfaces;
- новый request получает новый price snapshot, старый request не меняется;
- threshold resolver корректно применяет приоритеты;
- история prices/thresholds сохраняется бессрочно.

## Зависимости

EPIC 01, EPIC 05, EPIC 06.

---

# EPIC 05. Provider abstraction и ArgoLink adapter

## Цель

Сделать ArgoLink первым provider adapter без привязки ядра к его внутреннему API.

## Scope

Provider interface:
- validate_key;
- get_balance/quota;
- get_cost/pricing if supported;
- submit_generation;
- get_generation;
- cancel_generation if supported;
- normalize_error;
- health_check;
- fetch_result/refresh_result if supported;
- capability metadata.

### ArgoLink adapter

- один upstream key на partner;
- admin key отдельно;
- полная поддержка доступных параметров выбранных production models;
- input/reference contract не расширяем своими upload endpoints;
- лимиты inherit от ArgoLink model/endpoint;
- polling/result flow;
- сохраняем provider task id только внутренне;
- partner никогда не видит provider name/task id/raw error.

### Provider key lifecycle

- create/paste;
- validate before activation;
- atomic replace;
- invalid key исключает provider только для этого partner;
- sole provider invalid => новые generation blocked + emergency alert.

## Acceptance criteria

- один и тот же domain generation request может быть отправлен через adapter;
- provider-specific errors нормализуются;
- invalid provider key не протекает в partner-facing response;
- key replacement не ломает in-flight tasks;
- capability mismatch обнаруживается до routing.

## Зависимости

EPIC 01.

---

# EPIC 06. Routing, fallback и provider economics

## Цель

Реализовать детерминированный routing по глобальному приоритету без silent degradation.

## Scope

### Routing rules

- global priority per model;
- не выбирать динамически cheapest/fastest;
- slow-but-healthy provider остаётся первым;
- partner-level deviation только из-за invalid key, unavailability, economics;
- fallback только при полном equivalence request capabilities;
- никакой тихой смены resolution/mode/duration/reference semantics.

### Fallback

Allowed when:
- primary не принял task;
- primary failed uncharged;
- charged-but-no-result provider incident case.

### Charged failure

- partner платит максимум один normal partner charge;
- extra provider cost => provider_incident_loss;
- all fallback failed => partner charge 0, reserve returned, upstream costs remain incident loss.

### Negative economics

Если актуальная себестоимость >= partner price:
- не создавать generation UUID/reserve;
- HTTP 503;
- `provider_temporarily_unavailable`;
- idempotency_key остаётся reusable;
- admin получает one-shot incident alert.

## Acceptance criteria

- routing всегда следует configured priority;
- fallback не ухудшает request;
- charged failure не приводит к double partner charge;
- negative economics не расходует idempotency key;
- provider details не видны партнёру.

## Зависимости

EPIC 04, EPIC 05.

---

# EPIC 07. Generation orchestration, idempotency и state machine

## Цель

Создать надёжный lifecycle генерации от API request до terminal state.

## Status model

- queued
- sent_to_provider
- processing
- completed
- failed
- timeout
- cancelled

## Scope

### Create generation

- Bearer auth;
- обязательный `idempotency_key`;
- validate model/capabilities;
- price snapshot;
- provider-cost estimate;
- retail/partner charge reserve;
- cost reserve;
- UUID;
- durable queue enqueue.

### Idempotency

- accepted generation key хранится forever;
- same partner/key + same idempotency_key => same UUID;
- no duplicate charge;
- pre-creation reject не consume key.

### Cancellation

- только если provider реально подтверждает cancel до charge;
- иначе `cancellation_not_supported`;
- fake cancellation запрещён.

### Late success

Если provider success пришёл после нашего timeout:
- generation становится completed;
- result публикуется;
- charge применяется;
- balances могут уйти в минус;
- новые gens после этого blocked до recovery.

## Acceptance criteria

- двойной create не создаёт duplicate upstream job;
- deploy/restart не теряет accepted job;
- state transitions валидируются;
- terminal state повторно не биллится;
- late success корректно отражается в ledger.

## Зависимости

EPIC 03, EPIC 04, EPIC 05, EPIC 06, EPIC 08.

---

# EPIC 08. Финансовый ledger, balances и reservations

## Цель

Создать финансовое ядро, где любую сумму можно восстановить из истории без ручных догадок.

## Принципы

- partner-facing balance: RUB;
- internal management currency: USDT;
- `NUMERIC` high precision;
- final partner charge: ROUND_HALF_UP to 0.01 RUB;
- никаких float.

## Scope

### Partner balance

- append-only ledger;
- current balance projection;
- positive/negative values;
- manual adjustment +/- с internal reason;
- partner видит neutral `Корректировка баланса`.

### Reservations

При accepted generation:
- reserve max partner charge;
- reserve expected provider cost coverage;
- release/settle atomically.

### FX snapshot

Для completed generation:
- immutable RUB/USDT rate snapshot;
- source;
- timestamp;
- revenue_usdt;
- actual_provider_cost_usdt;
- margin_usdt;
- margin_percent.

### FX fallback chain

1. fresh automatic rate;
2. manual fallback, если задан;
3. last known automatic rate.

### Historical cost coverage

- snapshot при manual credit top-up;
- не переоценивается;
- используется для audit/reconciliation;
- не является единственным runtime gate.

### Current required reserve

- рассчитывается по актуальной upstream economics;
- используется для safe-to-withdraw/risk control.

## Acceptance criteria

- balance выводится как сумма ledger entries;
- double settlement невозможен;
- historical operation не меняется при новом FX rate;
- negative balance поддерживается;
- manual corrections не удаляют исходные записи.

## Зависимости

EPIC 01, EPIC 04.

---

# EPIC 09. Crypto Bot payments и ручное зачисление

## Цель

Принимать криптоплатёж как транспорт денег, но управлять partner balance вручную и предсказуемо.

## Scope

### Invoice creation

Партнёр задаёт:
- integer RUB amount;
- минимум 1000 RUB;
- без max.

Система:
- конвертирует в supported crypto asset по current rate;
- создаёт invoice TTL 1 hour;
- связывает invoice с partner.

### Payment states

- pending;
- paid_waiting_credit;
- credited;
- expired;
- refunded/cancelled/manual-final.

### Rules

- auto-credit запрещён;
- после provider payment confirmation партнёр видит “Оплачено, ожидает зачисления администратором”;
- paid amount immediately становится obligation для safe-to-withdraw;
- expired-but-paid позднее тоже попадает в manual credit flow;
- duplicate provider webhook не double-credit;
- technical failure после admin credit action retry до success;
- requested RUB amount зачисляется exactly;
- FX difference <=1% просто лог;
- >1% prominent admin warning, но credit разрешён.

### Refund

Если credit уже был:
- full refund => automatic reverse adjustment;
- partial refund => proportional reverse;
- используются original snapshots, не current FX;
- balance может стать negative;
- partner видит только neutral balance adjustment;
- refund details admin-only.

## Acceptance criteria

- paid invoice сам balance не увеличивает;
- admin credit идемпотентен;
- duplicate callbacks безопасны;
- partial refund корректно пересчитывает связанный historical coverage;
- unpaid invoice не влияет на safe-to-withdraw.

## Зависимости

EPIC 02, EPIC 08.

---

# EPIC 10. Safe-to-withdraw, working capital и profit withdrawals

## Цель

Дать владельцу реальную оценку доступной прибыли в USDT без риска вытащить деньги, необходимые для исполнения обязательств.

## Scope

### Safe-to-withdraw inputs

- current partner obligations;
- current required reserve;
- active generation reserves;
- paid-but-not-credited deposits;
- required provider float;
- provider incident effects;
- actual accessible USDT wallet balance;
- previously recorded profit withdrawals.

### Display

- label: USDT;
- допускается negative value;
- negative означает coverage deficit;
- stale Crypto Bot balance permitted;
- показывать freshness: “обновлён N минут назад”.

### Profit withdrawal record

Action:
- accounting-only;
- не инициирует on-chain/Crypto Bot transfer;
- fields:
  - amount USDT;
  - datetime;
  - internal reason/comment.

History:
- append-only;
- edit/delete запрещены;
- correction только обратной записью;
- partial correction допустима.

### Override

Если amount > safe-to-withdraw или safe-to-withdraw < 0:
- critical warning;
- mandatory textual override reason;
- allow proceed.

## Acceptance criteria

- safe-to-withdraw может быть отрицательным;
- paid uncredited deposit уменьшает показатель;
- refund снимает соответствующее obligation;
- stale wallet snapshot явно помечен;
- withdrawal corrections не мутируют исходную запись.

## Зависимости

EPIC 08, EPIC 09, EPIC 14.

---

# EPIC 11. Provider float и procurement funding control

## Цель

Контролировать достаточность upstream working balance без хранения всей клиентской выручки у provider.

## Scope

- current provider float;
- active provider-cost reservations;
- expected next-hour demand;
- target float calculation;
- recommended provider top-up для admin payment card;
- if float below target => admin alert;
- if partner funded but provider float insufficient:
  - durable queue;
  - wait up to 15 minutes;
  - auto-resume if restored;
  - after 15m => timeout/fail/release reserve.

### Rules

- обычная generation не должна финансироваться owner capital;
- provider float recommendation может быть 0;
- admin physically funds provider outside system;
- system verifies real provider balance where API supports it.

## Acceptance criteria

- low provider float не теряет accepted requests;
- queue survives restart;
- restoration within 15m resumes task;
- timeout releases partner reserve;
- provider float shortage не раскрывается партнёру как financial reason.

## Зависимости

EPIC 05, EPIC 07, EPIC 08.

---

# EPIC 12. Partner webhooks

## Цель

Доставлять terminal generation events надёжно и проверяемо.

## Scope

Events:
- completed
- failed
- timeout
- cancelled

### Snapshot semantics

При generation creation фиксируются:
- webhook URL;
- webhook secret.

Изменение/удаление API key позже in-flight event не меняет.

### Signing

HMAC-SHA256:
`timestamp + "." + raw_body`

Headers должны содержать:
- timestamp;
- signature;
- event_id;
- delivery_id;
- attempt.

### Delivery

- at least once;
- retry every 15 minutes;
- max 24h;
- delivery failure after 24h => terminal delivery failure + admin alert.

### Manual resend

- из Telegram cabinet;
- same event_id;
- new delivery_id;
- incremented attempt;
- fresh delivery timestamp;
- destination только original snapshot URL/secret.

## Acceptance criteria

- receiver может verify signature;
- manual resend не меняет generation/billing;
- webhook settings change не влияет на in-flight;
- duplicate event безопасно различим через event_id/delivery_id.

## Зависимости

EPIC 03, EPIC 07.

---

# EPIC 13. Partner Telegram cabinet

## Цель

Дать партнёру минимальный self-service кабинет без отдельного web UI.

## Scope

Главное меню:
- Баланс;
- Пополнить;
- API-ключи;
- История;
- Поиск по UUID;
- Документация;
- Support;
- Удалить аккаунт.

### History

Навсегда:
- date;
- model;
- params;
- duration;
- partner charge;
- status;
- result;
- errors/refunds/adjustments;
- prompt;
- refs.

Filters:
- UUID;
- date;
- model;
- status.

### Generation lookup

Показывает:
- current status;
- result/CDN links;
- billing metadata.

Не показывает:
- provider;
- provider task id;
- upstream attempts/cost.

### Free tests

- 2 lifetime video generations per Telegram ID;
- через bot;
- any enabled production video model;
- full supported params;
- partner charge 0.

## Acceptance criteria

- все действия доступны только owner TG ID account;
- partner не видит internal financial/provider data;
- free tests нельзя получить повторно через re-registration;
- manual webhook resend доступен из generation card.

## Зависимости

EPIC 02, EPIC 03, EPIC 07, EPIC 09, EPIC 12.

---

# EPIC 14. Admin Telegram control plane

## Цель

Сделать Telegram основной операционной админкой без отдельного web admin в v1.

## Scope

Разделы:
- applications;
- partners;
- provider keys;
- model catalog;
- partner pricing;
- margin thresholds;
- payments waiting credit;
- provider float;
- generations/search;
- incidents;
- safe-to-withdraw;
- profit withdrawals;
- support;
- health;
- test/prod switch.

### Critical actions

Confirmation button required for:
- approve/reject partner;
- price changes;
- model enable/disable;
- balance adjustment;
- coverage adjustment;
- credit payment;
- profit withdrawal record;
- refund finalization;
- partner disable/delete.

### Admin financial views

USDT:
- profit;
- provider cost;
- margin;
- working capital;
- provider float;
- safe-to-withdraw.

## Acceptance criteria

- one configured admin TG ID;
- unauthorized TG cannot access admin routes;
- critical actions are confirmed;
- admin can recover by changing admin_telegram_id in env/config;
- test/prod switch does not route partners to test.

## Зависимости

Большинство domain epics; skeleton можно начать после EPIC 01.

---

# EPIC 15. Support tickets и attachments

## Цель

Организовать поддержку полностью внутри Telegram.

## Scope

Ticket:
- open;
- in_progress;
- closed.

Content:
- text;
- screenshots;
- files;
- history.

Rules:
- attachment max 20 MB each;
- отдельного count limit нет;
- closed ticket не reopen;
- новая проблема => новый ticket;
- immediate admin/partner notifications;
- retention indefinite;
- attachments хранятся в RF MinIO/storage.

## Acceptance criteria

- файл >20 MB отклоняется понятной ошибкой;
- закрытый ticket нельзя изменить обратно в open;
- history immutable enough for support audit;
- attachments не уходят на foreign storage.

## Зависимости

EPIC 01, EPIC 02, EPIC 18.

---

# EPIC 16. Reporting и XLSX exports/imports

## Цель

Дать партнёру и админу сверяемую финансовую отчётность.

## Partner XLSX

Periods:
- 7d;
- 30d;
- all time.

Содержит:
- generations;
- expenses;
- top-ups;
- neutral balance adjustments;
- opening balance;
- additions;
- deductions;
- closing balance.

## Admin XLSX

Periods:
- 7d;
- 30d;
- all time.

Содержит:
- all partners;
- top-ups;
- deductions;
- adjustments;
- partner charge;
- actual provider cost USDT;
- margin USDT;
- margin %;
- financial result.

## Pricing import/export

- writable: partner prices;
- read-only: provider costs;
- preview diff;
- margin preview;
- invalid/negative-economics rows block apply.

## Acceptance criteria

- reconciliation формула сходится;
- historical rows используют immutable snapshots;
- partner export не раскрывает provider costs;
- import не может изменить upstream cost;
- bulk price apply атомарен.

## Зависимости

EPIC 04, EPIC 08, EPIC 09.

---

# EPIC 17. Alerts и incident lifecycle

## Цель

Сделать единый механизм operational/financial alerting без спама и потери инцидентов.

## Alert types

- low partner balance;
- provider float low;
- payment awaiting manual credit;
- provider/API changes;
- low margin;
- negative economics;
- provider incident loss;
- webhook delivery failed;
- negative safe-to-withdraw;
- provider key invalid;
- circuit breaker open/recovered;
- backup restore-test failed;
- DR drill failed.

## Rules

### Repeating 15m

- low partner balance;
- provider float low;
- paid waiting credit;
- negative safe-to-withdraw до manual silence;
- другие явно зафиксированные repeating incidents.

### One-shot per incident

- low-margin incident;
- negative-economics incident.

New incident only after recovery and re-entry.

## Acceptance criteria

- incident deduplication работает;
- no alert storm;
- manual silence действует только на текущий deficit incident;
- новый deficit после recovery снова уведомляет;
- state alert incidents сохраняется.

## Зависимости

EPIC 04, EPIC 08, EPIC 10, EPIC 11, EPIC 12.

---

# EPIC 18. Queueing, retries, circuit breaker и recovery

## Цель

Выдерживать provider degradation и deploys без потери accepted tasks.

## Scope

### Durable queue

- fair strict-ish round-robin across partners;
- preserve per-partner order where possible;
- no VIP;
- 20–50 parallel tasks baseline;
- 10× growth without core rewrite.

### Retry

Для 429/temp5xx:
- Retry-After;
- иначе exponential backoff + jitter.

### Timeouts

- provider acceptance max 15m;
- after accepted provider-specific documented timeout;
- default 15m if provider has no explicit value;
- admin override possible.

### Circuit breaker thresholds v1

- provider errors >10% / 5m with enough requests;
- timeout >5% / 10m;
- oldest queued >2m;
- incoming but no successful gen >5m;
- webhook errors >10% / 10m.

### Recovery

- 3 successful free health checks, 1 minute apart;
- then one real task at a time;
- 3 successful real tasks => normal;
- any provider-side fail => breaker again.

## Acceptance criteria

- deploy не теряет queued/accepted jobs;
- breaker автоматически исключает provider;
- recovery staged;
- no paid synthetic health tests;
- financial loss alone breaker не открывает.

## Зависимости

EPIC 05, EPIC 06, EPIC 07.

---

# EPIC 19. Result links, retention semantics и refresh capability

## Цель

Отдавать результаты без собственного media storage и не обещать неподтверждённый upstream retention.

## Scope

- сохраняем provider result URL;
- если provider сообщает expires_at — передаём;
- partner обязан скачать результат immediately и не позже договорного 24h window;
- 24h не является upstream technical guarantee;
- no refund если корректно выданный URL позже expired;
- не храним generated video/image files ourselves.

### Conditional refresh

На integration stage проверить:
- можно ли повторно получить/refresh completed result URL.

Если да:
- добавить Telegram action “Обновить ссылку”.

Если нет:
- action отсутствует.

## Acceptance criteria

- completed response содержит только доступный result metadata;
- система не делает ложного promise о 24h storage;
- refresh capability включается только после доказанного integration behavior.

## Зависимости

EPIC 05, EPIC 07.

---

# EPIC 20. Partner documentation surface

## Цель

Дать партнёру ровно ту документацию, которая нужна для подключения моделей, не публикуя полный внутренний API contract.

## Scope

Public docs domain:
- `docs.нейроныч.online`;
- public `/docs` содержит самостоятельный справочник inference API (§128 product brief);
- публичный full OpenAPI / Swagger / ReDoc отключён.

Public docs:
- RU + EN;
- production API base URL;
- partner authentication header;
- список/ID production-моделей;
- endpoint family для подключения каждой модели;
- параметры, типы, обязательность, defaults, лимиты и несовместимые сочетания;
- JSON/curl/Python примеры текста, изображений, видео с референсом;
- upload → create → status → download, SSE и клиентские правила идемпотентности;
- собственные форматы ответов/ошибок без раскрытия первого провайдера.

Не публикуются в открытой документации:
- billing/ledger/admin endpoints;
- provider internals;
- webhook/retry/reconciliation operational details;
- support/admin/financial APIs;
- полный OpenAPI schema.

Полная техническая документация при необходимости живёт только в авторизованном partner surface.

### ArgoLink compatibility

Цель:
- partner меняет base_url + API key;
- endpoint/model names/params/shapes максимально совместимы where practical.

## Acceptance criteria

- публичный `/docs` позволяет интегрировать native inference API без сторонней документации;
- параметры и примеры проверены относительно исполняемого контракта;
- справочные примеры отделены от актуального списка включённых моделей;
- `/openapi.json` и публичные Swagger/ReDoc недоступны;
- public docs не раскрывают billing/admin/provider operational contract;
- no enabled model without current model-connection docs;
- закрытая partner documentation остаётся доступной только после partner authentication, если она реализована.

## Зависимости

EPIC 03, EPIC 04, EPIC 07, EPIC 12.

---

# EPIC 21. Security, privacy и 152-ФЗ technical controls

## Цель

Минимизировать риск утечки ключей/ПД и удержать production data в РФ.

## Scope

- prod DB/Redis/app/backups in RF;
- Telegram proxy outside RF stateless;
- no business payload persistence on proxy;
- secrets via env/Docker secrets;
- no secrets in Git;
- encrypted backups;
- master recovery key outside servers;
- webhook SSRF protection;
- URL validation;
- outbound request allow/deny strategy;
- secret redaction in logs;
- sensitive prompt/reference logging policy;
- partner data access boundaries;
- admin-only raw provider diagnostics.

## Legal-review flags

Нужны отдельные юридические проверки:
- indefinite business/support retention vs PD minimization;
- non-refundable positive balance terms;
- cross-border transfer of prompts/references to upstream providers;
- wording of 24h result download obligation.

## Acceptance criteria

- secret scanner green;
- logs do not expose API keys/webhook secrets;
- provider keys encrypted at rest or equivalently protected;
- proxy has no persistent PD/business storage;
- SSRF tests cover private/link-local ranges.

## Зависимости

EPIC 01 onward; security requirements обязательны поперёк всех эпиков.

---

# EPIC 22. Observability, metrics и SLO

## Цель

Дать админу понимание текущего состояния системы и историю деградаций.

## Scope

Metrics:
- API latency;
- create generation latency;
- queue depth/age;
- provider success/error/timeout rates;
- webhook delivery rates;
- provider float;
- payment waiting count;
- margin;
- incident loss;
- safe-to-withdraw;
- active reservations.

Health views:
- current;
- 1h;
- 24h;
- 7d.

Retention:
- technical logs/metrics 90d.

SLO:
- balance/pricing/status p95 <=300ms, p99 <=1s;
- create generation p95 <=500ms, p99 <=1s excluding actual generation time.

## Acceptance criteria

- critical metrics available without DB manual inspection;
- alert thresholds consume the same metric definitions;
- correlation ID links API request, generation, provider attempt and webhook delivery.

## Зависимости

EPIC 01, then incremental integration with all domains.

---

# EPIC 23. Backup, restore и disaster recovery

## Цель

Обеспечить восстановление критичных данных и проверять это автоматически.

## Scope

PostgreSQL:
- continuous WAL archive;
- daily base/full backup;
- client-side encryption;
- MinIO on separate RF storage VPS;
- retention 14d.

Tests:
- weekly automatic restore test;
- failure => emergency Telegram alert;
- monthly full DR drill.

Runbook:
- Markdown outside Git/server;
- у владельца;
- locations/instructions only, no actual secrets.

Release gate:
- DR drill FAIL blocks next production release until fixed + successful rerun.

## Acceptance criteria

- restore-test реально поднимает DB и проверяет critical tables;
- backup without decryption key unusable;
- DR result stores date/duration/issues/steps/PASS/FAIL;
- RTO drill target <=15m.

## Зависимости

EPIC 01, EPIC 08.

---

# EPIC 24. CI/CD, preview test environment и production deploy

## Цель

Сделать безопасный pipeline от PR до production без ручной рутины.

## Scope

GitHub:
- private repo;
- no direct code pushes to main;
- PR required;
- green CI enables auto-merge;
- critical check failure blocks.

CI:
- unit;
- integration;
- smoke;
- secret scanning;
- dependency scan;
- static analysis;
- security tests.

PR test environment:
- same VPS;
- isolated DB/schema;
- isolated Redis;
- isolated keys/config;
- only admin can use test bot mode.

Deploy:
- GH Actions;
- private GHCR;
- immutable image SHA;
- SSH dedicated deploy user;
- pinned host key;
- blue/green-like switch;
- readiness check;
- graceful drain;
- automatic rollback.

Migrations:
- expand → deploy → contract.

## Acceptance criteria

- PR deploy cannot touch prod DB/Redis;
- failed health never becomes active prod;
- rollback tested;
- migration compatible with previous app version;
- deploy process documented and reproducible.

## Зависимости

EPIC 01, EPIC 23.

---

# EPIC 25. Production readiness, load testing и go-live

## Цель

Перед подключением реальных партнёров проверить, что система готова финансово, технически и операционно.

## Scope

### Required test scenarios

- onboarding / reject / reapply;
- consent version update;
- key create/revoke;
- duplicate idempotency;
- insufficient balance;
- negative economics;
- provider key failure;
- fallback;
- charged provider incident;
- queue restart;
- late success;
- top-up paid/manual credit;
- duplicate payment callback;
- refund full/partial;
- webhook retry/manual resend;
- safe-to-withdraw positive/negative;
- profit withdrawal/correction;
- backup restore;
- deploy rollback;
- circuit breaker/recovery.

### Load

Проверить:
- 6 initial partners;
- 20–50 parallel generation requests;
- synthetic 10× scheduling/queue load;
- API SLO under non-generation endpoints.

### Go-live gate

Critical FAIL blocks launch.

Первый rollout:
- production technically ready for all 6;
- partners onboard one by one.

## Acceptance criteria

- signed readiness report;
- all critical tests PASS;
- backup restore PASS;
- rollback PASS;
- payment/billing reconciliation PASS;
- no known critical security issue;
- first partner can complete end-to-end flow.

## Зависимости

Все предыдущие эпики.

---

# Рекомендуемый порядок реализации

## Wave 1 — фундамент

EPIC 01 → 02 → 03 → 05.

Результат: есть account/auth/provider skeleton и можно безопасно строить domain logic.

## Wave 2 — денежное и generation ядро

EPIC 04 → 08 → 06 → 07 → 11.

Результат: можно принять generation request, посчитать деньги, отправить provider и корректно завершить lifecycle.

## Wave 3 — платежи и партнёрский integration surface

EPIC 09 → 12 → 13 → 20.

Результат: партнёр может зарегистрироваться, пополниться, интегрировать API, получить webhooks и результаты.

## Wave 4 — операционка

EPIC 10 → 14 → 15 → 16 → 17 → 18 → 19 → 22.

Результат: владелец реально может управлять системой в production.

## Wave 5 — hardening

EPIC 21 → 23 → 24 → 25.

Результат: production readiness и controlled rollout.

---

# Что считать MVP blocker

До первого платного партнёра обязательно должны быть готовы:

- EPIC 01 — foundation;
- EPIC 02 — partner lifecycle;
- EPIC 03 — auth/versioning;
- EPIC 04 — catalog/pricing;
- EPIC 05 — ArgoLink adapter;
- EPIC 06 — routing/economics;
- EPIC 07 — generation state machine;
- EPIC 08 — financial ledger;
- EPIC 09 — payments;
- EPIC 10 — safe-to-withdraw;
- EPIC 11 — provider float;
- EPIC 12 — webhooks;
- EPIC 13 — partner cabinet;
- EPIC 14 — admin operations;
- EPIC 17 — critical alerts;
- EPIC 18 — queue/retries/circuit breaker;
- EPIC 19 — result handling;
- EPIC 20 — docs;
- EPIC 21 — security baseline;
- EPIC 23 — backup/restore;
- EPIC 24 — CI/CD;
- EPIC 25 — readiness.

EPIC 15/16/22 можно развивать параллельно, но минимальные support/reporting/observability функции всё равно должны войти до широкого rollout.

---

# Рекомендуемый формат задач внутри каждого эпика

Для каждой engineering task использовать одинаковый шаблон:

- **Problem**
- **Expected behavior**
- **Domain rules**
- **API/Telegram UX**
- **DB changes**
- **Failure modes**
- **Security considerations**
- **Metrics/logs**
- **Tests**
- **Migration/rollback**
- **Acceptance criteria**

Это позволит Codex/агентам брать отдельные задачи без необходимости перечитывать весь проектный чат.
