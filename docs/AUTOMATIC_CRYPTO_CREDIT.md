# Automatic RUB credit after Crypto Pay confirmation

## Requirement and baseline

2026-09-30: the owner explicitly changed the previous manual-credit policy. A partner's confirmed crypto payment must automatically credit the original requested RUB invoice amount. This supersedes the older manual-only brief/checklist. It is not an arbitrary balance top-up or a provider-wallet transfer.

Baseline: main 06352569cb07d398f5f349dadb819eddc5022656. Signed webhooks re-fetch Crypto Pay status; partner payment checks and admin reconciliation share apply_paid_provider_invoice. That function currently stops at paid_waiting_credit. credit_paid_invoice already provides locked, idempotent retail/coverage ledger entries and immutable FX/coverage snapshots. Telegram notifications are durable but partner credit notification currently lives only in the admin action. Production has Crypto Pay configured; three active and one already credited invoice, no recorded webhook events at audit time.

## Implementation and acceptance

1. Demonstrate the old behavior with a failing automatic-credit regression.
2. Credit only after authoritative paid status and amount/payload/invoice checks; use the existing atomic ledger operation. Preserve refunds and duplicate confirmation behavior.
3. Move credit notifications into the shared transaction, deduplicated per payment. Keep manual credit as an idempotent recovery route.
4. Add bounded background reconciliation of known uncredited invoices so missed webhooks do not require a manual button. Run independently of generation dispatch and isolate each payment transaction; do not create or pay invoices.
5. Cover duplicate/new update IDs, mismatched/unpaid confirmation, rollback/retry, late paid invoices, notification deduplication, background recovery and tenant boundaries. Run relevant and full checks; request independent review before merge.
6. Update policy/docs, merge through a green PR, build and deploy the exact verified revision. Check runtime and preserved configuration without fabricating production payments.

No schema migration is planned. User prices, accepted assets and invoice amounts remain unchanged. Real upstream balance checks for generation and treasury calculations remain active. Automatic credit cannot invent real crypto or fund ArgoLink.

## Evidence

- RED: previous manual-only flow failed the automatic-credit assertion (`paid_waiting_credit` instead of `credited`). Recovery tests also failed before implementation.
- GREEN: full suite on isolated PostgreSQL 16: **644 passed**. Two additional refund-status replay cases and removal of redundant Telegram notification then verified in focused payment/Telegram suite: **30 passed**.
- Ruff and `git diff --check` pass. No schema changes.
- Independent review (`review_automatic_crypto_credit`): no Critical/Important issues; stale manual-snapshot wording corrected.
- Includes USDT/TON, amount/payload/provider-id mismatch, unpaid state, invalid signature, duplicate/new webhook IDs, concurrent webhook/recovery, rollback after retail write, late payment, unknown creation response, fair recovery pagination and refund replay.
- Release requires green PR/main CI before exact-revision production rollout; no fabricated production payments.
