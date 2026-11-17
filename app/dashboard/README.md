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
