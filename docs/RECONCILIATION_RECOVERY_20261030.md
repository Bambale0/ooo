# Bounded reconciliation and customer finality (2026-10-30)

## Scope and invariants

This release implements the production-log audit requested by the owner. It
preserves the actual-only billing and historical-compensation boundaries in
PRODUCT_BRIEF_INTERVIEW_SNAPSHOT sections 130-131. No catalog prices, partner
balances, historic refunds, provider routing or shared-wallet isolation change.

## Crypto Pay

Scheduled reconciliation stops selecting an **expired, provider-confirmed** invoice
once its expiry is older than PAYMENT_EXPIRED_RECHECK_SECONDS (default 259200).
Unverified expiry, missing expiry timestamps, active/creating/unknown invoices and
paid-waiting-credit remain eligible. A late signed paid webhook, explicit refresh
or audited reconciliation can still credit the invoice exactly once; polling
expiry never means an invoice is non-payable. Provider read errors cannot erase a
confirmed payment. No currency-rate polling is added.

## Lava.top lost-create recovery

Before the one permitted POST, the local payment ID is durably recorded in
reconciliation_snapshot.creation_correlation_id and sent using documented
clientUtm fields: utm_source=neironych, utm_medium=api_topup, utm_content=<local UUID>.
The ordinary reconciliation worker scans GET /api/v1/invoices, explicitly asking
for NEW, IN_PROGRESS, COMPLETED and FAILED, and accepts only a unique exact marker.
It then re-reads GET /api/v1/invoices/{id} and verifies the marker, buyer, amount
and currency before binding or crediting under a database lock. Lookup is bounded
by LAVA_TOP_RECOVERY_MAX_PAGES (default 5, 100 items/page). Incomplete, malformed,
ambiguous or mismatching reads fail closed. No replacement invoice is created.

GET invoice lists do not supply a checkout URL. Recovery can reconcile a known
payment outcome but must not invent a payment URL. Legacy records without the
marker require explicit provider-contract evidence and the existing admin
reconciliation endpoint. A diagnostic snapshot is not a prior reconciliation:
its content is retained when recording provider_invoice_id, actor and reason.
A contract bound to another local invoice is rejected, even for the same buyer
and amount. A stale unpaid response cannot downgrade paid_waiting_credit.

## Unknown video submissions

The worker closes an actual-only video request without any known provider task ID
once the generation, all attempts and the latest durably recorded POST start exceed the configured
WORKER_PROVIDER_PROCESSING_TIMEOUT_SECONDS (default 1800). The customer outcome is
failed/not_chargeable, the terminal outbox event is idempotent, and any customer
reserve is released by the existing ledger-aware method. Supplier attempts remain
reconciliation_required with explicit unknown cost liabilities. No provider POST,
assumed free usage or historic money rewrite occurs. A late submit reply can add
the recovered task ID for cost review but cannot reopen customer charging.

Known-ID videos retain the existing late-result recovery contract. Pre-actual-only
history is not automatically refunded or silently marked free. Its cost review
requires supplier evidence, including the original credential/account scope.

## Admission diagnostics

provider_capital_balance_unavailable records only safe error class and HTTP status.
provider_capital_insufficient records Decimal capital components and required funds.
Public responses remain neutral; credentials, URLs, buyer emails and raw provider
messages are never logged. The capital admission lock and fail-closed rules remain.

## Release verification and rollback

Run focused audit regressions, existing payment/video/referral suites, PostgreSQL
concurrency tests and the repository CI/security/migration checks. Check public
health/readiness, the exact deployed revision, fresh worker logs, payment outcomes,
provider cost review and duplicate debits after rollout. Use the established release
rollback workflow; this change has no schema migration. Rollback restores code but
must not reopen finalized customer requests or remove appended ledger/audit history.

Provider contract references: https://gate.lava.top/docs/documentation.yaml and
https://help.send.tg/en/articles/10279948-crypto-pay-api .

## Review hardening

Each provider POST stores provider_submission_started_at before network I/O. An old
retry row therefore cannot cause premature finalization of a fresh submission.
Legacy rows without this marker retain the conservative existing timestamp checks.
Only definitively failed/cancelled historical task IDs stop blocking unknown fallback
finalization; accepted, processing, provisional timeout, usage review and completed
IDs remain protected. Supplier-cost uncertainty stays separate from this eligibility.
