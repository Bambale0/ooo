# Lava.top: пополнение через СБП

Контракт сверяется с [официальной документацией Lava.top](https://developers.lava.top/en)
и актуальной [OpenAPI-схемой](https://gate.lava.top/docs/documentation.yaml).

## Что реализовано

При `PAYMENT_PROVIDER=lava_top` API и Telegram-кабинет создают RUB invoice через
`POST https://gate.lava.top/api/v3/invoice` со значениями
`paymentProvider=PAY2ME` и `paymentMethod=SBP`. Оффер должен быть опубликован с
опцией «Price on request via API». Покупатель указывает email и сумму от 1 000 ₽.

После webhook backend не доверяет полям события и повторно читает контракт через
`GET /api/v1/invoices/{id}`. Баланс увеличивается только для `COMPLETED`, при полном
совпадении contract UUID, валюты RUB, суммы и email. Retail ledger, cost-coverage
ledger, статус и уведомления записываются атомарно и идемпотентно.

## Настройка

```dotenv
PAYMENT_PROVIDER=lava_top
LAVA_TOP_API_KEY=<API key из Integrations → API>
LAVA_TOP_OFFER_ID=<UUID динамического оффера>
LAVA_TOP_WEBHOOK_SECRET=<случайный секрет 32–80 символов>
LAVA_TOP_BASE_URL=https://gate.lava.top
```

В Lava.top добавьте webhook:

- URL: `https://<ваш-домен>/api/v1/payments/lava-top/webhook`;
- события: `payment.success` и `payment.failed`;
- authentication: `Your service's API key`;
- значение ключа: то же, что `LAVA_TOP_WEBHOOK_SECRET`.

Опционально задаются HTTPS-адреса возврата:

```dotenv
LAVA_TOP_SUCCESSFUL_RETURN_URL=https://example.org/payment/success
LAVA_TOP_FAILURE_RETURN_URL=https://example.org/payment/failure
LAVA_TOP_CANCEL_RETURN_URL=https://example.org/payment/cancel
```

Параметр `status` в return URL никогда не подтверждает оплату. Источники истины —
проверенный webhook и ответ `GET /api/v1/invoices/{id}`.

## Надёжность и эксплуатация

- Одинаковый partner idempotency key возвращает существующий локальный счёт.
- Lava.top не принимает merchant idempotency key. Если результат create неизвестен,
  счёт переходит в `creation_unknown`; POST автоматически не повторяется.
- Известные contract UUID фоновая сверка проверяет автоматически. Потерянный webhook
  не мешает зачислению.
- Для `creation_unknown` без UUID администратор использует
  `POST /api/v1/payments/invoices/{payment_id}/reconcile` с `provider_invoice_id`
  (UUID контракта) и причиной.
- Повторные webhook, повторная сверка и ручной credit не создают второе зачисление.
- Неизвестные события, включая refund/chargeback envelope, подтверждаются ответом 2xx.
  Фактический возврат по-прежнему проводится отдельным подтверждённым refund-действием.
- API отмены one-time invoice в этом контуре не используется; пользователь может
  создать новый счёт с новым idempotency key.

Перед выкладкой примените миграцию `20261026_0033`, настройте webhook, выполните
тестовую оплату минимальной суммы и проверьте один retail и один coverage ledger entry.
Для отката создания новых счетов переключите `PAYMENT_PROVIDER=crypto_pay`; additive
миграцию откатывать сразу не требуется, а уже созданные Lava.top счета продолжит
подбирать фоновая сверка при сохранённых credentials.
