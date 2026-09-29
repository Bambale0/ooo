# Нейроныч SaaS — текущая спецификация продукта

> Статус: зафиксированный снимок интервью на 2026-09-19.  
> Это не финальная спецификация: интервью продолжается.  
> Принцип: ниже отдельно отмечены подтверждённые решения, эксплуатационные правила и открытые вопросы.

## 1. Продукт и бизнес-модель

Нейроныч SaaS — закрытый B2B-сервис для небольшого круга доверенных партнёров. Партнёры получают наш API и продают AI-генерацию своим конечным пользователям по своим ценам и в своей валюте. Наши отношения, биллинг, лимиты и финансы ведутся на уровне партнёра.

Стартовая группа — около 6 партнёров. Ожидаемая нагрузка по одному из ориентиров: примерно 5.9 тыс. видео-секунд в неделю на одного партнёра; для 6 партнёров — порядка 35.8 тыс. секунд в неделю. Архитектура должна иметь запас порядка 10× без переписывания ядра.

Основной upstream-провайдер на старте — ArgoLink. В будущем допускается несколько провайдеров с глобальным приоритетом по каждой модели и автоматическим fallback.

Референс по UX/архитектурным паттернам — репозиторий `stupidbot`, но новый SaaS строится как отдельный проект с чистой кодовой базой.

## 2. Стартовый стек

Подтверждённый стек:

- Python
- FastAPI
- aiogram
- PostgreSQL
- Redis
- SQLAlchemy 2
- Alembic
- Pydantic Settings
- httpx
- Docker + Docker Compose
- Nginx
- MinIO
- структурированные JSON-логи
- Prometheus-совместимые метрики

Критичные финансовые операции и durable jobs опираются на PostgreSQL. Redis не является единственным источником истины.

## 3. Инфраструктура и размещение

Production размещается в РФ:

- API
- Telegram backend
- PostgreSQL
- Redis
- workers
- application services
- бизнес-данные
- резервные копии с персональными данными

На старте — один production VPS в РФ. Архитектура сразу должна позволять подключить второй узел/реплику без переделки ядра.

Отдельный недорогой storage-VPS в РФ:

- MinIO
- зашифрованные резервные копии
- вложения тикетов поддержки

На старте одного отдельного storage-VPS достаточно.

Для Telegram используется отдельный stateless proxy/VPS вне РФ:

- входящий webhook Telegram → proxy → backend в РФ
- исходящие вызовы Bot API → proxy → Telegram
- никакой БД
- никаких бизнес-данных
- никаких payload-хранилищ
- минимальные технические access/error-логи без персональных данных

Партнёрский API через этот proxy не проходит.

### Домены

- `api.нейроныч.online` — production API напрямую в РФ
- `bot.нейроныч.online` — Telegram proxy/webhook-контур
- `docs.нейроныч.online` — документация, Swagger UI, публичный прайс и changelog

Machine-readable OpenAPI дополнительно доступен на `api.нейроныч.online/openapi.json`.

## 4. Надёжность, RTO/RPO и резервное копирование

Целевой RTO после полного падения — до 15 минут.

На одном VPS настоящий гарантированный RPO=0 не заявляется. До появления второго сервера требование формулируется как «максимально близко к нулю потерь».

Критичные данные:

- балансы
- платежи
- генерации
- API-ключи
- настройки цен
- webhook-конфигурация

Должны восстанавливаться полностью.

### Backup

- непрерывное PostgreSQL WAL-архивирование
- ежедневный base/full backup
- client-side encryption перед отправкой в MinIO
- окно хранения — 14 дней
- старые backup/WAL автоматически удаляются после истечения окна
- weekly restore-test
- при failed restore-test — аварийный alert админу
- раз в месяц полноценный disaster-recovery drill

Recovery key:

- рабочая копия доступна production
- аварийный recovery key хранится вне серверов
- основная аварийная копия — в менеджере паролей
- дополнительная offline-копия
- единственной незаменимой копии ключа на сервере быть не должно

### Disaster recovery

Отдельный автономный Markdown runbook:

- не хранится на production/storage VPS
- не хранится в GitHub
- хранится у владельца
- не содержит сами секреты, только инструкции, где их получить
- должен быть достаточен агенту для восстановления без истории чатов

Результаты DR-drill сохраняются во внутренней админской истории:

- дата
- фактическое время восстановления
- найденные ошибки
- выполненные шаги
- PASS/FAIL

Если DR-drill = FAIL, production-релизы блокируются до исправления и успешного повторного drill.

## 5. CI/CD и GitHub

GitHub-репозиторий должен быть приватным.

Правила:

- direct push в `main` запрещён
- любые изменения только через PR
- ручное approval для merge не требуется
- полностью зелёный CI разрешает auto-merge
- каждый PR автоматически выкатывается в изолированный test-контур на том же VPS
- test и prod полностью разделены по БД/схемам, Redis-очередям, ключам, балансам и конфигурации
- после зелёного test/smoke/integration PR auto-merges в `main`
- merge в `main` автоматически запускает production deployment

Обязательный CI:

- unit/integration/smoke tests
- secret scanning
- dependency vulnerability scan
- static analysis
- security checks
- production-readiness gates по необходимости

Deploy:

- GitHub-hosted Actions
- immutable Docker image по commit SHA
- private GHCR
- GitHub Actions → SSH на VPS под отдельным deploy-пользователем
- production secrets не передаются в GitHub
- Nginx/blue-green-like переключение
- новая версия поднимается рядом
- health-check
- переключение трафика
- старая версия корректно завершается
- при провале — automatic rollback

Миграции БД:

- `expand → deploy → contract`
- новая и предыдущая версия должны временно быть совместимы с одной схемой
- rollback не должен ломать данные

Go-live:

- агент обязан провести полный production-readiness audit
- инфраструктура
- backup/restore
- payments
- billing
- generations
- webhooks
- queues
- rollback
- security
- CI/CD
- 152-ФЗ-контур
- любой critical FAIL полностью блокирует запуск
- WARNING только уведомляет
- финальный go-live подтверждается совместно владельцем и агентом

## 6. Telegram: роли и интерфейс

Telegram-интерфейс на старте только на русском.

Один администратор, привязанный к одному Telegram ID.

Аварийная смена `admin_telegram_id` должна быть возможна через server config/env с перезапуском.

Один Telegram-бот с переключаемым admin-only test/prod режимом:

- админ может переключать test/prod
- партнёры всегда работают только с production

Критические admin actions подтверждаются кнопкой; отдельный PIN не нужен.

На старте аварийные уведомления только через Telegram.

## 7. Онбординг партнёра

Партнёр подаёт заявку через Telegram-бот.

Поля заявки:

- имя / название компании
- Telegram ID определяется автоматически
- название проекта/бота

Перед отправкой заявки партнёр обязан согласиться с:

- политикой обработки персональных данных
- условиями сервиса / офертой

Сохраняются:

- версия документа
- дата/время согласия
- Telegram ID
- факт согласия

Обновление документов:

- партнёр должен подтвердить новую версию
- API не останавливается
- кабинет не блокируется
- до принятия показывается заметное обязательное уведомление

### Одобрение

Процесс:

1. Партнёр отправляет заявку.
2. Админ идёт в ArgoLink.
3. Выпускает отдельный upstream API-ключ для этого партнёра.
4. Вставляет его в карточку заявки.
5. Backend валидирует upstream-ключ.
6. Пока ключ не прошёл проверку, кнопка «Одобрить» заблокирована.
7. Админ одобряет заявку.
8. Партнёр получает доступ в кабинет.

ArgoLink/upstream-ключ клиент никогда не видит.

При нескольких активных upstream-провайдерах нового партнёра нельзя одобрить, пока для него не заведены и не проверены отдельные ключи всех провайдеров production-routing.

При отказе заявки партнёр видит только «Заявка отклонена», без причины.

После одобрения production API доступен сразу; наличие нулевого баланса не мешает получить/создать ключ, но платные генерации невозможны.

### Первый вход

Короткий onboarding:

1. создать API-ключ
2. открыть документацию
3. при желании сделать бесплатные тесты
4. пополнить баланс
5. подключить API в свой бот

Первый наш API-ключ партнёр создаёт сам вручную в разделе «API-ключи».

## 8. Бесплатные тесты

Каждому Telegram ID выдаётся только один раз за всю историю:

- 2 бесплатные тестовые видеогенерации

Повторная регистрация не восстанавливает тесты.

Тесты:

- доступны через наш Telegram-бот
- можно выбрать любую видеомодель, включённую в production
- разрешены любые поддерживаемые параметры, включая разрешение и длительность
- партнёру стоят 0 ₽
- upstream-себестоимость оплачивается нами
- после тестов рабочие коммерческие генерации идут через API партнёра

## 9. Модели и доступность

Стартовый video catalog:

- Seedance 2
- Seedance 2.5
- Grok Imagine

Глобальное правило:

> Любая включённая модель должна поддерживаться полностью, со всеми режимами, параметрами, разрешениями, референсами и возможностями upstream-провайдера. Урезанных интеграций не делаем.

Для партнёров все равны:

- модель либо выключена для всех
- либо включена для всех
- индивидуальных model-access списков по партнёрам нет

Админ — техническое исключение:

- админ может использовать выключенные модели для тестов
- выключенные модели полностью скрыты от партнёров

Включение модели = production release.

До включения обязательно:

- полная реализация интеграции
- partner price
- документация
- успешный автоматический smoke-test

После включения атомарно становятся доступны:

- API
- документация
- публичный прайс

LLM технически поддерживаются в первой версии; пока партнёрам могут быть выключены, но админ может пользоваться ими для внутренних тестов. При включении модель становится доступна всем партнёрам.

Image billing/type поддерживается архитектурно; image generation изначально используется админом/внутренне, если модель не включена для всех.

## 10. Provider routing

Архитектура provider-agnostic с первого дня.

Для каждой модели задаётся глобальный порядок:

`Provider A → Provider B → Provider C`

Например:

`ArgoLink → Provider B → Provider C`

Партнёр не знает, какой upstream обработал задачу.

### Upstream-ключи

Для каждого партнёра у каждого активного провайдера должен быть отдельный upstream API-ключ.

Многие наши API-ключи партнёра:

`our key 1 / our key 2 / ... → one partner → one upstream key per provider`

Если ключ одного провайдера сломан:

- этот provider исключается из routing только для данного партнёра
- партнёр целиком не блокируется
- остальные провайдеры продолжают работу
- админу приходит alert

Замена upstream-ключа:

- выполняется в карточке партнёра
- новый ключ сначала валидируется
- затем новые задачи атомарно переключаются
- старый рабочий ключ не заменяется невалидным
- уже запущенные задачи отслеживаются по старым `provider_task_id`

### Provider fallback

Fallback допускается на более дорогого провайдера, если:

- partner price покрывает себестоимость
- задача не требует финансирования из денег владельца

Партнёр платит ту же зафиксированную partner price независимо от внутреннего provider routing.

Если задача уже принята upstream, duplicate launch в другом provider без основания не делается.

Если provider завершил accepted task как `failed` и не списал деньги:

- запрос автоматически повторяется через следующий fallback
- наш UUID сохраняется
- upstream attempt меняется

Если provider списал деньги, но результата не дал:

- fallback всё равно запускается за наш счёт
- это исключение из правила «не кредитовать партнёра»
- партнёр всё равно платит максимум одну обычную partner price одной успешной генерации

Если все fallback закончились без результата:

- partner charge = 0 ₽
- весь partner charge reserve возвращается
- понесённые provider-costs записываются как `provider_incident_loss`

## 11. Circuit breaker и recovery

### Уточнение 2026-09-29: повтор подтверждённого внутреннего сбоя

Для текущего видеопровайдера разрешены максимум два дополнительных запуска
внутри одной нашей заявки, через 5 и 15 секунд после отказа. Условие: сырой
статус `failed`, ошибка `internal_error`, `retryable: true`, нет результата
и данных usage. Другие отказы и неизвестный исход POST не запускают новую задачу.
`WORKER_GENERATION_MAX_RETRIES=0` отключает это поведение (допустимо 0–2).

Сохраняются наш UUID, исходный запрос, цены, один резерв партнёра и исходный
активный upstream-ключ. Общий deadline 30 минут не продлевается. Отзыв исходного
ключа отменяет ожидающий повтор; новый ключ другого аккаунта не подставляется.
До последней попытки terminal webhook и возврат резерва не отправляются.
Перезапуск воркера сохраняет счётчик, историю task IDs и защиту submit intent.
Уже закрытые и возвращённые заявки автоматически не открываются заново.

Пустой usage не доказывает отсутствие списания у провайдера. Стоимость прежних
отказов хранится как `unknown` в приватной истории для сверки, отдельно от
стоимости успешной попытки. Подтверждённые расходы отказов относятся к
`provider_incident_loss`, партнёру не выставляются. Автоматическая сверка этой
неизвестной стоимости пока не реализована.

При массовой деградации provider автоматически исключается из routing.

Alert админу:

- при отключении
- при восстановлении

После circuit breaker:

- бесплатный health/status endpoint используется, если существует
- платные test generations для health-check не запускаются
- health-check раз в 1 минуту
- требуется 3 успешных бесплатных проверки подряд

Если отдельного бесплатного health endpoint нет:

- используем бесплатные доступные API-признаки
- затем provider входит в recovery mode

Recovery mode:

- реальные задачи пропускаются по одной
- требуется 3 успешных реальных генерации подряд
- только затем provider возвращается в обычный routing
- любая provider-side ошибка снова сразу открывает circuit breaker
- после недавнего recovery повторная деградация отключает provider по ускоренному сценарию

## 12. Provider incident loss

Во внутренней админке:

- сумма `provider_incident_loss`
- история
- разбивка по провайдерам

Alert за последние 24 часа, если одновременно:

- incident loss > 3% себестоимости данного провайдера
- incident loss > 1 000 ₽

Если incident loss > 10%:

- provider автоматически не выключается только из-за финансового показателя
- админу отправляется отдельный критический финансовый alert

Все потери учитываются в статистике даже ниже alert threshold.

## 13. API

Партнёрский API должен быть максимально совместим с ArgoLink:

- те же endpoint-паттерны насколько возможно
- те же названия моделей
- те же параметры
- максимально похожая структура request/response
- идеальный сценарий интеграции: партнёр меняет только `base_url` и API key

Авторизация:

`Authorization: Bearer <API_KEY>`

Все API/webhook поля:

- английский
- `snake_case`

Все human-readable API error messages:

- английский

Stable `error_code` является основным машинным контрактом.

### Версионирование

- `/v1`
- далее при необходимости `/v2`
- обратную совместимость держим
- breaking change не выкатывается поверх действующей major version
- старая major version поддерживается 90 дней
- countdown показывается:
  - в Telegram-кабинете
  - в API-документации
- за 7 дней до отключения отправляется Telegram reminder
- админ видит, какие партнёры и какие их API keys всё ещё используют старую версию
- после 90 дней старая версия отвечает:
  - HTTP `410 Gone`
  - `error_code = api_version_expired`
  - ссылка на документацию новой версии

### Swagger/OpenAPI

- публичный Swagger/OpenAPI без авторизации
- RU/EN документация с переключателем языка на одной странице
- public Swagger UI на `docs.нейроныч.online`
- raw OpenAPI на `api.нейроныч.online/openapi.json`

Выключенные модели не должны раскрывать рабочую partner-facing документацию.

### `/pricing`

- только по API key
- возвращает только partner prices и billing units
- не раскрывает upstream cost/margin
- не используется как full model capability schema
- technical capabilities живут в документации

### `/balance`

- только по API key
- возвращает доступный баланс партнёра
- все API keys одного партнёра видят один общий баланс
- без отдельного reserve breakdown

## 14. API keys партнёра

Партнёр может создавать любое количество ключей.

Для каждого ключа:

- label
- собственный webhook URL
- собственный webhook secret
- ключ бессрочный до удаления
- доступ с любых IP
- IP allowlist нет
- индивидуального spending limit нет
- все ключи расходуют общий баланс партнёра

При создании:

- полный secret показывается только один раз
- есть quick-copy
- после этого показывается masked key

Удаление ключа:

- немедленно блокирует новые запросы
- уже запущенные генерации не отменяет
- уже запущенная генерация использует snapshot webhook URL/secret, сохранённый в момент старта

## 15. Admin API key

Админский API key:

- один активный ключ одновременно
- rotation создаёт новый
- новый немедленно инвалидирует старый
- без grace period
- admin usage рассчитывается по upstream себестоимости, без markup over provider cost

## 16. Idempotency

Создание generation обязательно требует `idempotency_key`.

Если generation создана:

- ключ хранится бессрочно вместе с историей
- повтор с тем же key и тем же partner возвращает существующий UUID
- duplicate generation/charge невозможен

Если запрос отклонён до создания generation по preflight/economics:

- UUID не создаётся
- reserve не создаётся
- `idempotency_key` считается неиспользованным
- партнёр может повторить тот же key позже

## 17. Generation lifecycle

Статусы:

- `queued`
- `sent_to_provider`
- `processing`
- terminal:
  - `completed`
  - `failed`
  - `timeout`
  - `cancelled`

Полная partner history хранится бессрочно.

Храним:

- UUID
- idempotency key
- prompt
- full request params
- reference metadata/links
- provider task id
- provider attempts
- result/CDN links
- billing snapshot
- errors/refunds
- timestamps

Сами видеофайлы не храним. Result/CDN provider links можно отдавать напрямую.

По UUID:

- API status lookup
- Telegram lost-generation lookup

Отдельный API endpoint «список активных генераций» не требуется.

## 18. Webhooks generation events

Generation webhooks:

- `completed`
- `failed`
- `timeout`
- `cancelled`

Delivery model:

- at-least-once
- уникальный `event_id`
- при retry `event_id` не меняется
- увеличивается только `attempt`

Retry:

- каждые 15 минут
- максимум 24 часа
- после 24 часов — delivery failed + alert админу

Webhook snapshot:

- URL и secret фиксируются при старте generation
- последующая смена URL/secret не влияет на уже запущенную generation
- даже если API key позже удалён, финальный webhook идёт по сохранённым настройкам

Optional signing:

- если secret задан — webhook подписывается
- если secret пуст — unsigned

`completed` payload должен содержать как минимум:

- UUID
- status
- model
- params
- result/CDN links
- charged amount

Баланс в webhook не отправляется; партнёр делает `/balance`.

`failed`:

- stable `error_code`
- English human-readable message
- raw provider error наружу не отдаётся
- raw provider error хранится внутри

System/account events на generation webhook не идут.

## 19. Системные уведомления партнёра

Account-level события только в Telegram:

- low balance
- emergency messages
- API changes
- migration reminders
- прочие системные события

Не отправляются на API-key webhooks.

## 20. Очередь и fairness

Никаких собственных rate limits на API key/partner.

Ограничителями являются:

- баланс
- coverage/economics
- fair queue
- provider rate limits

Fairness:

- round-robin между партнёрами
- один партнёр не может монополизировать очередь
- внутри одного партнёра порядок сохраняется насколько возможно

Искусственного concurrent generation cap нет:

- 20–50 параллельных задач допустимы
- если хватает баланса
- если хватает покрытия себестоимости
- если upstream принимает нагрузку

Provider 429/temporary 5xx:

- задача остаётся durable queued
- honour `Retry-After`
- иначе exponential backoff + jitter
- broad outage → circuit breaker
- fallback возможен до provider acceptance

Максимальное ожидание provider acceptance — 15 минут.

Если за 15 минут ни один provider не принял:

- `timeout`/terminal failure
- reserve released
- normalized webhook
- для повторной самостоятельной попытки партнёр использует новый idempotency key

## 21. Deployment while requests arrive

При обновлении production:

- новые requests не должны получать 503 только из-за deploy
- request фиксируется durable
- UUID/idempotency/reserve переживают restart
- processing продолжается после deploy
- in-flight tracking не теряется

## 22. Cancellation

Partner/API cancel:

- если upstream поддерживает cancel и подтверждает до charge:
  - reserve полностью возвращается
  - status `cancelled`
  - charge 0 ₽
- если provider не поддерживает cancel после старта:
  - stable error `cancellation_not_supported`
  - fake local cancellation запрещена

При admin-disable или account deletion:

- система пытается отменить active generations
- если provider уже не позволяет cancel:
  - generation доводится до конца
  - charge по обычным правилам

## 23. Partner account disable/delete

Admin disable:

- требует confirmation
- все API keys блокируются сразу
- история и баланс сохраняются
- при re-enable старые ключи снова работают

При disable:

- active tasks пытаемся cancel
- uncancellable tasks завершаются и тарифицируются

### Delete account

Партнёр имеет кнопку «Удалить аккаунт».

Перед финальным удалением:

- если баланс < 0 ₽ — deletion запрещён до погашения долга
- если balance > 0 ₽ — остаток невозвратный и не возвращается
- правило должно быть явно отражено в оферте/условиях

После подтверждения:

- удаление сразу, без grace period
- API keys блокируются
- access блокируется
- active generations пытаемся cancel
- uncancellable завершаются и тарифицируются
- персональные данные удаляются/анонимизируются там, где это допустимо
- финансовая/generation/обязательная история сохраняется по правилам хранения

Повторная регистрация с тем же Telegram ID разрешена:

- новая заявка
- новое ручное одобрение
- новый partner account
- balance = 0 ₽
- новые API keys
- старый account остаётся архивом
- бесплатные тесты повторно не выдаются

## 24. Перенос Telegram ID

Один partner account привязан к одному Telegram ID.

Несколько сотрудников в одном кабинете в v1 не поддерживаются.

Если партнёр потерял Telegram:

- перенос только вручную админом после проверки
- старый Telegram ID сразу теряет доступ
- partner account остаётся тем же
- balance, API keys, history, webhooks не меняются

## 25. Billing currency и точность

Партнёр-facing currency — RUB.

Слово «credits» в обычном UI не используется.

Баланс и customer charges — до копейки, `0.01 ₽`.

Внутренние вычисления:

- высокая точность NUMERIC
- промежуточная себестоимость/курс/маржа не округляются раньше времени
- финальный customer charge округляется до 2 знаков
- правило `ROUND_HALF_UP`

Это заменяет прежнее решение про целые рубли.

## 26. Pricing

partner price всегда задаётся админом вручную.

Provider procurement price может синхронизироваться автоматически, если provider это технически позволяет.

Partner никогда не меняется автоматически из-за procurement changes.

Video pricing:

- per model
- per mode
- per resolution
- RUB/sec
- duration × rate

Reference-video billing:

- billable seconds = output duration + суммарная длительность reference videos
- customer billing следует той же логике с нашим partner rate

Images:

- per generation
- resolution-specific

LLM:

- per 1M input tokens
- per 1M cached input
- per 1M output tokens
- optional cache-write pricing

Price snapshot:

- фиксируется на старте generation
- изменение partner price не пересчитывает already-running tasks

Model disable:

- new requests блокируются сразу
- in-flight tasks завершаются

Margin alert:

- если margin < 30% → alert админу
- пока margin положительная, автоматического блокирования только по порогу 30% нет

Если procurement делает generation отрицательно-маржинальной или недостаточно customer cost coverage:

- preflight reject
- UUID не создаётся
- reserve не создаётся
- `idempotency_key` не тратится
- наружу `503 provider_temporarily_unavailable`
- закупочные цены/margin не раскрываются

## 27. Два финансовых счётчика партнёра

Для каждого partner account есть:

### partner RUB balance

То, что видит партнёр.

Пример:

партнёр пополнил 10 000 ₽ → partner RUB balance = 10 000 ₽.

### Cost coverage

Внутренний невидимый счётчик реального клиентского капитала, которым разрешено покрывать upstream-cost.

То же пополнение:

- partner RUB balance +10 000 ₽
- cost coverage +10 000 ₽

После generation:

- partner charge = 1 000 ₽
- provider cost = 560 ₽

Получаем:

- partner RUB balance = 9 000 ₽
- cost coverage = 9 440 ₽
- earned gross margin = 440 ₽

Это не удвоение денег, а две бухгалтерские проекции одной суммы.

### Manual balance adjustment

Admin может:

- увеличить partner RUB balance
- уменьшить partner RUB balance
- увести balance ниже нуля

Manual partner bonus/compensation по умолчанию:

- меняет только partner RUB balance
- не увеличивает cost coverage
- generation не должна тратить деньги владельца из-за виртуального бонуса

Отдельная admin operation:

- increase cost coverage
- decrease cost coverage
- используется при реальном поступлении вне Crypto Bot или осознанном внесении своего капитала
- обязательная внутренняя причина

Если late provider success после release reserve создал реальный provider cost:

- cost coverage может уйти ниже нуля как аварийный долг
- новые generations блокируются до восстановления

## 28. Upstream quota

Для каждого partner/provider upstream key quota синхронизируется автоматически.

Quota не равна partner RUB balance 1:1.

Она должна быть ограничителем допустимого реального provider-spend на основе:

- cost coverage
- active provider-cost reserves
- provider economics
- risk buffer

Цель: один партнёр не может потратить upstream-капитал, предназначенный другим, и система не должна незаметно финансировать его generation из денег владельца.

## 29. ArgoLink working float

Клиентская крипта в основном остаётся в кошельке владельца.

На ArgoLink отправляется только необходимый procurement working capital.

Target float ориентировочно учитывает:

- минимум $20
- себестоимость активных generations
- прогноз ближайшего часа
- +20% safety buffer

Если history недостаточно:

- минимум + active costs

Если фактический Argo float ниже target:

- immediate alert
- repeat каждые 15 минут до восстановления

Низкий float сам по себе не блокирует задачу, если фактических средств хватает.

Если upstream funds закончились:

- generation durable waits
- partner reserve сохраняется
- admin alert
- при восстановлении до 15 минут задачи автоматически продолжаются
- после 15 минут без acceptance → timeout, reserve release

## 30. Manual payment/top-up flow

Платёжный контур — один Crypto Bot/Crypto Pay shop.

Partner вводит желаемую сумму в RUB:

- любая целая сумма от 1 000 ₽
- верхнего лимита нет

Система пересчитывает:

- USDT amount
- либо GRAM amount
- по актуальному Crypto Bot rate

До оплаты показывает preview.

Invoice TTL:
- 1 час

Если unpaid/expired:

- закрывается
- partner может создать новый

Если expired invoice всё же фактически оплачен:

- платёж не теряется
- помечается как «invoice expired»
- проходит обычный ручной admin flow

### Главное правило

Crypto Bot никогда сам не зачисляет partner balance.

Flow:

1. Crypto Bot подтверждает, что crypto пришла.
2. Система фиксирует payment.
3. Фиксируются:
   - crypto amount
   - asset
   - payment-time rate
   - requested RUB amount
   - payment id
4. Partner видит:
   - «Оплачено, ожидает зачисления администратором»
5. Admin получает alert.
6. Admin идёт в ArgoLink и пополняет рабочий provider float, если требуется.
7. Admin возвращается в Telegram.
8. Backend проверяет реальный ArgoLink balance.
9. Admin нажимает «Зачислить баланс».
10. partner RUB balance и cost coverage увеличиваются.
11. Payment status → credited.

Повторное confirmation не может double-credit.

### Recommendation in payment card

В paid top-up card система должна показывать:

- partner
- Telegram ID
- crypto amount
- currency
- payment id
- requested RUB
- current Argo float
- calculated target float
- recommended amount to deposit into ArgoLink

Если recommended deposit = $0:

- admin может сразу credit partner

Кнопка «Зачислить баланс» должна учитывать проверку, что достаточный working float реально присутствует.

### FX difference

Partner получает ровно запрошенную RUB сумму.

Небольшой курс drift не перекладывается на partner.

- до 1% — допустимая внутренняя погрешность
- >1% — показывается заметный warning админу
- admin всё равно может manually credit исходную requested RUB amount
- расхождение пишется во внутреннюю financial history как FX difference

## 31. Crypto Bot FX

Один официальный Crypto Bot rate используется:

- для top-up conversion
- для USD→RUB procurement cost conversion

Background refresh:

- каждые 5 минут
- последнее successful rate кешируется

Если fresh rate недоступен:

- система продолжает работать по последнему successful rate

Если rate старше 1 часа:

- продолжаем использовать его
- immediate warning админу

## 32. Cost/margin snapshot

Фактическая provider себестоимость фиксируется после завершения generation.

USD→RUB rate для cost/margin:

- берётся в момент успешного завершения
- если Crypto Bot недоступен — последний известный rate
- сохраняется immutable snapshot

Исторические margin reports не пересчитываются задним числом.

## 33. Safe-to-withdraw

Админка обязана показывать:

> «Безопасно вывести сейчас: X ₽»

Расчёт учитывает:

- все partner cost coverage
- active reserves
- current upstream float
- required upstream target float
- internal obligations

Не учитывает:

- Crypto Bot withdrawal fees
- blockchain fees
- внешние комиссии вывода

Admin action:

- «Зафиксировать вывод прибыли»
- сумма и дата сохраняются во внутренней истории
- выведенная сумма больше не считается working capital

Если admin пытается вывести больше safe amount:

- операция разрешена
- перед этим серьёзный warning
- показывается превышение
- показывается потенциальный deficit
- требуется обязательный текстовый comment/reason override

## 34. Баланс и refund policy

Partner top-up balance:

- невозвратный
- расходуется только на услуги
- действует бессрочно
- не сгорает от inactivity

Это должно быть явно отражено в offer/terms.

Отдельное согласие перед каждым top-up не требуется — достаточно согласия с актуальной офертой при регистрации/обновлении.

## 35. Partner low balance

Default low balance threshold:

- 2 000 ₽

Настраивается отдельно на каждого партнёра.

При падении ниже threshold:

- Telegram alert partner
- Telegram alert admin
- повтор каждые 15 минут до top-up

## 36. Financial admin adjustments

Уточнение от 29.09.2026: в Telegram-форме коррекции баланса администратор выбирает
партнёра из списка с пагинацией либо ищет по Telegram ID / `@username` без учёта
регистра. UUID остаётся внутренним идентификатором получателя; прежний ручной ввод
UUID совместим. Перед суммой и подтверждением показываются компания, проект,
Telegram ID, сохранённый username и баланс. Username обновляется из личных обращений
к боту, удаляется из локального профиля при его отсутствии в новом обращении и
не является доказательством личности. При неоднозначном совпадении нужен выбор
партнёра; без сохранённого username доступны ID и список. Поиск и список доступны
только настроенному администратору. Начисление по-прежнему требует суммы, причины
и отдельного идемпотентного подтверждения.

Admin может вручную:

- увеличить partner RUB balance
- уменьшить partner RUB balance
- увести partner balance в минус

Внутри:

- amount
- type
- time
- reason

Partner в Telegram:

- не видит отдельную запись manual adjustment
- видит только итоговый balance

Partner XLSX:

- manual adjustment показывается нейтральной строкой «Корректировка баланса»
- без внутренней причины
- нужна для математической сверки

## 37. XLSX exports

Partner:

- 7 дней
- 30 дней
- всё время

Как минимум:

- generations/expenses
- top-ups
- summary:
  - starting balance
  - top-ups
  - charges
  - adjustments
  - ending balance

Admin export:

- 7 дней
- 30 дней
- всё время
- вся система
- все партнёры
- top-ups
- charges
- adjustments
- financial totals
- provider cost
- margin RUB
- margin %

## 38. Admin dashboard и health

Main financial dashboard:

- default period 7 days
- turnover/revenue
- upstream expense
- margin
- turnover comparison with previous week
- margin trend
- provider incident losses
- safe-to-withdraw

Health screen:

- current state
- 1 hour
- 24 hours
- 7 days

Metrics:

- queue
- active generations
- success/error rate
- model/provider latency
- ArgoLink status
- current ArgoLink balance
- provider health
- webhook delivery

Initial hardcoded alert thresholds:

- provider error rate > 10% за 5 минут при достаточном traffic
- timeout rate > 5% за 10 минут
- oldest queued task > 2 minutes
- incoming traffic есть, но no successful generation > 5 minutes
- webhook failures > 10% за 10 минут
- low upstream float — по dynamic target
- alert dedup/cooldown, без бессмысленного спама

Technical logs/metrics retention:

- 90 дней

Business/financial/generation/ticket history:

- бессрочно

## 39. API SLO

Обычные operations:

- `/balance`, `/pricing`, status:
  - p95 ≤ 300 ms
  - p99 ≤ 1 sec

Generation creation:

- p95 ≤ 500 ms
- p99 ≤ 1 sec

Generation execution time в API latency SLO не входит.

## 40. Support

В Telegram cabinet есть кнопка «Поддержка».

Support — встроенная ticket system.

Ticket statuses:

- `open`
- `in_progress`
- `closed`

Поддерживаются:

- text
- screenshots
- files

Admin:

- получает immediate notification о новом ticket
- immediate notification о новом message
- отвечает из admin bot

Partner:

- получает immediate notification об admin reply
- получает notification о status change

Closed ticket:

- не переоткрывается
- новый вопрос → новый ticket

Ticket history, conversation и attachments:

- хранятся бессрочно
- находятся в российском production/storage contour
- попадают в backup

## 41. Документация и публичные страницы

Telegram UI:

- только русский

API docs:

- русский + английский
- переключатель языка на одной странице

Public price:

- только русский
- публичный, без авторизации
- показывает только production-enabled models
- показывает partner-facing RUB only
- procurement/margin скрыты

Public changelog:

- только русский
- публичный
- только изменения нашего API и моделей
- provider incidents туда не попадают

Отдельной public status page нет.

## 42. API change notifications

При обнаружении upstream API/model change:

- система alert'ит админа
- auto-enable запрещён
- admin manually approves updated contract
- если upstream уже сломал old config:
  - affected config fail-closed
  - normalized error
  - никакого guessing
  - in-flight jobs продолжаются

Partner changelog notification:

- админ вручную выбирает, кому/всем отправлять Telegram update
- public changelog обновляется по принятому release flow

## 43. Pricing/model registry dev rule

Добавление новой модели/конфигурации кодовым агентом обязано автоматически регистрировать её в:

- unified model registry
- pricing catalog
- XLSX export/import

Новая модель/config:

- disabled for partners by default
- доступна admin
- не может быть production-enabled до:
  - partner price
  - docs
  - integration completion
  - smoke test

## 44. Public/partner equality

Партнёры равны:

- no VIP priority
- no model allowlist per partner
- no individual rate limit
- no special routing priority

Индивидуально могут различаться только:

- balance
- low-balance threshold
- API keys
- webhook URLs/secrets
- provider key validity
- история
- cost coverage
- manual admin corrections

## 45. 152-ФЗ — зафиксированные технические требования

Обязательное требование проекта — соблюдение 152-ФЗ.

Зафиксированная техническая схема:

- production и business data в РФ
- backup с personal data в РФ
- Telegram transport через stateless proxy вне РФ
- partner API напрямую в РФ
- consent на processing policy/terms
- versioned consent records
- deletion/anonymization mechanism

Юридические формулировки, основания и сроки хранения персональных данных должны пройти отдельную правовую проверку; техническая реализация не подменяет юридическую оценку.

## 46. Offer / terms — что обязательно отразить

Нужно явно отразить:

- пополненный balance невозвратный
- balance расходуется только на услуги
- balance не сгорает
- при удалении положительный остаток не возвращается
- при отрицательном balance удаление невозможно до погашения
- processing of personal data
- изменение policy/terms и повторное согласие
- правила доступа/деактивации
- сервисные ограничения и provider dependency

Точная юридическая формулировка — отдельная проверка.

## 47. Open questions / ещё не закрыто интервью

На момент этого snapshot остаются темы, которые стоит продолжить разбирать по одной:

- точная модель provider-cost coverage/quota API при реальных возможностях ArgoLink
- техническая проверка ArgoLink:
  - current balance API
  - key quota API
  - quota semantics
  - price catalog/source
  - task callbacks/status
  - cancellation
  - actual final cost
- точный Crypto Bot asset code для GRAM
- webhook signature algorithm
- exact API error catalog
- exact provider timeout catalog по моделям
- exact provider-cost reserve formulas
- exact safe-to-withdraw formula
- monitoring stack implementation details
- S3/MinIO lifecycle and attachment size limits
- support attachment limits
- API request/reference upload size limits
- database schema
- component boundaries
- queue implementation details
- backup tooling
- second-server replication design
- full acceptance test matrix
- production readiness checklist implementation
- legal review of offer / 152-ФЗ / personal-data retention

## 48. Current decision: money precision

Последнее зафиксированное решение:

- partner balance и списания — с точностью до 0.01 ₽
- internal calculations — более высокая точность
- customer final charge — ROUND_HALF_UP до копейки

## 49. Webhook signature

Подтверждено:

- partner generation webhooks подписываются привычной схемой HMAC-SHA256
- canonical payload для подписи: `timestamp + "." + raw_body`
- у каждого API-ключа свой отдельный webhook secret
- в headers передаются как минимум:
  - timestamp
  - signature
  - event_id
- схема должна быть простой и знакомой интеграторам
- receiver может отбрасывать слишком старые/replayed requests по timestamp


## 50. Manual webhook resend

Подтверждено:

- партнёр может вручную переотправить webhook по конкретной генерации из Telegram-кабинета
- повторная отправка использует тот же сохранённый webhook snapshot этой generation:
  - webhook URL
  - webhook secret
  - event payload
- manual resend не создаёт новую generation и не влияет на billing
- доставка остаётся at-least-once


## 51. Manual resend event identity

Подтверждено:

- manual webhook resend должен вести себя как обычная повторная доставка того же business event
- `event_id` сохраняется тем же, что у исходного события
- для каждой отдельной HTTP-доставки система создаёт отдельный `delivery_id` и увеличивает `attempt`
- это позволяет партнёру безопасно делать idempotent processing по `event_id`, при этом видеть и различать отдельные попытки доставки
- manual resend использует актуальный delivery timestamp, но тот же event payload и тот же business event identity


## 52. Manual resend destination

Подтверждено:

- manual webhook resend всегда отправляется только на webhook URL, сохранённый в generation snapshot
- текущий webhook URL API-ключа не используется для уже созданной generation
- partner не может подменить destination URL в момент manual resend
- для manual resend также используется сохранённый webhook secret из generation snapshot


## 53. Result URL retention

Подтверждено:

- готовые video/image файлы сервис у себя не хранит
- партнёру возвращается provider CDN/result URL
- если provider сообщает срок жизни ссылки, наружу передаётся `expires_at`
- партнёр сам обязан скачать результат до истечения ссылки
- бессрочная generation history хранит metadata, billing state, status и сам URL, но не гарантирует бессрочную доступность файла по этому URL
- истечение provider CDN URL не является refund-событием, если generation успешно завершилась и ссылка была выдана


## 54. ArgoLink result retention — docs check

Проверено по актуальной публичной документации ArgoLink:

- video generation возвращает `request_id`, затем status через `GET /v1/videos/{request_id}`
- при `done` ответ содержит `video.url`, ведущий на защищённый endpoint `GET /v1/videos/{request_id}/content`
- content endpoint требует Bearer key того же пользователя
- в публичной документации не найдено обещание конкретного срока хранения готового MP4 / TTL result URL
- упомянутые 24 часа относятся к `generation_timeout` для незавершённой генерации, а не к хранению готового результата
- статус `expired` в lifecycle описан как случай, когда результат не был произведён и списания нет; это не описание TTL готового CDN/result URL

Следствие для нашего продукта:

- нельзя обещать партнёру гарантированную доступность готового файла 24 часа, пока upstream явно не гарантирует такой retention или пока мы сами не храним файл
- текущая архитектура по-прежнему не хранит video/image result files
- в нашей документации/оферте нужно формулировать обязанность партнёра скачать результат как можно скорее; точное правило 24h нужно трактовать как contractual download window/maximum expectation, а не как техническую гарантию upstream retention, если ArgoLink не подтвердит срок отдельно


## 55. Partner responsibility to download result

Подтверждено:

- после успешной generation партнёр получает result URL и сам обязан скачать готовый файл
- сервис не гарантирует бессрочное хранение result file у upstream-провайдера
- партнёр должен забирать результат сразу после получения ссылки
- contractual download window в документации/оферте: не позднее 24 часов с момента выдачи result URL
- 24 часа не трактуются как техническая гарантия upstream retention, если provider отдельно её не заявляет
- истечение или недоступность upstream result URL после успешной выдачи результата не является основанием для refund, если сервис корректно выдал ссылку


## 56. Result URL refresh — implementation experiment

Подтверждено как условное требование:

- во время разработки нужно отдельно протестировать, можно ли после успешной generation повторно получить рабочий result URL / content access у ArgoLink после истечения первоначальной ссылки или спустя время
- если provider технически позволяет восстановить/обновить доступ к результату, в Telegram-кабинете можно добавить действие «Обновить ссылку»
- если provider этого не позволяет, такую функцию не обещаем и не показываем
- функция является best-effort convenience feature, а не заменой обязанности партнёра скачать результат сразу после получения
- точное поведение должно быть подтверждено integration test против реального provider API, а не предположением из документации


## 57. Support attachment size

Подтверждено:

- максимальный размер одного вложения в support ticket — 20 МБ
- ограничение применяется к скриншотам и обычным файлам
- превышение лимита должно отклоняться до сохранения файла в storage


## 58. Support attachment count

Подтверждено:

- отдельного лимита на количество вложений в одном сообщении support ticket нет
- действует только лимит 20 МБ на каждое отдельное вложение
- системные ограничения Telegram/API/storage могут технически ограничить фактическую отправку, но продуктовый лимит по count отдельно не вводится


## 59. Duplicate pending application

Подтверждено:

- если Telegram ID уже имеет заявку в статусе `pending`, новую заявку создать нельзя
- вместо повторной формы бот показывает текущий статус существующей заявки
- отдельная duplicate application record не создаётся


## 60. Re-application after rejection

Подтверждено:

- если заявка была отклонена, партнёр может подать новую заявку сразу
- cooldown после rejection не применяется
- новая заявка создаётся как отдельная application record


## 61. Fallback capability equivalence

Подтверждено:

- fallback provider участвует в routing только если он может выполнить конкретный запрос без потери заявленных возможностей
- должны совпадать все существенные параметры request:
  - model capability
  - mode
  - resolution
  - duration
  - reference inputs
  - другие provider-specific features, влияющие на результат
- если provider не поддерживает эквивалентный набор параметров, он исключается из candidate routing для этой generation
- система не имеет права молча деградировать качество, менять режим, игнорировать параметры или подменять request на упрощённый вариант


## 62. Provider routing visibility

Подтверждено:

- выбор конкретного upstream/fallback provider полностью скрыт от партнёра
- наружу партнёр видит только наш контракт:
  - UUID generation
  - status
  - result
  - billing/result metadata, предусмотренные нашим API
- provider name, provider task id, routing attempts, fallback sequence, procurement cost и внутренние причины переключения наружу не раскрываются
- provider-specific details хранятся только во внутренней admin/technical history


## 63. Provider routing priority

Подтверждено:

- если для конкретного запроса доступны несколько полностью эквивалентных providers, routing идёт строго по заранее заданному глобальному приоритету для этой модели
- система не выбирает provider динамически по цене
- система не выбирает provider динамически по текущей latency/скорости
- переход к следующему provider происходит только по предусмотренным причинам:
  - текущий provider недоступен
  - circuit breaker
  - invalid partner provider key
  - economic/preflight constraints
  - request не принят в пределах retry/acceptance flow
  - provider failure, допускающий fallback


## 64. Slow-but-healthy provider behavior

Подтверждено:

- если provider формально доступен и не попал под технический circuit breaker/timeout, он остаётся первым в routing согласно глобальному приоритету
- временное ухудшение latency само по себе не переключает трафик на более быстрый provider
- динамической latency-based балансировки нет
- переключение возможно только когда срабатывают зафиксированные технические условия: timeout, circuit breaker, недоступность, provider-side failure или другие заранее определённые причины fallback


## 65. Safe-to-withdraw display currency

Подтверждено:

- показатель «Безопасно вывести сейчас» показывается в USD
- это заменяет ранее зафиксированное отображение safe-to-withdraw в RUB
- внутренний расчёт по-прежнему может использовать RUB/native provider currencies и FX snapshots, но итоговый admin-facing amount для safe withdrawal отображается в USD


## 66. Safe-to-withdraw limited by actual wallet balance

Подтверждено:

- показатель «Безопасно вывести сейчас» в USD ограничивается не только внутренними обязательствами, но и фактически доступными средствами в Crypto Bot/кошельке
- система не должна показывать safe-to-withdraw выше реально доступного wallet balance
- итоговый safe-to-withdraw = минимум между:
  - суммой, разрешённой внутренней финансовой моделью после всех обязательств/reserves/required provider float
  - фактически доступным wallet balance, приведённым к USD


## 67. Safe-to-withdraw asset scope

Подтверждено:

- показатель «Безопасно вывести сейчас» считается и отображается только в USD
- для фактического wallet cap учитываем только USD/USDT-деноминированный доступный баланс
- GRAM и другие активы не конвертируются и не добавляются в safe-to-withdraw


## 68. Profit withdrawal amount currency

Подтверждено:

- операция «Зафиксировать вывод прибыли» принимает сумму только в USD
- safe-to-withdraw и profit-withdrawal record используют одну admin-facing валюту — USD
- если вывод превышает safe-to-withdraw, сохраняются warning и обязательная причина override, как зафиксировано ранее


## 69. Profit withdrawal record fields

Подтверждено:

- для операции «Зафиксировать вывод прибыли» достаточно сохранять:
  - сумму в USD
  - дату/время
  - внутреннюю причину/комментарий
- tx_hash, blockchain transaction id и внешний payment reference не обязательны


## 70. Partner API reference/file inputs

Подтверждено:

- partner API для референсов и входных файлов принимает только те форматы и способы передачи, которые поддерживает ArgoLink для соответствующей модели/endpoint
- отдельный собственный upload API для файлов в v1 не добавляем
- не вводим дополнительный storage layer только ради загрузки reference/input files
- request contract должен оставаться максимально совместимым с ArgoLink


## 71. Partner API input limits

Подтверждено:

- ограничения по количеству references, размеру input files, длительности входного видео и другим input-параметрам наследуются от конкретной модели/endpoint ArgoLink
- дополнительных собственных лимитов поверх ArgoLink в v1 не вводим
- если provider/model limits меняются, наши docs/validation должны синхронизироваться с актуальным upstream contract


## 72. Crypto Bot balance source for safe-to-withdraw

Подтверждено:

- фактический USD/USDT-баланс для расчёта safe-to-withdraw подтягивается автоматически через API Crypto Bot
- ручной ввод wallet balance в админке не используется как основной источник
- safe-to-withdraw должен опираться на актуальный provider/API balance snapshot и внутренние обязательства
- при недоступности Crypto Bot API система не должна молча считать wallet balance по устаревшему/ручному значению без явного статуса freshness


## 73. USD vs USDT semantics and partner-facing RUB

Уточнение пользователя:

- USD и USDT в финансовом контуре считаются долларовым эквивалентом: USDT — удобный технический способ расчётов, а не отдельная бизнес-валюта продукта
- со стороны владельца поступление/working capital/profit рассматриваются в долларовой логике
- партнёр при этом работает только с RUB:
  - задаёт сумму пополнения в RUB
  - видит balance в RUB
  - видит partner prices/charges в RUB
- crypto asset используется как транспорт платежа; партнёру не нужно вести отдельный USDT-баланс внутри сервиса
- safe-to-withdraw показывается в USD; фактический доступный USDT может использоваться как долларовый эквивалент для этого показателя


## 74. Internal reporting currency

Подтверждено:

- внутренняя управленческая отчётность ведётся и показывается преимущественно в USD/USDT
- к ней относятся:
  - profit
  - working capital
  - provider float
  - safe-to-withdraw
  - internal cost/margin views
- RUB остаётся основной валютой partner-facing billing:
  - balance
  - partner prices
  - charges
  - top-up amount
- исторические RUB snapshots по top-up/charge сохраняются для сверки и аудита


## 75. Internal admin currency label

Подтверждено:

- во внутренней админке для долларовых показателей используем единый ярлык `USDT`
- не показываем одновременно USD и USDT как два отдельных значения
- внутренние показатели profit, working capital, provider float и safe-to-withdraw отображаются в USDT


## 76. Margin reporting currency

Подтверждено:

- внутреннюю маржу показываем в `USDT` и в процентах
- RUB-маржа не является основным admin-facing показателем
- это соответствует тому, что расчёты с upstream-поставщиками и внутренний финансовый контур ведутся в USDT
- partner-facing billing при этом по-прежнему остаётся в RUB


## 77. Immutable FX snapshot for generation margin

Подтверждено:

- для каждой успешно завершённой генерации фиксируем один immutable курс RUB/USDT на момент финализации списания
- этот курс используется для перевода partner partner charge из RUB в USDT для внутреннего расчёта маржи
- формулы:
  - revenue_usdt = charged_rub / fx_snapshot_rub_per_usdt
  - margin_usdt = revenue_usdt - actual_provider_cost_usdt
  - margin_percent = margin_usdt / revenue_usdt * 100
- исторические генерации не переоцениваются по текущему курсу
- вместе с генерацией сохраняется использованный FX snapshot для последующей сверки и отчётности


## 78. Safe-to-withdraw fallback on stale Crypto Bot balance

Подтверждено:

- если Crypto Bot API временно не отдаёт актуальный wallet balance, safe-to-withdraw продолжаем считать по последнему успешно полученному балансу
- последнее успешное значение баланса кэшируется/хранится как fallback
- при использовании устаревшего wallet balance расчёт не блокируется
- freshness последнего успешного balance snapshot должна быть доступна системе и администратору


## 79. Manual RUB/USDT FX override in admin

Подтверждено:

- в Telegram admin должна быть возможность вручную указать курс RUB/USDT
- ручной курс нужен как управляемый fallback/override, если автоматический источник курса временно недоступен или администратор хочет использовать заданное значение
- источник курса и факт manual override должны сохраняться вместе с FX snapshot, чтобы было понятно, каким курсом посчитана конкретная операция


## 80. Manual FX override lifetime

Подтверждено:

- ручной курс RUB/USDT после включения действует постоянно, пока администратор сам его не отключит
- при активном manual override автоматический курс не используется для новых FX snapshots
- после отключения manual override система возвращается к автоматическому источнику курса
- в истории сохраняются факт включения/отключения и источник курса для каждой операции


## 81. Manual FX rate is fallback only

Уточнение, supersedes предыдущую трактовку manual override:

- ручной курс RUB/USDT используется только как fallback
- пока автоматический источник курса доступен, для новых расчётов используется автоматический курс
- ручное значение можно заранее задать в админке и держать активным/сохранённым, но оно применяется только при недоступности автоматического курса
- если автоматический источник восстановился, система автоматически возвращается к нему без ручного переключения
- для каждого FX snapshot сохраняется источник курса: automatic или manual_fallback


## 82. FX source fallback order

Подтверждено:

- порядок источников курса RUB/USDT:
  1. актуальный автоматический курс
  2. ручной fallback-курс, если он задан
  3. последний известный автоматический курс
- если automatic source временно недоступен и manual fallback не задан, новые RUB↔USDT расчёты продолжаются по последнему успешно полученному автоматическому курсу
- вместе с каждым FX snapshot сохраняется источник курса и timestamp последнего успешного automatic rate


## 83. Safe-to-withdraw wallet balance freshness

Подтверждено:

- если safe-to-withdraw рассчитан по последнему известному Crypto Bot balance, администратор видит возраст этого snapshot
- в admin UI рядом с показателем показываем понятный freshness status, например: `баланс обновлён 2 ч 17 мин назад`
- это позволяет отличить расчёт по свежему wallet balance от расчёта по stale fallback


## 84. Paid-but-not-yet-credited deposits in safe-to-withdraw

Подтверждено:

- Crypto Bot payment, уже подтверждённый как paid, но ещё не зачисленный партнёру вручную, считается обязательством
- такая сумма сразу исключается из safe-to-withdraw
- до нажатия администратором «Зачислить баланс» эти средства нельзя считать доступной прибылью/свободным working capital
- после зачисления обязательство переходит в обычный partner balance / cost coverage контур и продолжает учитываться в общей финансовой модели


## 85. Safe-to-withdraw reserves only future provider cost coverage

Подтверждено:

- после ручного зачисления partner top-up в safe-to-withdraw не резервируется весь partner RUB balance целиком
- резервируется только консервативно необходимое покрытие будущей upstream себестоимости
- расчёт должен ориентироваться на худшее текущее соотношение provider cost / partner price среди доступных партнёру production-конфигураций, а не на среднюю маржу
- отдельно продолжают учитываться:
  - active generation reserves
  - paid-but-not-yet-credited obligations
  - required provider working float
  - фактический доступный USDT wallet cap
- цель: не финансировать обычные генерации из owner capital, но и не замораживать уже экономически заработанную маржу до полного расходования partner RUB balance


## 86. No dynamic repricing of future cost coverage

Подтверждено:

- уже сформированное покрытие будущей себестоимости для зачисленного partner balance не пересчитывается динамически при последующих изменениях procurement price или partner price
- изменение текущих цен влияет на новые операции и новые snapshots, но не должно автоматически переписывать ранее зафиксированное покрытие по уже зачисленному балансу


## 87. Cost coverage snapshot at manual top-up credit

Подтверждено:

- покрытие будущей upstream-себестоимости фиксируется в момент, когда администратор вручную зачисляет оплаченное пополнение партнёру
- для этого snapshot используются действующие на тот момент:
  - partner prices
  - procurement/provider costs
  - RUB/USDT FX rate согласно текущей цепочке источников курса
- полученное cost coverage по уже зачисленному top-up далее не переоценивается автоматически из-за изменения цен или курса


## 88. Block generation when cost coverage is exhausted

Подтверждено:

- если из-за изменения upstream-себестоимости cost coverage партнёра исчерпан раньше его partner RUB balance, новые генерации блокируются
- наличие положительного partner RUB balance само по себе недостаточно для запуска генерации
- разблокировка возможна после:
  - нового пополнения, которое создаёт новое cost coverage
  - либо отдельной admin-операции «Пополнить покрытие себестоимости»
- это сохраняет правило: обычные генерации не финансируются из owner capital


## 89. Historical cost coverage vs current reserve requirement

Уточнение, supersedes жёсткий блок из раздела 88:

- зафиксированный при top-up historical cost coverage остаётся immutable учётным snapshot и не переписывается при изменении цен
- его исчерпание само по себе НЕ блокирует партнёра, если у партнёра ещё есть partner RUB balance
- для safe-to-withdraw отдельно рассчитывается current required reserve по актуальной upstream-себестоимости оставшихся обязательств
- рост provider cost уменьшает safe-to-withdraw и доступный owner profit, но не должен сам по себе неожиданно остановить API партнёра
- новую генерацию блокируем только если:
  - реально не хватает доступного working capital / provider float покрыть её фактическую себестоимость
  - либо генерация стала экономически отрицательной по текущим ценам
- historical cost coverage используется для учёта и сверки, current required reserve — для текущего risk/safe-withdraw контроля


Реализация проверки текущего капитала уточнена 2026-09-29 после production-инцидента:
деньги, уже внесённые на upstream wallet, допускают генерации независимо от нулевого
Crypto Bot wallet. Для выбранного credential запрашивается свежий подтверждённый
денежный balance; из него консервативно вычитаются глобальные active reserves,
paid-but-not-credited obligations и required float. Quota/remaining без доказанного
денежного wallet не считаются капиталом. Недоступный или неподтверждённый balance
даёт прежний нейтральный 503 до создания заявки и списаний. Проверки partner RUB balance
и экономики сохраняются. Prepaid balance не прибавляется к safe-to-withdraw и не создаёт
учётных поступлений/пополнений партнёра. Глобальные резервы могут консервативно ограничивать
независимые upstream accounts, пока нет надёжного идентификатора общего кошелька.


## 90. Neutral error for economically unavailable generation

Подтверждено:

- если конкретная генерация стала экономически отрицательной из-за актуальной upstream-себестоимости, партнёру не раскрываем причину про маржу, закупочную цену или внутреннюю экономику
- запрос отклоняется до создания generation UUID / reserve
- partner-facing ответ: HTTP 503 с нейтральным стабильным кодом `provider_temporarily_unavailable`
- `idempotency_key` при таком pre-creation reject не расходуется и может быть безопасно повторён позже


## 91. Profit withdrawal action is accounting-only

Подтверждено:

- кнопка «Зафиксировать вывод прибыли» только записывает факт вывода во внутренний учёт
- система не инициирует перевод USDT через Crypto Bot API и не управляет фактическим выводом средств
- физический вывод владелец выполняет отдельно вне системы
- запись уменьшает доступный working capital / safe-to-withdraw согласно финансовой модели


## 92. Immutable profit withdrawal history

Подтверждено:

- записи о зафиксированном выводе прибыли после создания не редактируются и не удаляются
- если администратор ошибся, создаётся отдельная корректирующая обратная запись
- корректирующая запись должна ссылаться на исходную операцию или иным образом сохранять связь с ней
- история выводов остаётся append-only и пригодной для последующей сверки


## 93. Partial correction of profit withdrawal records

Подтверждено:

- корректирующая запись по выводу прибыли может быть частичной
- пример: если ошибочно зафиксирован вывод `500 USDT`, а фактически было `450 USDT`, создаётся отдельная корректирующая запись `+50 USDT`
- исходная запись остаётся неизменной
- итоговый учётный эффект определяется суммой исходной и всех связанных корректирующих записей


## 94. Negative safe-to-withdraw shows coverage deficit

Подтверждено:

- если расчёт safe-to-withdraw уходит ниже нуля, в админке не обрезаем значение до `0 USDT`
- показываем фактический отрицательный результат, например `-120 USDT`
- отрицательное значение трактуется как дефицит покрытия / working capital относительно текущих обязательств
- это позволяет администратору видеть реальную глубину дефицита, а не только факт отсутствия доступной прибыли к выводу


## 95. Profit withdrawal override when safe-to-withdraw is negative

Подтверждено:

- даже если safe-to-withdraw отрицательный, действие «Зафиксировать вывод прибыли» не блокируется полностью
- администратор может продолжить только через жёсткий override
- перед подтверждением показывается критическое предупреждение о дефиците покрытия и риске
- обязательна текстовая причина override
- причина сохраняется вместе с записью вывода во внутренней append-only истории


## 96. Telegram alert on negative safe-to-withdraw

Подтверждено:

- если safe-to-withdraw становится отрицательным, администратору автоматически отправляется Telegram-alert
- alert должен явно показывать факт дефицита покрытия и его величину в USDT
- дефицит также остаётся видимым в admin UI как отрицательное значение safe-to-withdraw


## 97. Negative safe-to-withdraw alert repeats until manual cancellation

Подтверждено:

- Telegram-alert о отрицательном safe-to-withdraw повторяется, пока администратор вручную не отменит/заглушит этот alert
- автоматическое восстановление safe-to-withdraw выше нуля само по себе не является единственным механизмом остановки уже запущенного alert cycle
- в admin UI должна быть явная ручная action для прекращения повторных уведомлений по текущему инциденту


## 98. Negative safe-to-withdraw alert interval

Подтверждено:

- Telegram-alert о отрицательном safe-to-withdraw повторяется каждые 15 минут
- интервал совпадает с другими финансовыми/операционными alert-циклами системы
- повтор продолжается до ручной отмены/заглушения администратором


## 99. New deficit after recovery starts a new alert incident

Подтверждено:

- если администратор вручную заглушил alert по отрицательному safe-to-withdraw, затем показатель восстановился до нуля/плюса, а позже снова ушёл в минус, это считается новым инцидентом
- для нового инцидента Telegram-alert автоматически запускается заново
- предыдущая ручная отмена не переносится на будущие независимые случаи дефицита


## 100. Unpaid invoices are not obligations

Подтверждено:

- созданные Crypto Bot invoices до фактической оплаты вообще не считаются обязательствами
- unpaid/pending invoice не уменьшает safe-to-withdraw
- обязательство возникает только после подтверждённой фактической оплаты
- после paid confirmation сумма сразу исключается из safe-to-withdraw, даже если администратор ещё не зачислил партнёру RUB balance


## 101. Refunded/cancelled paid invoice releases safe-to-withdraw obligation

Подтверждено:

- если ранее оплаченный invoice вручную получает финальный статус refund/cancelled вне системы, связанное с ним обязательство снимается
- после фиксации такого финального статуса соответствующая сумма снова перестаёт уменьшать safe-to-withdraw
- изменение должно быть отражено в append-only финансовой истории, без удаления исходного факта оплаты


## 102. Automatic reversal after confirmed refund

Подтверждено:

- если invoice ранее был оплачен и RUB-пополнение уже зачислено партнёру, а затем администратор подтверждает фактический refund, система автоматически создаёт обратную корректировку
- с partner RUB balance списывается ровно исходная зачисленная RUB-сумма
- связанный historical cost coverage разворачивается симметричной корректировкой по исходным snapshot-значениям
- текущий FX rate не используется; берутся исходные значения операции
- исходное пополнение и refund остаются отдельными append-only записями
- если после корректировки partner RUB balance становится отрицательным, отрицательное значение сохраняется, а новые генерации блокируются до восстановления баланса


## 103. Partial refund creates proportional reversal

Подтверждено:

- если фактический refund по ранее зачисленному invoice частичный, система создаёт только пропорциональную обратную корректировку
- partner RUB balance уменьшается пропорционально фактически возвращённой части исходной суммы
- historical cost coverage разворачивается в той же пропорции по исходным snapshot-значениям
- оставшаяся незарефанденная часть invoice продолжает считаться действительным пополнением
- исходная операция и все partial refund adjustments сохраняются как отдельные append-only записи


## 104. Partner-facing refund visibility

Подтверждено:

- детали refund и внутренняя причина возврата остаются только во внутренней админской истории
- партнёру не показываем термин `refund`, детали Crypto Bot и внутренний комментарий
- если refund меняет partner RUB balance, в partner history показывается нейтральная запись `Корректировка баланса` с суммой и датой
- та же нейтральная корректировка должна попадать в partner XLSX, чтобы reconciliation баланса сходился


## 105. No automatic partner notification for refund-induced negative balance

Подтверждено:

- если refund/обратная корректировка уводит partner RUB balance в минус, система не отправляет партнёру отдельное автоматическое Telegram-уведомление об этом
- администратор сообщает партнёру вручную
- при этом сам отрицательный balance и стандартная блокировка новых генераций сохраняются в системе
- refund-причина и внутренняя платёжная механика партнёру по-прежнему не раскрываются


## 106. Standard low-balance alerts still apply after refund adjustment

Подтверждено:

- отдельное refund-specific уведомление партнёру не отправляется
- стандартный low-balance alert продолжает работать по обычным правилам, даже если снижение баланса вызвано refund/обратной корректировкой
- сообщение остаётся нейтральным: показывает низкий/отрицательный баланс и необходимость пополнения без раскрытия refund, Crypto Bot деталей или внутренней причины
- если баланс отрицательный, стандартная блокировка новых генераций сохраняется


## 107. Existing partner prices are not repriced by new model rollout

Уточнение пользователя:

- включение новой production-модели не меняет уже действующие partner prices по существующим моделям
- уже зафиксированные partner balances и их исторические pricing/cost snapshots не переоцениваются из-за появления новой модели
- новая модель получает собственную partner price при публикации, но не используется как основание для пересчёта старых цен партнёра


## 108. partner price changes apply only to new generations

Подтверждено:

- если администратор меняет partner-цену уже существующей модели, новая цена применяется только к новым генерациям, созданным после момента изменения
- уже созданные, queued, processing и иные in-flight генерации продолжают использовать price snapshot, зафиксированный при их создании
- завершённые исторические операции не пересчитываются задним числом


## 109. partner price change publication without Telegram broadcast

Подтверждено:

- при изменении partner-цены существующей модели отдельное автоматическое Telegram-уведомление партнёрам не требуется
- достаточно обновить:
  - authenticated `/pricing`
  - docs/public price
  - changelog
- новые генерации используют новую цену с момента её активации; исторические и in-flight generation price snapshots не меняются


## 110. Atomic partner price publication

Подтверждено:

- изменение partner-цены применяется атомарно
- новая цена начинает использоваться для новых generation requests только после одновременной публикации согласованного состояния:
  - API pricing catalog / `/pricing`
  - public price/docs
  - changelog
- до завершения публикации продолжает действовать предыдущая цена
- in-flight и historical generation price snapshots не меняются

## 111. Partner-specific partner price overrides

Новое подтверждённое требование, supersedes прежнюю трактовку полностью глобальных partner-цен:

- администратор должен иметь возможность задавать отдельные partner-цены для конкретных партнёров по отдельным моделям
- при наличии partner-specific override для партнёра используется его цена вместо глобальной default price
- если override отсутствует, действует глобальная цена модели
- model availability при этом остаётся глобальной: индивидуальная цена не означает индивидуальное включение/выключение модели
- partner-specific price changes применяются только к новым генерациям после активации и не меняют historical/in-flight price snapshots
- partner-facing `/pricing` должен возвращать фактические цены именно этого партнёра с учётом его overrides


## 112. Revert partner-specific partner price overrides

Уточнение пользователя, supersedes раздел 111:

- индивидуальные partner-цены для отдельных партнёров не вводим
- partner pricing остаётся глобальным для всех партнёров
- для video сохраняется общая детализация pricing catalog по `model + mode + resolution` в RUB/sec
- partner-facing `/pricing` возвращает те же глобальные цены, действующие для всех партнёров
- model availability и partner prices остаются едиными для всей партнёрской базы


## 113. Global partner price changes take effect immediately

Подтверждено:

- после подтверждения администратором новая глобальная partner-цена вступает в силу сразу
- отложенного запуска по дате/времени не требуется
- активация остаётся атомарной: новая цена применяется к новым генерациям только вместе с обновлёнными `/pricing`, public price/docs и changelog
- уже созданные и исторические генерации продолжают использовать свои price snapshots


## 114. Canonical pricing terminology

Подтверждено:

- термин `retail price` больше не используем
- канонический термин: `partner price` / «цена для партнёра»
- это цена, по которой Нейроныч продаёт генерацию партнёру и списывает средства с его RUB-баланса
- цена, по которой партнёр перепродаёт генерацию своему конечному пользователю, находится вне нашего биллинга и не участвует в нашей финансовой модели
- связанные термины в спецификации нормализованы: `partner RUB balance`, `partner charge`, `partner charge reserve`, `partner pricing`


## 115. Minimal confirmation UI for partner price changes

Подтверждено:

- при ручном изменении одной partner price перед сохранением показываем минимальный confirmation diff:
  - старая partner price
  - новая partner price
  - ожидаемая margin %
- абсолютную маржу в USDT на этом экране не показываем, чтобы не усложнять интерфейс
- если новая margin % ниже 30%, показываем заметное предупреждение, но сохранение не блокируем
- для массового XLSX import сохраняется отдельный более подробный diff-flow, как зафиксировано ранее


## 116. Partner price change history retention

Подтверждено:

- историю изменений partner price храним бессрочно
- для каждой записи сохраняем:
  - кто изменил
  - когда изменил
  - предыдущую цену
  - новую цену
  - затронутую model/mode/resolution конфигурацию
- история append-only и используется для финансовой сверки и разбора спорных случаев


## 117. Price history lives inside model/configuration card

Подтверждено:

- отдельный глобальный экран истории partner price не нужен
- историю изменений показываем внутри карточки конкретной model/mode/resolution конфигурации
- в карточке видны бессрочные append-only записи изменений цены: кто, когда, старая цена, новая цена


## 118. Prevent partner price below current provider cost

Подтверждено:

- администратору запрещено сохранять partner price ниже текущей upstream-себестоимости для соответствующей конфигурации
- UI должен блокировать сохранение такой цены и явно показывать, что цена ниже себестоимости
- предупреждение о margin <30% остаётся неблокирующим только пока маржа положительная
- экономически отрицательная partner price не может быть опубликована


## 119. Alert when provider cost overtakes active partner price

Подтверждено:

- если upstream-себестоимость выросла после публикации и действующая partner price стала ниже текущей себестоимости, автоматически выключать модель/конфигурацию не требуется
- администратору отправляется Telegram-alert о возникшей отрицательной экономике
- пока экономика отрицательная, новые generation requests по этой конфигурации отклоняются по уже зафиксированному правилу:
  - HTTP 503
  - `provider_temporarily_unavailable`
  - без раскрытия партнёру причин про себестоимость/маржу
  - без создания UUID/reserve и без расходования `idempotency_key`
- после изменения partner price или восстановления положительной экономики новые запросы снова могут проходить без отдельного ручного enable


## 120. Negative-economics alert frequency

Подтверждено:

- если upstream-себестоимость обгоняет действующую partner price и экономика конфигурации становится отрицательной, администратору достаточно одного Telegram-уведомления на инцидент
- повторные уведомления каждые 15 минут не нужны
- новый alert отправляется только при новом независимом инциденте после восстановления положительной экономики и последующего повторного ухода в отрицательную


## 121. Low-margin alert frequency

Подтверждено:

- если partner price остаётся выше текущей upstream-себестоимости, но margin падает ниже 30%, администратору отправляется одно Telegram-уведомление на инцидент
- повторные уведомления каждые 15 минут не нужны
- генерации продолжают выполняться, пока экономика остаётся положительной
- новый alert отправляется только после восстановления margin до 30%+ и последующего повторного падения ниже 30%


## 122. Configurable low-margin alert threshold

Подтверждено:

- порог low-margin alert настраивается администратором
- значение по умолчанию — 30%
- настройка глобальная для системы
- если фактическая margin падает ниже текущего порога, отправляется одно Telegram-уведомление на инцидент
- изменение порога влияет только на будущую оценку инцидентов и не переписывает исторические расчёты маржи


## 123. Per-model low-margin alert threshold

Подтверждено:

- помимо глобального default low-margin threshold должна быть возможность задать отдельный threshold для конкретной модели
- если для модели задан собственный threshold, он имеет приоритет над глобальным значением
- если override для модели отсутствует, используется глобальный threshold
- threshold влияет только на alerting/monitoring и не меняет partner price автоматически
- генерации продолжаются при положительной экономике даже ниже alert-threshold; при отрицательной экономике действует отдельное правило блокировки новых запросов


## 124. Per-configuration low-margin alert thresholds

Подтверждено:

- low-margin threshold можно задавать не только глобально и на уровне модели, но и отдельно для конкретной `mode + resolution` конфигурации внутри модели
- приоритет применения threshold:
  1. configuration-level override (`model + mode + resolution`)
  2. model-level override
  3. global default
- threshold влияет только на alerting/monitoring
- partner price автоматически не меняется
- при положительной экономике генерации продолжаются даже ниже threshold; при отрицательной экономике действует отдельная блокировка новых запросов


## 125. Low-margin threshold change history retention

Подтверждено:

- историю изменений low-margin thresholds храним бессрочно
- история хранится в соответствующей карточке модели/конфигурации, аналогично истории partner price
- для каждой записи сохраняем:
  - кто изменил
  - когда изменил
  - уровень настройки: global / model / configuration
  - предыдущее значение
  - новое значение
- история append-only


## 126. Lowering threshold below current margin has no side effects

Подтверждено:

- если администратор снижает low-margin threshold ниже текущей фактической маржи, настройка просто сохраняется
- дополнительных alert, блокировок, пересчётов или иных действий не требуется
- новая граница применяется только к последующей оценке low-margin incidents


## 127. Public documentation exposure

Подтверждено, supersedes прежнее решение о публичном полном Swagger/OpenAPI:

- открытая документация без авторизации должна быть минимальной
- публично показываем только то, что нужно партнёру для подключения моделей:
  - production API base URL
  - формат partner API authentication header
  - список/ID доступных production-моделей
  - endpoint family, необходимый для подключения каждой модели
- полная OpenAPI schema не должна быть публично доступна
- публичные Swagger UI / ReDoc с полным API contract отключаются
- открытая документация не должна раскрывать:
  - billing/ledger/admin endpoints
  - provider internals
  - webhook operational details
  - retry/incident/reconciliation internals
  - support/admin/financial APIs
- полная техническая документация может быть доступна авторизованному партнёру внутри закрытого partner surface, но не является публичной страницей
- `/docs` используется как минимальная публичная страница подключения моделей; `/guide` может оставаться её alias

## 128. Self-contained partner inference reference (2026-09-28)

Уточнение владельца: партнёр должен пользоваться нашим API только по нашей документации,
без обращения к документации первого провайдера и без раскрытия его названия/адресов.
Это расширяет публичный model connection guide из §127 до справочника inference API:

- `/docs` и `/guide`, RU/EN: авторизация, идемпотентность, доступные модели, все поддерживаемые
  семейства запросов, типы/обязательность параметров, ограничения и совместимость опций;
- самостоятельные примеры текста, изображений, видео, загрузки референса, статуса и скачивания;
- точные собственные ID, коды/форматы ответов, ошибки и безопасное поведение клиента при обрыве;
- ограничения описываются по контракту нашего API, без обещания автоматической поддержки новых
  возможностей внешнего каталога; присутствие модели в примере не означает её включение;
- исполняемые примеры проверяются локально, без платных генераций и тестовых записей в production;
- полный OpenAPI, административные/финансовые/служебные маршруты, закупочные цены,
  данные маршрутизации, ключи и идентификаторы провайдера по-прежнему не публикуются.

Клиентские правила повторной отправки относятся к публичному контракту; внутренние механизмы
очередей, восстановления и reconciliation остаются закрытыми.

Уточнение контракта загрузки референсов (2026-09-29): `POST /v1/media/uploads`
выдаёт upload ticket по JSON-описанию файла. Сам файл отправляется отдельным PUT
по подписанному `upload_url`, без partner API key. Явно неподдерживаемый Content-Type
возвращает `415 media_upload_requires_json`, повреждённое JSON-тело —
`422 invalid_request_contract`; входные байты не должны вызывать необработанный 500.
