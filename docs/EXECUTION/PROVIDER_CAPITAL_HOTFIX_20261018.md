# Provider-capital admission regression (2026-10-18)

## Cause

PR #124 added all terminal generations' historical `provider_cost_hold_usdt`
to live admission exposure. That value aggregates multiple providers and was
never bounded by age. Old reconciliation risk could therefore block every new
request even with a positive live prepaid balance and no running generations.

## Narrow correction

For terminal generations, admission counts only an unresolved (`unknown`)
ProviderAttempt for the requested provider and created within the existing
`worker_provider_processing_timeout_seconds` window. It uses the attempt amount,
not the generation-wide aggregate. A recent retry/fallback of an older generation
still counts; resolved attempts do not. Live/queued admission reservations, the
shared transaction lock, paid-pending deposits and configured funding floor remain.

No ledger, partner balance, historical hold or cost evidence is cleared. Historical
procurement risk stays visible in `capital_state()` / safe-to-withdraw and in cost
reconciliation. Expiration of an admission reservation does NOT confirm free usage.
The balance adapter and its validated contract are not changed by this hotfix.

Do not group or split supplier accounts based on equal balances or different API
keys. Same-provider admission remains conservatively shared until explicit account
mapping is implemented. That limitation and external HTTP under the admission lock
are not claimed resolved here.

## Verification and release

Eight focused regressions failed on the prior implementation before the repair;
they cover historical holds, provider attribution, settled costs and expiry.
Additional checks cover fresh unknown exposure, still-active reservations, boundary
expiry, real insufficient funds, shared-provider protection, both video creation
routes, replay and zero partner debit on admission. PostgreSQL is tested separately.

Release through PR and green CI. No schema migration or balance adjustment required.
After deployment, verify exact runtime revision/source, financial invariants, and
run real Seedance admission against current configured credentials and prices inside
a transaction that is rolled back before provider dispatch. Health alone is not an
admission check. Never run a paid generation or monetary correction as a side effect
of this diagnostic, and never mutate historical tasks to make the test pass.
