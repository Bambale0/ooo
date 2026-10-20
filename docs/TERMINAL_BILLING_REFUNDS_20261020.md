# Terminal billing and procurement refund repair (2026-10-20)

## Incident and root cause

A positive release of unused procurement coverage called the admission-style
nonnegative-balance guard. An already-negative internal coverage account therefore
rejected the refund with a local 503 and rolled back a known terminal provider
failure. Repeated polls retried the same accounting failure until the processing
timeout took a different path. Customer cash was not debited in the incident.

The video service also left final failures/cancellations at billing_status=pending.
A direct usage settlement could subsequently debit a failed generation. Provider
cost exceeding an estimate could reopen the customer task for reconciliation.

## Narrow repair

- Permit this positive reservation refund even when coverage remains negative.
  New-spend admission and all amount/idempotency checks are unchanged.
- Commit final failed/cancelled billing_status=not_chargeable and a stable
  client_finalized_at with existing refunds and terminal outbox creation.
- Keep provider reported/unknown costs on attempts; do not reopen a definitive
  failed customer task solely because procurement needs review.
- Preserve provisional processing timeout/late-success semantics and all accepted
  price snapshots. No schema, provider route, price, or historical ledger rewrite.

## Verification

Test-first reproduction: seven new cases failed against production source,
including local 503, missing finality, and a direct late debit which did not raise.
After the patch: targeted suite 48 passed / 4 PostgreSQL cases skipped locally.
Expanded isolated suite with APP_ENV=test: 1523 passed / 42 infrastructure skips.
The new PostgreSQL racing-worker regression runs in normal CI with the real PG16
service. Production-image APP_ENV=production was not used for isolated health tests.
External HTTP is blocked by the existing autouse fixture; no paid work is submitted.

Tests cover free and provider-charged failure under coverage debt, unknown fallback
cost, above-estimate procurement, duplicate polling, terminal submit rejection,
cancellation, late-debit denial, outbox rollback/retry and racing PostgreSQL workers.

## Release and repair controls

Release only after required CI checks, through the normal immutable-image deploy.
Verify live revision, readiness, new generation outcomes and monetary invariants.
Previously failed records require an explicitly scoped metadata-only repair with
zero net cash movement; never blindly release old reserves or reissue compensations.
Keep historical reconciliation and compensation work separate from this code fix.

Methodology: pinned .agents systematic-debugging, test-driven-development (including
testing-anti-patterns), verification-before-completion. No claim of completed
production deployment is made by this document.
