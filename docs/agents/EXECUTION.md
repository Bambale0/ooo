# Seedance self-developed models — execution ledger

Started 2026-10-01. Baseline: `8f420a6c2f1da7128595e4d615f372892c28078c`.
Branch: `codex/seedance-self-developed`.

## Outcome and acceptance

The owner requested `seedance-2.0-self-developed-nsfw` and
`seedance-2.5-self-developed-nsfw`, and explicitly confirmed enabling both for all
partners. Both must have reviewed contracts, documented limits, database-managed
prices/capabilities and successful smoke evidence before public enablement.
Public model and pricing endpoints must reflect the enabled database state.

## Preflight

- Existing FastAPI/SQLAlchemy catalog exposes authenticated model creation, price,
  capability, gate and enable operations. Runtime configuration stays in the DB.
- 2.5 already has a reviewed contract and optional restricted per-partner access.
  2.0 self-developed is absent from the reviewed catalog; adapter family support exists.
- The provider catalog advertises 2.5 480p, but the repository records an actual
  provider rejection. Do not erase that evidence or expose a known rejected tier.
- Reuse the provider adapter, validation, billing snapshots and existing admin API.
  No schema migration, new secret, or new environment setting is planned.
- Preserve other models, prices, partner grants and historical financial records.
- `context.md` is a historical development context; this file tracks this task.
- Production revision, existing tariffs and smoke evidence are being inspected
  read-only. Live credentials must stay on the host and out of output/Git.
- Public model pages and catalog were checked on 2026-10-01. Generic provider
  guide disagrees with model-specific pages on some limits; concrete API failures
  take precedence over advertising. Live checks will use benign prompts only.
  The targeted live catalog revision was `64d3bfe6eb8e6878`; unrelated catalog
  entries have not been represented as freshly reviewed.
- Read-only production inspection confirmed baseline `8f420a6`, migration
  `20260930_0022`, missing 2.0 variant and restricted 2.5 variant with smoke=false.
  The existing 2.5 variant has 720p/1080p prices of 21.80/55.13 RUB per second.
  Reuse ordinary 2.0's 720p/1080p prices, 14.11/35.90 RUB per second, after checks.
- Limited smoke uses only the configured owner's credential, selected by the
  configured admin Telegram identity. Six requests have a conservative total cap
  of USD 6.50; submitted requests are persisted privately and are never resubmitted.
- Live 2.0 variant 480p was rejected with HTTP 400 (`request_unsupported`): only
  720p, 1080p and 4k are accepted. Other five planned requests were accepted but
  all finished with `capacity_unavailable`; no video/content was produced.
  Observed provider usage delta was USD 0. No submit was repeated. Known 2.5
  480p rejection remains. Public enablement is blocked by the failed smoke gate.
- The owner was asked to set the missing 2.0 4k price (proposed 74.36 RUB/second)
  or to keep 4k unavailable initially. That tariff decision is pending.

## Guidance inspected

- Bambale0/claw @10e261e: `.agents/skills/backend-integration/SKILL.md`,
  `QA_AUDIT_CHECKLIST.md`: preserve adapters; verify provider contracts and lifecycle.
- wondelai/skills @c172996: `clean-code/SKILL.md`,
  `clean-code/references/testing-principles.md`: minimal changes and behavior tests.
- Bambale0/dev-agents-pack @9ae0af4: API integrator, database and Python backend
  checklists: explicit contracts, idempotent configuration and financial safety.
- agentskills/agentskills @69ef37e: searched; skill-format documentation has no
  applicable model integration skill.
- anthropics/skills @8a1541c: `skills/webapp-testing/SKILL.md` inspected for UI
  verification; inspect scripts first per the user's higher-priority instructions.
- Bambale0/ksu @68baa83: TDD, verification-before-completion and requesting-code-review.
- Local `.agents/skills/` submodule @e4a2e49 and workspace mirror searched;
  TDD and verification-before-completion read and applied.

## Steps and evidence

1. [x] Inspect project instructions, product enable gates, contracts, catalog/admin
   paths, tests and CI; verify official model IDs and procurement tiers.
2. [x] Confirm public availability scope with the owner.
3. [x] Add missing contract and behavioral tests; document both public variants.
4. [x] Run local checks and independent review; submit PR and verify CI for its SHA.
5. [ ] Verify actual smoke/price prerequisites, release and enable through admin
   operations, then check live availability and pricing.

## Verification and follow-up

Implementation and automated code verification are complete. [PR #65](https://github.com/Bambale0/ooo/pull/65)
CI for `eec66274f213574d75c0311288eb04df86b97e2a` passed: **676 tests**, PostgreSQL
migrations/schema comparison, dependency audit, SAST, secret scan, backup/restore,
PITR, Nginx validation and image build. Runtime release/catalog provisioning is
being prepared; public enablement remains blocked by actual upstream capacity
failures. Do not mark `has_successful_smoke` true without a successful live run.

Local environment setup: Python 3.14 virtual environment on drive D. The Linux
lock file contains uvloop and omits Windows-only colorama; a temporary ignored copy
skips uvloop and separately installs colorama 0.4.6. Repository dependency files
remain unchanged. The initial pip attempt hit insufficient C-drive temporary
space; subsequent temporary/cache paths are scoped to the project's ignored venv.

Local full suite: 665 passed, 10 skipped, one Windows-only failure at
`test_partner_reference_example.py:144`: the pre-existing example test expects
POSIX mode 0600 but Windows reports 0666. This is unrelated to the model changes;
do not weaken that security assertion. PostgreSQL/Unix verification is left to CI.
Ruff, compileall and pip check passed. The independent contract review found no
actionable issue. Public-surface review caught unconditional documentation rows
before enablement; this was fixed with a DB-driven production filter. The new
pre-enable slug assertion failed first, then passed after the correction.

Final focused local verification: 228 passed across contract registry, normalized
video validation, public documentation/enablement, restricted model access,
provider adapter, native inference and partner generation flows. Ruff and
`git diff --check` also passed. No schema migration is required.

Release preparation uses local `deployment-procedures`: immutable image, preserved
previous release and existing host-specific configuration, readiness verification
and rollback on failure. The first isolated Docker build could not resolve PyPI
on its default build network; only that build container was stopped, and the build
was retried with host networking. Existing application containers were unchanged.

## Live matrix (2026-10-01)

| Model | Resolution | Input | Result |
| --- | --- | --- | --- |
| 2.0 self-developed | 480p | Neutral text, 4 seconds | HTTP 400, unsupported resolution |
| 2.0 self-developed | 720p | Synthetic image reference, 4 seconds | `capacity_unavailable` |
| 2.0 self-developed | 1080p | Neutral text, 4 seconds | `capacity_unavailable` |
| 2.0 self-developed | 4k | Neutral text, 4 seconds | `capacity_unavailable` |
| 2.5 self-developed | 720p | Neutral text, 4 seconds | `capacity_unavailable` |
| 2.5 self-developed | 1080p | Synthetic first frame, 4 seconds | `capacity_unavailable` |

Follow-up: a successful bounded live test is required before setting smoke gates
and invoking public enable. The pending 4k tariff is a separate operator decision.
There is no evidence of a working NSFW render in this task; unit/CI success does
not override this provider failure.


# Admin partner management — 2026-10-02

Baseline: `1cfd6ae31d7c4af2c26cfb94fbe9b07bd12d96a6` (`main`).
Branch: `feat/admin-partner-management`.

## User outcome and acceptance criteria

The Telegram admin cabinet must provide one convenient partner-management entrypoint
without creating a second partner source of truth. An administrator can browse and
search partners, open a partner card, inspect the operational data already owned by
the existing tables, and start existing confirmed mutations from that card.

Acceptance criteria:
- partner list is paginated and searchable by Telegram ID, @username, UUID, company,
  or project;
- partner card shows identity, status, balance, cost coverage, application and counts;
- drill-down views expose API keys, provider credentials, payments, generations,
  balance ledger and restricted-model grants;
- secret hashes/encrypted credentials are never rendered;
- balance/status/Telegram mutations continue to use the existing confirmation paths;
- non-admin callbacks disclose no partner data;
- existing adjustment-picker search behavior remains backward compatible.

## Architecture / security / migration impact

- Reuse `partners` and the existing account, billing, payment, generation, provider,
  catalog and Telegram tables; no migration and no new source of truth.
- All new callbacks retain the existing `admin_` authorization gate and additionally
  re-check admin access inside the partner module.
- Dangerous mutations are not duplicated: the partner card links to the existing
  confirmed adjustment, status and Telegram-transfer flows.
- Key views render only label/prefix/status metadata. Hashes and encrypted provider
  secrets stay server-side.
- No mutable business parameter is hardcoded and no pricing/routing behavior changes.

## Skills applied

- `python-fastapi-development`: preserve async SQLAlchemy/FastAPI project patterns.
- `python-testing-patterns`: add focused pytest regression/integration coverage.
- `telegram-bot-builder`: keep navigation as compact inline-keyboard flows.
- `verification-before-completion`: no completion claim before fresh CI evidence.

## Steps

1. [x] Audit repository instructions, product/runtime status, current partner/admin flows.
2. [x] Add admin partner browser, search, partner card and read-only drill-down views.
3. [x] Wire the browser into the admin menu and reuse confirmed mutation flows.
4. [x] Add regression tests for browsing/search, secret non-disclosure and authorization.
5. [ ] Run CI for the exact branch SHA; fix failures before completion.
6. [ ] Review diff/requirements and open PR with verification evidence.



# FX policy + inference rejection diagnostics — 2026-10-02

Baseline: `a919f0a9699ca9a6d66ea403c60b4807a5d8c355` (`main`).
Branch: `fix/fx-policy-and-inference-diagnostics`.

## User outcome and acceptance criteria

1. Crypto Pay exchange-rate lookup must happen only while a new invoice is being
   created. Ordinary generation pricing, catalog reads, admin views and payment
   credit/reconciliation must not call the external exchange-rate endpoint.
2. Admin must be able to switch FX policy between automatic and manual mode and
   set the manual RUB/USDT rate durably. Automatic mode refreshes the rate at
   invoice creation and persists that snapshot; manual mode never calls the
   exchange-rate endpoint.
3. Each invoice must keep the FX snapshot captured for its creation so later credit
   and coverage accounting use the same rate instead of fetching a new one.
4. Native inference rejections must be queryable by safe identifiers
   `partner_id` and `api_key_id`, with a stable failure stage and public error
   code that distinguishes request validation, balance, provider-capital/pricing
   admission and upstream provider rejection.
5. No API key values, prompts, reference URLs, provider response bodies or other
   secrets may enter diagnostics.

## Evidence / root cause

- `current_fx()` currently invokes Crypto Pay `getExchangeRates` whenever the
  latest automatic snapshot is older than 60 seconds. It is called by generation
  admission and catalog paths, so normal inference traffic can refresh external FX.
- Telegram admin supports only a manual *fallback*; automatic mode always has
  priority and cannot be disabled.
- Payment credit calls `current_fx()` again, so the coverage rate is not pinned to
  invoice creation.
- Pre-generation failures (validation, reserve/admission) do not currently emit a
  structured event containing both partner/API-key identity and failure stage.
  Provider diagnostics start only after a generation/attempt already exists.

## Architecture / migration / safety

- Extend the existing append-only `fx_fallback_settings` history with an
  `automatic_enabled` flag instead of creating a second configuration source.
- Add nullable `payment_invoices.fx_snapshot` for immutable per-invoice provenance.
  Legacy invoices without a snapshot use the current persisted/configured rate once
  at credit time, without network I/O, and persist that fallback snapshot.
- Existing ledger entries remain append-only. No historical financial row is rewritten.
- Diagnostics use UUIDs/enums/counts only; raw request bodies and secrets are excluded.
- No paid production request is required for verification.

## Skills applied

- `systematic-debugging`: evidence first; separate balance, provider and validation paths.
- `python-fastapi-development`: async SQLAlchemy/FastAPI patterns and migration safety.
- `python-testing-patterns`: focused regression tests for FX and diagnostics.
- `observability-and-instrumentation`: stable structured events with safe correlation fields.
- `verification-before-completion`: exact-SHA CI and production smoke before completion claims.

## Steps

1. [x] Inspect production evidence and current FX/inference paths.
2. [x] Add regression tests for invoice-scoped FX and rejection tracing.
3. [x] Implement FX policy, invoice snapshots, admin controls and migration.
4. [x] Implement safe rejection telemetry and provider correlation.
5. [x] Run focused/full CI through migration/schema/test/security/build gates.
6. [ ] Review diff, merge through PR and deploy exact verified SHA.
7. [ ] Verify production readiness and read-only diagnostics after deployment.


## Verification before final merge

- Product brief sections 31–32 and implementation FX policy were updated to the
  explicit 2026-10-02 decision: external exchange-rate lookup only at invoice
  creation; manual override persists until explicitly disabled.
- Initial test-first PR run exposed test/lint issues before implementation was
  complete; no completion claim was made from that run.
- CI run #221 for code SHA `aa133a88d1a7de0b6794f49b1d43765c57e656f6`
  passed all gates before the documentation alignment commit:
  - Ruff, dependency audit, SAST and secret scan: PASS.
  - Alembic upgrade and ORM schema comparison: PASS.
  - Pytest: **719 passed in 49.98s**.
  - Encrypted backup/restore and WAL PITR: PASS.
  - Production Nginx validation and production image build: PASS.
- A final exact-SHA CI run is required after these documentation/spec commits
  before the PR can be marked ready and merged.


# Production autodeploy authentication — 2026-10-03

Baseline: `959f29187a72d6d53d53150bcb7b35281849be72` (`main`).
Branch: `fix/production-autodeploy-auth`.

## Intended outcome and acceptance

- A successful push CI on the current `main` SHA starts the protected production
  deployment only while `PRODUCTION_DEPLOY_ENABLED=true`.
- The release uses the exact CI SHA, authenticates to the private GHCR package for
  the duration of this run, verifies all four application processes, and leaves no
  long-lived registry credential on the host.
- Production SSH remains pinned to the host key; release failures stop before a
  switch or use the existing rollback path. A new release is not claimed from CI
  or API revision alone.

## Baseline evidence and risks

- GitHub CI run `37067486951` attempt 2 passed for the baseline SHA. Deploy run
  `37070605960` reached SSH preflight but failed host-key verification; subsequent
  retry was skipped after the arming variable changed externally to `false`.
- The API, `/opt/neironych/REVISION`, and four running `ooo` containers report the
  baseline SHA. This is an existing manual release, not proof of Actions deploy.
- The host has no `deploy` user and `/opt/neironych` is root-owned. An anonymous
  GHCR manifest request is unauthorized; the current workflow cannot pull a new
  private image on that host without per-run authentication.
- Production is an existing-host Compose installation. Its code-only release path
  refuses pending migrations, preserves the database/proxy, and verifies rollback.
  SSH account configuration and the external change to the arming variable must be
  resolved before release activation.
- No database schema, business configuration, API contract, or provider routing
  change is intended. The GitHub Actions token is a short-lived credential, kept
  outside the image and application settings.

## Plan and verification seams

1. [x] Inspect repository release instructions, exact-head CI, workflow, host
   topology, host key, running revision, registry access, and rollback boundary.
2. [x] Add temporary, private GHCR authentication for the SSH release; clean it
   on success and failure. Preserve the current exact-SHA and provenance gates.
3. [x] Run workflow syntax, focused deployment tests, and PR CI for the exact SHA.
4. [ ] Resolve the SSH execution identity and the externally disabled arming gate;
   enable the gate only when authorized state is clear.
5. [ ] Merge through PR, verify push CI and the production job, then verify the
   host revision, four containers, API readiness, and credential cleanup.

Observability: use CI/deploy run IDs, commit SHA, safe deploy progress markers,
container revision/image identities, and readiness. Never print SSH keys, registry
tokens, Docker auth files, host `.env`, or private payloads.

Preparation evidence: a dedicated GitHub Actions SSH identity reaches the host as
`root` on port 2022; this host has no `deploy` user. Its private key is held only
locally outside Git and in the production environment secret; the laptop's existing
SSH key remains separate. Repository `DEPLOY_SSH_USER=root`, `DEPLOY_USE_SUDO=false`, and
`DEPLOY_SSH_PORT=2022` now match that existing access. The arming variable remains
`false` after an external change, so no deployment attempt is authorized until
its owner clarifies the change. The new GHCR auth flow remains a branch change
pending PR review, CI, and an exact-SHA release run.

Local verification: workflow YAML loaded; 11 local/remote Bash blocks passed
`bash -n`; deployment-focused pytest run passed `10/10` with `--noconftest`.
The laptop's Python environment lacks Pillow, so the full suite will run in
GitHub CI after PR creation. `git diff --check` passed.

PR #81 CI run `37072752588` passed for code SHA `a6065a42ad5d1850a4bebe01e8ad2fe719be924d`: Ruff, dependency/security scans,
migrations/schema, 750 pytest tests, backup/restore, PITR, Nginx validation,
and production image build. A final exact-SHA CI run is required after this
ledger update before merge.

Review correction: cleanup now uses an unconditional `always()` step and checks
`DEPLOY_AUTH_DIR` in the runner shell. `GITHUB_ENV` makes the path available to
subsequent shell steps, while the Actions `env` expression context is limited to
workflow/job/step declarations. The prior expression could skip cleanup.


# Durable individual partner price (2026-10-10)

Baseline main/live: `9cb809057aded8aff4b18a61974f41fa5daa146c`.
Branch: `fix/persistent-partner-pricing-20261010`.

- User outcome: APIX bot Seedance 2.5 default 720p has negotiated 23.00 RUB/sec
  independently of global 23.80 RUB/sec; no retroactive generation/ledger repricing.
- Production preflight: the sole non-global snapshot among 300 entries is APIX
  (23.00 vs 23.80); five other partners' rate remains 23.80. Existing production
  migration is 20261003_0027. Production API/worker run 9cb809057.
- Root cause: `publish_global_partner_price` overwrites *every* snapshot.
  Comparing numeric prices is insufficient: global may temporarily equal a custom rate.
- Decision: an explicit `is_custom` database flag, migration preserving all
  existing price values, audited admin-only partner override, lock ordering
  template -> snapshot, margin check, and unchanged accepted generation snapshots.
- Regression: custom rate survives repeated global publication including a
  momentary equal price; other partners follow the global price; unauthorized,
  below-cost and excess-precision writes fail closed; duplicate writes are
  idempotent. Migration backfill selects by data, not hardcoded partner identifiers.
- Rollout: backup and apply 0028 migration to existing host before new code
  (host autodedeploy blocks pending migrations), verify baseline row counts,
  run CI and code-only immutable release, then confirm live revision and prices.
- Risk: old code will not respect the flag if rolled back; disallow global pricing
  publications on old revisions after the migration. Schema downgrade refuses
  while any custom rate exists.
- Verification status: pending PR CI / migration / production release.
