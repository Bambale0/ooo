# Partner Dashboard API

Полноценный личный кабинет партнёра согласно лучшим практикам API-провайдеров.

## Endpoints

### Profile & Settings
- GET /v1/dashboard/profile
- PATCH /v1/dashboard/profile

### Balance & Billing
- GET /v1/dashboard/balance
- GET /v1/dashboard/ledger
- GET /v1/dashboard/usage-summary

### API Keys Management  
- GET /v1/dashboard/api-keys
- POST /v1/dashboard/api-keys
- PATCH /v1/dashboard/api-keys/{key_id}/webhook
- DELETE /v1/dashboard/api-keys/{key_id}

### Generations
- GET /v1/dashboard/generations

### Analytics
- GET /v1/dashboard/analytics

### Invoices
- GET /v1/dashboard/invoices

## Партнёрская программа в веб-версии

- `GET /partners` — публичные условия программы, FAQ и переход в личный кабинет.
- `GET /cabinet?section=referrals` — раздел для активного авторизованного аккаунта. При первом открытии используется существующий идемпотентный сервис создания реферального профиля; ссылка ведёт в Telegram-бота с тем же кодом, что и в Telegram-кабинете.
- Ставки берутся из `app.referrals.service`: API-пополнения и полностью оплаченные заказы на разработку. Привязка first-touch не меняется.
- Приглашения и сумма начислений берутся из referral_attributions / referral_commissions текущего аккаунта; журнал включает отрицательные корректировки. История не раскрывает идентификаторы привлечённых пользователей или внутренние идентификаторы операций.
- Начисления не являются API-балансом или подтверждённой суммой к выводу. Самостоятельные выплаты не добавлены, поскольку существующий реферальный контур их не предусматривает.
- Раздел использует действующую сессию кабинета и private/no-store; публичная страница индексируется, но не публикуется на отдельном docs-хосте.
