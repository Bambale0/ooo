# Проверка готовности репозитория — 23 сентября 2026

Подготовлен release candidate для изолированного запуска и выборочного включения
проверенных моделей. **Продажи всех моделей ArgoLink пока не подтверждены:** 9 из
39 моделей не дали рабочего результата в live-проверке. Production не разворачивался;
в этой задаче согласованы изменения репозитория и изолированные проверки.

## Реализовано

- Проверенный каталог 39 моделей: 24 текстовые, 9 графических, 6 видео. Нативные
  Responses, Chat Completions, Messages, Images и асинхронный Videos API; JSON/SSE,
  multipart image edits, upload tickets, референсы image/video/audio и video edit.
  GPT Image принимает 16 файлов-референсов плюс отдельную маску.
- Импорт каталогов в draft, сравнение изменений контракта/закупочных цен, ручные
  retail prices и gates перед включением. Ложный smoke PASS автоматически не ставится.
  Параметры и модель не заменяются для обхода ошибок поставщика.
- Резерв до отправки, durable submit intent, запрет слепого повторного платного
  запроса, фактический расчёт tokens/images/seconds, immutable snapshots и
  компенсирующие ledger entries. Неопределённые исходы доступны для admin reconciliation.
  Отмена до provider submit возвращает оба резерва; queued задачи отключённого/удалённого
  аккаунта также отменяются до отправки. Уже принятые upstream задачи продолжают учитываться.
- Персональные зашифрованные upstream credentials, привязка задач к исходному ключу,
  tenant isolation, DNS-pinned HTTPS для webhook и проверки image URLs, редактирование
  секретов в логах, ограниченные метки Prometheus.
- Казначейство с реальным USDT wallet snapshot, отдельными текущими резервами и
  историческим покрытием; safe-to-withdraw, audit вывода/коррекций, FX fallback chain.
  Уведомления о дефиците сохраняются в БД, повторяются через 15 минут, заглушаются
  для текущего инцидента и возобновляются при новом дефиците после восстановления.
- Русский Telegram-кабинет: согласия/заявка, admin approval/rejection, ключи и webhook,
  счета/проверка оплаты/зачисление, история и поиск UUID, перенос/отключение/удаление
  аккаунта, поддержка с файлами до 20 МБ, ответы/закрытие обращений и notification outbox.
  Состояния и подтверждения переживают перезапуск; роль и владелец проверяются повторно.
- Lifetime trial: два бесплатных запуска видеомоделей на Telegram ID, все native
  параметры, нулевая retail charge при учёте upstream cost и реальном capital gate.
  Admin forms: цены/пороги, refund/withdrawal/corrections, сверки и provider/model configuration.
  Пороги low-margin имеют три уровня и append-only history; смена документов создаёт
  устойчивое уведомление и требует явного подтверждения отображённой версии в кабинете.
- Durable provider circuit: бесплатные health checks, три успешные реальные recovery
  задачи по одной, ускоренное повторное отключение; принятые задачи продолжают polling.
  Точные условия описаны в [cabinet/recovery](CABINET_AND_RECOVERY.md).
- Подделка Telegram-согласия через открытый HTTP endpoint закрыта. HTTP-подача заявки
  требует admin auth; обычная регистрация идёт через Telegram. Старый Telegram ID
  освобождается при удалении для новой заявки, финансовая история сохраняется.
- Неопределённое создание платёжного счёта не повторяется автоматически. Возможны
  read-only lookup и явная проверяемая привязка найденного invoice администратором.
  Повторное подтверждение paid не сбрасывает уже зачисленный платёж.
- Публичные `/guide?lang=ru|en`, `/prices` и OpenAPI; публичные цены содержат только RUB.
- Непривилегированный app image, отдельные API/workers/bot, production config checks,
  миграции, locked dependencies, CI/security checks и проверка точной версии при release.
- Зашифрованные logical backups с вложениями поддержки и отдельный WAL/PITR overlay.
  Реальный restore drill и тест восстановления на заданный момент включены в CI.

## Доказательства

[Live-матрица и квота](LIVE_VERIFICATION_2026-09-23.md),
[контракты](ARGOLINK_CONTRACT.md), [финансовая модель](TREASURY.md),
[эксплуатация](OPERATIONS.md), [backup/DR](../ops/backup/README.md).

- PostgreSQL 16: миграции до `20260923_0020`, `alembic check` без drift.
- Unit/integration suite включает финансовые повторы, конкуренцию PostgreSQL,
  crash/restart generation flow, клиентский SSE disconnect, tenant isolation,
  FX/кошелёк, перенос аккаунта и подтверждения бота. Итоговый результат запуска
  фиксируется в PR и CI; SQLite не подменяет concurrency-проверки PostgreSQL.
- Populated logical restore: партнёр, платёж, генерация, API-ключ, оба ledger и
  файл поддержки восстановлены; несовпадений балансов 0; локально 2 секунды.
- PITR: базовая транзакция восстановлена, транзакция после backup проиграна из
  зашифрованного WAL, транзакция после выбранного времени исключена; локально 5.97 с.
  Это небольшие тестовые данные, а не RTO production-нагрузки.
- Live ArgoLink: 30/39 моделей дали результат хотя бы через один протокол;
  все 6 видео-моделей приняли предоставленный референс. Через наш gateway также
  проверены text/SSE, multipart image edit и video edit с image+video+audio.
- Реальный Telegram: проверены getMe и getWebhookInfo. Updates не потреблялись,
  сообщения реальным пользователям не отправлялись; кабинет проверен fake updates.

## Что остаётся условием реального запуска

1. Ошибки upstream: 6 текстовых и 3 Nano-модели не подтверждены; некоторые GPT
   работают через Chat, но возвращают 502 через Responses. Эти конфигурации нельзя
   продавать как прошедшие проверку. Нужен повторный smoke после исправления ArgoLink.
2. Проверка каждой продаваемой конфигурации (протокол, режим, размер, число выходов)
   и её закупочной цены под конкретным partner credential. Особенно Grok image edits:
   live списания расходились с опубликованной ценой. Все комбинации не объявлены PASS.
3. Deployment inputs: РФ-сервер/домен/TLS, production Crypto Pay, опубликованные
   оферта/политика, ADMIN_TELEGRAM_ID, согласованные retail prices и opening capital.
   Эти данные не подменены тестовыми значениями и не являются частью изолированного deploy.
4. Независимое РФ backup storage, расписание backups/weekly recovery, доставка
   мониторинга и off-site restore на объёме production. Репозиторий содержит
   работающие encryption/WAL/restore paths; география storage и RTO по сети требуют
   проверки в целевом окружении.
5. Опубликованная документация Gemini Omni не сопровождается моделью/ценой в текущем
   live-каталоге. Модель не включена в resale и не получила выдуманный тариф.

Покрытие реализации и ограничения проверок перечислены в
[implementation status](../IMPLEMENTATION_STATUS.md). Зелёный CI не подтверждает
production SLO, внешнюю инфраструктуру или ещё не прошедшие live режимы upstream.

Мерж в main запускает существующий production workflow. В рамках этой задачи
изменения публикуются в PR, без мержа и без запуска production.

## Использованные skills

Решения и проверки опирались на `team-lead`, `security-audit`, `devops`,
`aiogram-codegen`, `bot-ux-designer`, `bot-tester`, а также project-local
`.agents/skills/api-security-best-practices/SKILL.md` и
`.agents/skills/verification-before-completion/SKILL.md`.
