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
  720p, 1080p and 4k are accepted. Other five planned requests were accepted and
  are being polled; acceptance is not completion. Known 2.5 480p rejection remains.
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
4. [ ] Run local checks and independent review; submit PR and verify CI for its SHA.
5. [ ] Verify actual smoke/price prerequisites, release and enable through admin
   operations, then check live availability and pricing.

## Verification and follow-up

Implementation and verification are in progress. Production database and release
are unchanged so far. Bounded provider smoke has started; no successful generation,
deployment or enablement is claimed before its evidence is available.

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
