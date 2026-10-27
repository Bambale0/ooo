# Partner-owned balance read (2026-10-26)

## Purpose and confirmed gap

KSU requires a read-only RUB balance check for its **own** Neironych partner API key so its operator can receive a low-funds Telegram alert at 1000 RUB or below. Currently `GET /api/v1/billing/partners/{partner_id}/balance` requires the Neironych **admin** API token, which must never be copied into KSU. The public API guide only documents generation methods, not a partner-facing balance method.

## Solution and boundaries

- Add **GET /v1/balance**, protected by existing `get_current_partner` dependency. Accept exactly the existing partner API key via Bearer or x-api-key; its hashed key is validated for active key and active partner with current tenant isolation.
- Return the same `BalanceRead` schema already used for admin reads: `balance_rub`, `credit_limit_rub`, `available_balance_rub`, `partner_id`; all RUB amounts are Decimal/NUMERIC, not float. Set `Cache-Control: no-store` to protect financial data.
- Do **not** allow an arbitrary partner ID; no operator/admin credentials are accepted in place of partner API key. No financial ledger writes, API bill, provider calls or data migrations.
- Acceptance via ASGI endpoint: owner A reads A balance and approved credit exactly, B can only read B; no key, revoked key, inactive partner and standalone admin API token fail; existing admin endpoint remains unchanged.
- Observability: existing access metrics and API error categories, never emit API key/balance in logs. Verify through automated HTTP endpoint tests.
- Rollback: revert this route; existing admin/cabinet/billing unaffected.

## Process

Repository `AGENTS.md`, `PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md`, and existing `app.billing.router` / `app.api.dependencies` have priority. Apply the user's `Bambale0/.agents` `api-integration`, `python-testing-patterns`, `observability-and-instrumentation` as narrow procedural guides. No scripts from skills executed. This PR is a prerequisite for KSU partner-key monitoring and does not deploy KSU.

## Review resolution

- P2: The authoritative product brief specifies public `GET /v1/balance`; the partner-key-only route now mounts under `/v1`, not the admin billing namespace. KSU consumer updated to use this exact public path.
- P2: Both RU/EN public docs now list the endpoint, partner-key authentication and exact Decimal balance fields, with tests covering both language renderings.
