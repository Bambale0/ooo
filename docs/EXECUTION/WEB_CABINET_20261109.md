# Web cabinet — 2026-11-09

## Target and baseline

Requested: a full Russian-language responsive personal cabinet for API.Нейроныч with a dashboard, billing and API operations.

Baseline: main 0f69d2a99a50aa94498afa74c95b96f03ce93e47; branch feat/partner-web-cabinet-20261109. Source of truth: PRODUCT_BRIEF_INTERVIEW_SNAPSHOT.md, IMPLEMENTATION_EPICS.md, docs/CABINET_AND_RECOVERY.md, docs/PRODUCTION_REVIEW.md, AGENTS.md. Current server uses FastAPI/SQLAlchemy, PostgreSQL money, Telegram-linked partner identity and existing immutable ledger, model prices, generation records and invoice workflow. There was no independent browser session/cabinet.

## Architecture decision

- Reuse partner accounts, identity, balance, final generation history, invoices and model catalog.
- Use Telegram Login Widget signed identity -> partner telegram_id -> revocable, HTTP-only, same-site 24h browser cookie. Store only SHA-256 token digest, never API key in browser session.
- New routes /cabinet, /cabinet/login, /cabinet/auth/telegram, /cabinet/logout, /cabinet/{section}. All tenant views join authenticated partner_id on the server; no partner ID in user-controlled routes.
- SQL summaries reflect real usage: 30-day statuses and final ledger charges, 14-day chart. No invented usage metrics or upstream costs.
- Show masked API keys only. Existing Telegram cabinet retains key creation/revocation and payment creation; no second payment/ledger implementation is introduced.
- Fixed login URL/domain must be configured for the production Telegram bot through BotFather. The bot username and secret remain environment-controlled, not hardcoded.
- Do not enable admin access in the partner UI; admin settings remain separate.
- New migration 20261109_0043 adds revocable browser sessions; no customer balance/ledger mutation.
- UI style direction: restrained dark operational console; mobile horizontal navigation, real tabular data, accessible headings/focus and no placeholder demo data.

## Implementation / progress

1. [x] Audit existing accounts, authentication, billing, catalog, generations, payments, migration and CI.
2. [x] Read pinned .agents skills: fastapi-pro, frontend-design, test-driven-development and api-security-best-practices.
3. [x] Add server-side session model, HMAC Telegram authentication verifier and bounded login parsing.
4. [x] Add pages: overview, generations, operations, invoices, keys, models, integration and settings.
5. [x] Add responsive cabinet CSS and Telegram widget client callback; package web assets.
6. [x] Add Alembic migration and register server routes.
7. [x] Add synthetic auth/isolation/revocation and asset boundary tests.
8. [ ] Validate exact branch with Ruff, pytest, Alembic upgrade/check, security scan, browser interaction; record concrete results before release.
9. [ ] Complete business operations in web (web key create/revoke, checkout, per-key analytics, logs, payment history pagination, support tickets, admin panel) behind authenticated CSRF-safe endpoints, after current foundational release is verified.

## Security, finance, observability and rollout

- Preserve per-partner boundaries even for admin/disabled sessions; no admin role is inferred from browser cookie.
- Token and bot credential must never appear in logs, client JavaScript or HTML. Login data processed by POST and not query string. Disable caching of private pages. Sessions expire after 24h and can be revoked on logout.
- Existing ledger retains financial truth; daily aggregation uses finalized charges, not reserves.
- Critical follow-up: add operational event/trace for login success/failure with pseudonymous partner IDs and no sensitive payloads.
- CI does not constitute a production/browser check; keep PR draft and do not merge until migrations, browser/mobile, BotFather origin binding, login, tenant isolation and payment handoff are tested in staging. Confirm production TLS, CSP behavior and application domain before deployment.
- No live production tests, payments, bot updates, migrations or deploy performed from GitHub connector session.
