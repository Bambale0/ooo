# Internal FX minimum and Seedance procurement

Approved on 2026-10-09: internal RUB/USD (RUB/USDT accounting) must use at least
84.50 RUB per unit. This is a minimum, not a fixed rate or a change to the
observed exchange rate.

## Configuration and scope

Set `INTERNAL_MIN_RUB_PER_USDT=84.50` in runtime configuration. The optional
setting is disabled when absent, so deployment and activation can be separate.
It must be a finite positive Decimal.

`app.billing.fx.current_fx` returns `max(persisted_rate, configured_minimum)`.
The existing automatic/manual source and timestamp remain available; the
returned snapshot also carries `observed_rate` and `minimum_rate` as decimal
strings. Pricing reads do not make exchange-rate HTTP requests.

This affects new generation procurement reserves, economic gates, internal
margin calculations, and Seedance 2.5 edit cost-plus quotations. Default retail
tariffs are not changed. `edit` keeps the approved formula
`procurement_USD_per_billed_second * internal_FX + configured_markup`.
Only the final RUB charge is rounded to kopecks, using ROUND_HALF_UP.

Invoice creation continues to obtain and persist its own raw exchange rate via
`refresh_fx_for_invoice`. It does not substitute the internal minimum for the
invoice exchange rate. Existing invoice and generation snapshots keep their
accepted rates, including after configuration changes or retries. Legacy
invoices without an exchange-rate snapshot keep the existing one-time internal
coverage valuation fallback; their requested retail credit is unchanged.

## Procurement review

The reviewed Seedance 2.5 contract now matches the provider's published rates:
480p = 0.0874 USD/s, 720p = 0.196 USD/s, 1080p = 0.483 USD/s.
Source: https://argolink.io/en/models/seedance-2.5 (reviewed 2026-10-09).

The production database is the admission pricing source. Update only the three
existing `seedance-2.5 / default` procurement fields to these reviewed values,
with price history records. Preserve retail prices and every partner retail
snapshot; the edit procurement rows are already current. Do not import or
reprice unrelated models as part of this change.

A 10-billed-second ordinary 720p request has 1.96 USD procurement and retains
its existing partner tariff. With an 84.50 internal rate, its procurement
valuation is 165.62 RUB. A 20-billed-second 720p edit at a 2.50 RUB/s markup costs
381.24 RUB. These are examples, not hardcoded business logic.

## Historical work

Do not rewrite accepted snapshots or rerun retail settlement to correct an old
procurement estimate. A confirmed historical cost discrepancy needs a separate
audited cost-only reconciliation with an idempotency key and coverage ledger
compensation. It must not cause a second partner retail debit. This rollout does
not backfill completed or in-flight generation costs from today's price list.

## Verification and rollout

Run `python -m pytest -q tests/test_internal_fx_floor.py tests/test_seedance_procurement_review.py`
and the full CI suite, including payments, cost coverage, and edit settlement.
After the verified image is deployed, activate the configuration on every
runtime service using the same revision and image. Keep a private environment
backup and a rollback path; never print the full environment.

Verify raw stored FX versus internal FX, ordinary and edit quotations for active
partners, unchanged retail snapshots, public edit prices, and readiness. Do not
create paid provider jobs or invoices just to verify arithmetic. Repeat tariff
updates must not duplicate price history when values already match.

Rolling back the minimum means restoring/removing the configuration variable;
it does not rewrite saved requests, exchange-rate observations, or ledger rows.
