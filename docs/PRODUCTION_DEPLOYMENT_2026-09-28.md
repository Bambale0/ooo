# Production deployment — 2026-09-28

The owner requested deployment into the existing production stack, without a
separate preproduction environment or synthetic customer/payment records. Manual
business-flow testing is performed by the owner.

## Deployed release

- Repository: `Bambale0/ooo`, merged PR #46.
- Runtime revision: `57a38650b8402ceb174c6a8c6d997f5ecf99325d`.
- Image: `neironych:57a38650b8402ceb174c6a8c6d997f5ecf99325d`.
- Existing host directory `/opt/neironych`, Compose project `ooo`, API loopback
  port `18000`, production PostgreSQL and Redis volumes retained.
- API: `https://api.xn--e1aikcel5c5a.online`.
- Telegram: `@creativetstbot1_bot`, single polling process, designated owner as admin.
- `APP_ENV=production`; production configuration validation passed.
- API, generation worker, webhook worker and Telegram use the same release.
- Host Compose override and existing encryption/database/admin secrets retained.
  DB pools limited to 3 connections plus 2 overflow per process for the existing
  50-connection PostgreSQL limit.

The temporary `ooo-preprod` stack and its volumes were removed. Its HTTPS route,
systemd unit and network rules were removed. PR #47 was closed without merging.
No preproduction prices, data or credentials were copied into production.

## Real integration settings

- Crypto Pay mainnet key authenticated successfully against `https://pay.crypt.bot`.
- Actual available wallet balance at deployment: **0 USDT**.
- Automatic FX probe: **84.4825649 RUB/USDT**, a point-in-time observation.
  Manual fallback is `84.52`; future automatic rates can change.
- Existing published documents are linked from registration:
  [business/API terms](https://xn--e1aikcel5c5a.online/business-terms) and
  [privacy policy](https://xn--e1aikcel5c5a.online/privacy), version `2026-09-24`.
- The server's DNS-selected Telegram address timed out. A container-only hosts
  override selects `149.154.167.220`, with normal TLS validation. The first bot
  startup hit a network timeout; Docker restarted it and polling started normally.
  `getMe`, registered `/start` and `/cancel` commands, and absent webhook were verified.
  This address is an operational dependency; recheck it if Telegram connectivity changes.
- ArgoLink uses encrypted credentials attached to each real partner/application.
  No shared test credential or fake customer was installed into production.

## Catalog and business state

Seedance 2.5 is provisioned as a real **draft catalog model**, with integration,
documentation and completed reference-smoke evidence. Routes for the verified
480p and 720p configurations are available for administration. No retail price was
invented and no model was enabled for sales. The owner sets prices and enables
the model in the admin cabinet. Previously measured procurement costs are
`0.078 USD/s` and `0.17 USD/s`; these are not retail prices.

At deployment verification, production had **0 partners, 0 applications,
0 generations and 0 invoices**. No balances, payments, consent records or
customer keys were fabricated. Wallet/FX observations are genuine provider data.
New accounts and payment/generation flows remain for the owner's manual use.

## Verification evidence

- Main-commit CI: [run 36423568441](https://github.com/Bambale0/ooo/actions/runs/36423568441),
  **288 tests passed**, including PostgreSQL integration cases.
- CI also passed lint, dependency audit, secret scan, SAST, encrypted backup/restore,
  WAL recovery, Nginx validation and production image build.
- Production migrations at `20260923_0020`; `alembic check` found no new operations.
- HTTPS readiness returned the exact deployed SHA and healthy DB/Redis.
- Public `/docs` and `/prices` returned 200; `/internal/metrics`, `/openapi.json`
  and the removed `/preprod/api/v1/readiness` returned 404.
- Missing/invalid API credentials returned 401, including a generation request
  rejected before any paid submission.
- Real treasury state reported a fresh zero wallet and zero available withdrawal.
- Polling startup was observed in application logs. API/worker processes were running;
  systemd boot activation and Docker `unless-stopped` restart policies are configured.

## Backup and operation

The scheduled encrypted backup was failing because it required a deleted legacy
Telegram systemd unit. That absent optional unit is now included only when present;
mandatory data/configuration paths remain required. The actual backup service
completed successfully after the fix.

Encrypted snapshots were taken before and after deployment and copied off-server.
The pre-deployment copy passed checksum verification, age decryption and a full
restore into a disposable PostgreSQL 16 instance **outside production**: schema
version `20260923_0020`, 36 tables, original zero partner/generation counts. The
disposable instance was removed. The recovery identity remains off-server.

Use `/opt/neironych/compose.sh` for runtime operations. Host overrides, `.env`,
the prior revision and backup script were preserved under a private rollback
directory before changes. Never run `down -v` on the production stack. Before
rollback, account for any real records created after deployment; do not overwrite
current customer state with an old dump or downgrade the schema blindly.

## Remaining operator actions and boundaries

- Register the actual customer, bind its personal provider credential, approve it,
  set retail prices and enable the desired verified model configurations.
- Fund and pay actual invoices as appropriate. No payment or generation was invoked
  during this deployment; real business end-to-end completion is not claimed.
- Upstream quota usage can be inspected. Automatic provider-side quota mutation
  is not implemented/verified by this deployment; an unrestricted key does not prove
  limited-quota behavior. Earlier provider reference tests remain separately documented.
- Daily encrypted backup is enabled, but continuous automated off-host replication
  and production WAL/PITR still retain their separate launch-checklist gates.

Skills used: `devops`, `bot-tester`, project-local `systematic-debugging` and
`verification-before-completion`.
