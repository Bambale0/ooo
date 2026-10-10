# Moscow time at the Telegram presentation boundary

Baseline: 8c64a6d. User requested Moscow time after reviewing a reserve and its
usage adjustment. Current Telegram screens format raw UTC without a zone label.
The admin usage report already uses Europe/Moscow. Public API and logs stay UTC.

Scope: common UI datetime formatting, partner history, admin partner screens,
threshold/price histories, and the final-only partner transaction projection.
No database/timezone setting changes, migration, tariff or accounting mutation.

Acceptance: naive DB dates mean UTC; aware values convert exactly once; day/year
boundaries work; timezone label is visible; ledger amounts/order/UUIDs/ownership
stay unchanged. Existing Telegram flows and financial tests must stay green.

Steps: baseline tests passed (54); added behavioral regressions before the fix.
Guidance: pinned project TDD and systematic-debugging skills.

## Revised owner scope before release
Hide internal reserve/release/usage adjustment and zero rows from partner history.
Present one final net debit per generation; retain topups, manual adjustments and
real payment refunds. Keep full admin ledger. Partner UUID search must not present
an active reserve as a final charge. Available balance and admission remain intact.
Projection, tenant filtering and pagination happen in SQL before the page limit.
No ledger writes, historical repricing, API contract changes or job resubmissions.

## Verification before PR
- Original timezone regressions: 10 expected failures and 2 existing valid placeholders.
- Final-only history regressions: 12 expected failures and 3 existing valid cases.
- Updated the prior UUID visibility test to retain the full-UUID assertion in the
  admin ledger, while asserting a single generation UUID and final debit for the partner.
  No integrity, ownership or accounting assertion was removed to mask a defect.
- Focused cabinet/history suite: 81 passed. Full suite: 1130 passed, 12 optional
  infrastructure tests skipped locally, including the added PostgreSQL query test.
- Full Ruff and git diff --check passed. New PostgreSQL projection regression
  uses a rollback-only outer transaction and runs under the normal CI database.
- Admin report already uses Moscow time; all other Telegram datetime rendering
  now shares format_msk. Public API timestamps, logs, clocks and scheduling stay UTC.
- Admin ledger retains all raw movements plus full ledger/generation UUIDs.

## Release boundary
Use ordinary PR -> green CI -> merge -> exact-revision automatic production deploy.
No deployment to APIX, no payment/provider calls, no historical replay or price change.
