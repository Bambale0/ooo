# Нейроныч SaaS — production launch checklist

> Цель: жёсткий чек-ап готового продукта перед запуском.
> Статусы: `PASS / FAIL / WARN / N/A`.
> Любой `CRITICAL FAIL` блокирует запуск.
> Проверка репозитория и изолированные результаты от 23.09.2026: [production review](docs/PRODUCTION_REVIEW.md).
> Эти результаты не отмечают автоматически инфраструктурные production gates ниже.

## 1. Инфраструктура — CRITICAL

- [ ] production реально поднят на РФ VPS
- [ ] PostgreSQL, Redis, workers, FastAPI и Telegram backend работают после полного restart
- [ ] Nginx/TLS работают на `api.нейроныч.online`
- [ ] Telegram proxy работает отдельно и не хранит бизнес-данные
- [ ] test и prod используют разные DB/Redis/secrets/config
- [ ] production secrets отсутствуют в GitHub
- [ ] health/readiness действительно ловят падение DB/provider dependencies
- [ ] время сервера/NTP корректно
- [ ] disk/RAM/CPU имеют запас
- [ ] после reboot VPS система поднимается автоматически

## 2. Backup / Disaster Recovery — CRITICAL

- [ ] PostgreSQL WAL archive реально работает
- [ ] daily backup реально создаётся
- [ ] backup уходит на отдельный РФ storage/MinIO
- [ ] backup зашифрован
- [ ] recovery key хранится вне обоих серверов
- [ ] проведён настоящий restore-test в чистую БД
- [ ] после restore проверены balances, payments, generations, API keys и configs
- [ ] измерен фактический RTO
- [ ] целевой RTO <= 15 минут
- [ ] weekly restore-test автоматизирован
- [ ] DR runbook существует у владельца вне Git/server
- [ ] последний DR/restore = PASS

## 3. Partner onboarding — CRITICAL

- [ ] новая заявка проходит end-to-end
- [ ] duplicate pending application не создаётся
- [ ] consent policy/terms сохраняет version + timestamp + TG ID
- [ ] reject работает
- [ ] rejected partner может reapply
- [ ] approval невозможен без валидного Argo/provider key
- [ ] approved partner получает cabinet
- [ ] transfer Telegram ID протестирован
- [ ] delete account протестирован
- [ ] negative balance запрещает delete
- [ ] re-register после delete создаёт чистый новый account

## 4. API keys / Auth — CRITICAL

- [ ] создание partner API key работает
- [ ] secret показывается только один раз
- [ ] в БД нет plaintext API key
- [ ] Bearer auth работает
- [ ] revoked key перестаёт работать немедленно
- [ ] несколько keys одного партнёра используют общий balance
- [ ] admin API key rotation инвалидирует старый
- [ ] unauthorized requests не раскрывают внутреннюю информацию

## 5. Models / ArgoLink compatibility — CRITICAL

- [ ] Seedance 2 полностью протестирован
- [ ] Seedance 2.5 полностью протестирован
- [ ] Grok Imagine полностью протестирован
- [ ] все заявленные modes/resolutions/durations/references реально работают
- [ ] input limits совпадают с upstream
- [ ] Argo-compatible contract проверен там, где заявлена совместимость
- [ ] provider errors корректно превращаются в stable errors
- [ ] raw provider errors/task IDs партнёру не уходят
- [ ] disabled model недоступна партнёру
- [ ] enable модели невозможен без price + docs + integration + smoke

## 6. Generation state machine — CRITICAL

- [ ] `queued -> sent_to_provider -> processing -> completed`
- [ ] failed flow работает
- [ ] timeout flow работает
- [ ] cancelled flow работает
- [ ] неправильный переход состояния невозможен
- [ ] restart worker не теряет generation
- [ ] deploy не теряет queued tasks
- [ ] accepted task не отправляется provider повторно
- [ ] late provider success после timeout обработан
- [ ] result после late success появляется
- [ ] charge при late success применяется ровно один раз

## 7. Idempotency — CRITICAL

- [ ] каждый create требует `idempotency_key`
- [ ] повтор того же accepted request возвращает тот же UUID
- [ ] duplicate provider task не создаётся
- [ ] duplicate charge невозможен
- [ ] idempotency сохраняется после restart/deploy
- [ ] pre-creation `503 provider_temporarily_unavailable` не сжигает key
- [ ] pre-creation economic reject можно повторить тем же key позже

## 8. Billing / Ledger — CRITICAL

- [ ] partner balance хранится в RUB
- [ ] precision до `0.01 ₽`
- [ ] intermediate calculations не используют float
- [ ] partner charge округляется ROUND_HALF_UP
- [ ] partner price snapshot фиксируется при generation creation
- [ ] изменение partner price не меняет in-flight generation
- [ ] balance deduction идемпотентна
- [ ] failed uncharged generation списывает `0 ₽`
- [ ] successful generation списывает правильную сумму
- [ ] manual balance +/- работает
- [ ] negative balance поддерживается
- [ ] ledger сходится с current balance
- [ ] старые финансовые записи не редактируются и не удаляются

## 9. Provider cost / Margin — CRITICAL

- [ ] actual provider cost сохраняется в USDT
- [ ] immutable RUB/USDT FX snapshot сохраняется
- [ ] revenue USDT считается по snapshot
- [ ] margin USDT и margin % воспроизводимы
- [ ] historical generation не переоценивается новым курсом
- [ ] partner price ниже provider cost сохранить нельзя
- [ ] если provider cost обогнал partner price, новые gens получают нейтральный 503
- [ ] low-margin threshold работает global/model/config
- [ ] margin alert приходит один раз на incident

## 10. Crypto Bot payments — CRITICAL

- [ ] invoice создаётся из RUB amount
- [ ] минимум 1000 ₽
- [ ] TTL = 1 час
- [ ] supported crypto asset реально принимается
- [ ] payment webhook проверяется
- [ ] duplicate webhook не создаёт duplicate credit
- [ ] подтверждённый paid автоматически зачисляет исходную RUB сумму
- [ ] партнёр получает уведомление о зачислении
- [ ] admin получает alert
- [ ] резервный manual credit работает без повторного начисления
- [ ] credit action идемпотентна
- [ ] exact requested RUB зачисляется
- [ ] FX difference логируется
- [ ] >1% показывает warning
- [ ] expired-but-paid зачисляется после проверки Crypto Pay
- [ ] потерянный webhook восстанавливается фоновой сверкой
- [ ] одновременные webhook и сверка не создают double credit
- [ ] full refund протестирован
- [ ] partial refund протестирован
- [ ] refund после уже потраченного баланса может сделать balance negative без поломки ledger

## 11. Safe-to-withdraw — CRITICAL

- [ ] показывается в USDT
- [ ] использует фактический Crypto Bot USDT balance
- [ ] stale balance допускается и показывает возраст
- [ ] paid-but-not-credited obligations учитываются
- [ ] unpaid invoices не учитываются
- [ ] active generation reserves учитываются
- [ ] current required reserve учитывается
- [ ] required provider float учитывается
- [ ] recorded profit withdrawals учитываются
- [ ] результат может быть отрицательным
- [ ] negative value корректно отображается
- [ ] negative deficit Telegram alert работает
- [ ] повтор deficit alert каждые 15 минут
- [ ] manual silence работает только для текущего incident
- [ ] после recovery новый deficit создаёт новый incident
- [ ] withdrawal > safe разрешается только через hard override + reason
- [ ] profit withdrawal — accounting-only
- [ ] correction append-only, включая partial correction

## 12. FX — CRITICAL

- [ ] automatic RUB/USDT source работает
- [ ] fallback chain: current automatic -> manual fallback -> last known automatic
- [ ] manual fallback можно задать
- [ ] источник курса сохраняется в snapshot
- [ ] stale automatic rate не ломает расчёт
- [ ] старая transaction не меняется после обновления FX

## 13. Provider float / funding — CRITICAL

- [ ] upstream balance читается реально
- [ ] target float считается
- [ ] admin видит recommendation
- [ ] low float alert работает
- [ ] если upstream денег мало, task не теряется
- [ ] task ждёт восстановления float
- [ ] при восстановлении <=15 минут продолжает автоматически
- [ ] после 15 минут корректно timeout/fail
- [ ] partner reserve освобождается
- [ ] внутренние причины про нехватку float партнёру не раскрываются

## 14. Routing / Fallback — CRITICAL

- [ ] глобальный provider priority соблюдается
- [ ] система не выбирает самовольно cheaper/faster provider
- [ ] slow-but-healthy primary не обходится
- [ ] broken partner provider key исключает только этот provider для этого партнёра
- [ ] fallback допускается только при полном feature equivalence
- [ ] resolution/mode/refs никогда молча не меняются
- [ ] uncharged failed attempt корректно fallback'ится
- [ ] charged-but-no-result вызывает fallback за наш счёт
- [ ] partner платит максимум одну generation price
- [ ] extra upstream cost попадает в `provider_incident_loss`
- [ ] all fallback failed => partner charge 0

## 15. Queue / Reliability — CRITICAL

- [ ] durable queue переживает restart
- [ ] fair scheduling между партнёрами
- [ ] 20–50 parallel requests протестированы
- [ ] 429 обрабатывает `Retry-After`
- [ ] 5xx retry с exponential backoff + jitter
- [ ] provider acceptance timeout = 15 минут
- [ ] provider processing timeout работает
- [ ] circuit breaker открывается на реальной деградации
- [ ] provider исключается из routing
- [ ] recovery health checks идут по правилам
- [ ] 3 successful free health checks -> limited real traffic
- [ ] 3 successful real generations -> normal
- [ ] любой provider-side failure в recovery снова открывает breaker

## 16. Webhooks — CRITICAL

- [ ] terminal events: completed/failed/timeout/cancelled
- [ ] HMAC-SHA256 считается по точному raw body
- [ ] headers содержат timestamp/signature/event_id/delivery_id/attempt
- [ ] signature проверена независимым тестовым receiver
- [ ] retry каждые 15 минут
- [ ] retries продолжаются до 24 часов
- [ ] after 24h failure фиксируется
- [ ] admin alert при terminal delivery failure
- [ ] manual resend работает
- [ ] resend использует same event_id
- [ ] resend создаёт new delivery_id
- [ ] resend использует original snapshot URL/secret
- [ ] изменение webhook config не ломает in-flight event

## 17. Results — CRITICAL

- [ ] completed generation возвращает рабочий result URL
- [ ] `expires_at` передаётся, если upstream его даёт
- [ ] generated files у нас не сохраняются
- [ ] partner docs ясно говорят скачать результат сразу
- [ ] нигде нет ложной гарантии технического хранения 24h
- [ ] проверено, возможен ли refresh Argo result
- [ ] если refresh не доказан — кнопки «Обновить ссылку» нет

## 18. Partner cabinet

- [ ] balance
- [ ] top-up
- [ ] API keys
- [ ] history
- [ ] UUID search
- [ ] docs
- [ ] support
- [ ] delete account
- [ ] generation history фильтруется
- [ ] provider internal data не показывается
- [ ] XLSX balance reconciliation сходится
- [ ] 2 free tests на TG lifetime реально ограничены

## 19. Admin Telegram — CRITICAL

- [ ] доступ только одному admin TG ID
- [ ] critical operations требуют confirmation
- [ ] applications
- [ ] partner management
- [ ] provider keys
- [ ] catalog
- [ ] partner pricing
- [ ] margin thresholds
- [ ] payments waiting credit
- [ ] balance adjustments
- [ ] provider float
- [ ] safe-to-withdraw
- [ ] withdrawals
- [ ] support
- [ ] incidents
- [ ] test/prod switch
- [ ] emergency смена admin ID через env реально проверена

## 20. Pricing / Docs

- [ ] `/pricing` показывает актуальную partner price
- [ ] partner price одна для всех партнёров
- [ ] price change применяется только к новым generations
- [ ] confirmation показывает old -> new + margin %
- [ ] price history бессрочная
- [ ] `/balance` работает
- [ ] public `/docs` содержит самостоятельный inference reference: base URL, auth, production model IDs,
  параметры/лимиты, примеры, собственные ответы/ошибки, upload → generation → status → download
- [ ] исполняемые примеры проверены без обращения к платным сервисам; документы не обещают недоступные опции
- [ ] public `/docs` не раскрывает billing/admin/provider/webhook/reconciliation internals
- [ ] public `/openapi.json`, Swagger и ReDoc отключены
- [ ] RU/EN model-connection guide актуален
- [ ] public price актуален
- [ ] changelog актуален
- [ ] deprecated major API countdown работает

## 21. Support

- [ ] ticket statuses: open/in_progress/closed
- [ ] closed ticket нельзя reopen
- [ ] files/screenshots работают
- [ ] attachment max 20 MB
- [ ] attachments идут в РФ storage
- [ ] partner/admin notifications приходят

## 22. Security — CRITICAL

- [ ] secret scanning = green
- [ ] dependency scan = green
- [ ] API/provider/webhook secrets отсутствуют в logs
- [ ] provider keys защищены в storage
- [ ] webhook URL защищён от SSRF
- [ ] localhost/private/link-local/internal metadata URLs нельзя вызвать
- [ ] SQL injection/basic abuse tests пройдены
- [ ] auth boundaries протестированы
- [ ] Telegram user не может посмотреть чужой account
- [ ] partner A не может получить generation/balance partner B
- [ ] admin endpoints невозможно вызвать partner key
- [ ] proxy logs не содержат payload/PD

## 23. 152-ФЗ / legal review

- [ ] production data в РФ
- [ ] backups в РФ
- [ ] support files в РФ
- [ ] consent texts подготовлены
- [ ] terms подготовлены
- [ ] non-refundable balance wording проверено
- [ ] cross-border provider transfer prompts/references отражён в документах
- [ ] retention policy проверена юристом
- [ ] 24h result download wording проверено

Существенные вопросы обработки персональных данных являются launch blockers. Остальные юридические пункты могут быть `WARN` только после осознанного решения владельца.

## 24. Observability

- [ ] API latency
- [ ] generation success/fail
- [ ] provider errors
- [ ] timeout rates
- [ ] queue depth
- [ ] oldest queue age
- [ ] webhook errors
- [ ] provider float
- [ ] payment waiting count
- [ ] margin
- [ ] safe-to-withdraw
- [ ] incident loss
- [ ] correlation ID связывает API request, generation, provider attempt и webhook delivery
- [ ] logs/metrics retention 90d

## 25. CI/CD — CRITICAL

- [ ] branch protection включён
- [ ] direct code push в `main` закрыт
- [ ] PR required
- [ ] CI запускается
- [ ] unit/integration/security/smoke checks работают
- [ ] PR test environment изолирован
- [ ] immutable image SHA
- [ ] GHCR private
- [ ] prod deploy автоматизирован
- [ ] failed readiness вызывает rollback
- [ ] rollback реально протестирован
- [ ] migration rollback/compatibility strategy проверена

## 26. Финальный end-to-end smoke — CRITICAL

Пройти реальный сценарий:

- [ ] создать нового test partner
- [ ] approve
- [ ] создать API key
- [ ] оплатить invoice
- [ ] подтвердить payment
- [ ] manual credit
- [ ] проверить RUB balance
- [ ] запросить реальную generation через public API
- [ ] получить UUID
- [ ] дождаться completed
- [ ] получить webhook
- [ ] скачать result
- [ ] проверить partner charge
- [ ] проверить provider cost
- [ ] проверить margin
- [ ] проверить FX snapshot
- [ ] проверить history
- [ ] проверить XLSX
- [ ] проверить safe-to-withdraw
- [ ] повторить duplicate idempotency request
- [ ] убедиться, что второй generation не создан

## 27. Финальный GO / NO-GO

### GO

- все CRITICAL = PASS
- нет незакрытых финансовых расхождений
- restore PASS
- real generation PASS
- payment reconciliation PASS
- webhook PASS
- rollback PASS

### LIMITED GO

- только некритичные WARN
- подключаем одного партнёра
- наблюдаем production
- остальных подключаем по одному

### NO-GO

Любой баг, который может:
- потерять деньги
- сделать double charge
- сделать double credit
- потерять accepted generation
- раскрыть secrets
- раскрыть данные другого партнёра
- сломать restore
- оставить финансовый ledger несверяемым

---

## Самые важные launch blockers

Если нужно сокращать аудит, первыми проверяются:

1. деньги и ledger
2. idempotency
3. generation lifecycle
4. provider routing/fallback
5. backups/restore
6. security/isolation

Красивый Telegram UI не компенсирует провал любого из этих шести блоков.
