# Provider-confirmed invoice expiry synchronization

The reconciliation worker previously ignored every invoice except paid results.
This left eight old invoices active despite CryptoPay confirming expiry. The new
path synchronizes only provider-confirmed expired state, under a payment row lock,
with exact provider ID, amount and payload checks. It never infers nonpayment from
a timeout, a missing invoice, or an error. A confirmed payment, paid_waiting_credit,
credited invoice, cancellation or refund is not downgraded by a stale expiry read.

No credit/refund or FX changes are introduced. Expired invoices remain eligible
for paid reconciliation, preserving delayed payment/webhook recovery. Snapshot
metadata records the provider observation and source without overwriting existing
reconciliation data. Repeated identical expiry reads do not write repeatedly.

Errors record allowlisted error codes and upstream HTTP status where available,
never raw provider messages, credentials or invoice URLs. Individual invoice errors
still do not starve the remaining batch.

TDD: two expiry regressions failed on the baseline. After repair, 28 focused tests
passed, covering idempotency, late payment once, wrong ID/payload/amount, concurrent
paid-webhook ordering, unavailable provider and secret-safe error diagnostics.
Normal CI and production verification remain release gates.
