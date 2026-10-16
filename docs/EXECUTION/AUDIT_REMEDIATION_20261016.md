# Audit remediation: customer finality and financial invariants

## Scope of this change

Separate customer outcome from provider cost reconciliation. Final free failures
cannot be reopened or charged by late settlement; PostgreSQL enforces the terminal
billing disposition. Preserve a validated result when usage is unavailable, with
an acceptance-time reconciliation deadline and permanent waiver after expiry.

Recover JSON/error/complete SSE responses using encrypted, bounded database storage.
The response, financial settlement and terminal webhook outbox share one transaction.
A settlement savepoint prevents partial charges after an accounting exception.
Repeated requests never resubmit a provider job, including after retention expiry.
The native deadline worker handles a crashed/disconnected request process; it does
not automatically reinterpret unmarked historical jobs.

Operator endpoints (admin authentication required):

- `GET /api/v1/billing/generations/{id}/compensations`: debits, returns, linked
  manual credits, remaining amount, allocation evidence.
- `POST` on the same endpoint: `idempotency_key`, `reason`, optional `amount_rub`,
  optional `source_ledger_entry_id`. An existing source must be a positive unlinked
  manual credit of the same partner. Linking does not move money. Omitting a source
  returns only the remaining charge. The authenticated shared principal is recorded
  as `admin_api`, not an invented human identity.

Financial history is not repriced or deleted. Linked credits reduce a later legacy
reserve release. Provider cost is settled to the actual existing coverage-ledger
net, so cost-first/usage-second reconciliation cannot credit coverage twice.
Metrics use cached wallet data and have no financial HTTP side effects.

## Verification evidence before release

New regressions were observed failing before the corresponding changes. Local
isolated tests cover terminal 408/5xx, late settlement, incomplete streams, exact
response replay, post-ledger exception rollback, body deadlines, crash recovery,
response expiry, usage waiver, strict financial idempotency, partial refunds,
existing manual-credit attribution, tenant isolation and allocation exhaustion.

Real PostgreSQL checks cover ten simultaneous settlement requests, ten simultaneous
refund requests, direct attempts to reopen a free failure, atomic outbox creation
and response recovery from a fresh session. Fresh migrations and ORM schema checks
were exercised on PostgreSQL 16.

An exported repeatable-read snapshot of actual production was restored to an
isolated internal Docker network and migrated. Original row fingerprints matched
for partners, customer and coverage ledgers, generations, attempts, invoices,
provider credentials and API keys. Balance/ledger mismatch count was zero.
Restored provider credentials decrypted correctly using the EXISTING production
runtime key. Detailed production counts/fingerprints remain in the private host
audit report, not in this public repository. The isolated copy was removed.

This proves logical restore of that snapshot, NOT independent off-site recovery,
PITR, an independently recovered encryption key, or production deployment of this PR.

## Operational limits and remaining audit work

Production `archive_mode=off` and no `.backup.env` were observed. Independent
storage/recovery-key configuration and a WAL/PITR restore drill remain required.
Supplier account mapping must be explicit: do not infer a wallet per credential,
or merge wallets based only on equal balances. Account-scoped balance snapshots,
short admission transactions, distributed provider limits, partner risk controls,
scoped circuit breakers and mixed ten-partner load acceptance are separate changes.
Do not interpret this PR as completion of those unverified operational controls.

## Release gates

Run Ruff and the complete test suite, plus the PostgreSQL integration tests with
`TEST_POSTGRES_DATABASE_URL` pointing ONLY to a disposable database. Run Alembic
upgrade/check against both an empty instance and an isolated restored snapshot.
Never run fault injection against production or use live provider calls in tests.

Apply through a reviewed PR and green required checks. No direct main pushes.
New financial columns have additive migrations. Downgrades refuse to remove durable
finality or retained compensation history; an old application image must not be
rolled back blindly because it does not enforce the new customer contract.

## Final candidate checks (2026-10-16)

The release review reproduced two additional defects before repair: malformed
nested token usage raised AttributeError and lost an otherwise valid response;
unexpired usage reviews could exhaust the recovery batch and starve expired
requests. Regressions failed first, then passed after explicit nested usage
validation and due-only SQL selection before LIMIT. The latter also passed on
PostgreSQL, not only SQLite. Full isolated suite: 1391 passed, 22 skipped; skipped
native FFmpeg and AF_UNIX checks still require their dedicated CI/runtime gates.
Ruff passed on the same code tree. No live provider HTTP was used by these tests.

First migration rollout must use the verified release image and an independently
validated pre-migration snapshot. The existing-host deployer deliberately refuses
to switch before Alembic is at the candidate head; it does not run migrations.
Pause new inference admission for the first schema/application cutover. Resume
only after exact-revision readiness and contract smoke pass. Preserve the schema
and ledger on failure; after new finality outcomes exist, roll forward rather
than manually restoring the pre-finality application. No historical compensation
or price recalculation is included in rollout.
