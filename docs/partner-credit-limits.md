# Individual partner credit limits

Release plan: add durable limits and an administrator endpoint; cover admission,
settlement, retries and account isolation with tests; validate migration upgrade
and downgrade on isolated PostgreSQL; merge through a green PR; back up production,
apply the additive migration, deploy and verify; configure the requested account.

`partners.credit_limit_rub` is a nonnegative RUB amount, defaulting to zero for
existing and new accounts. Available balance is `balance_rub + credit_limit_rub`.
A limit of 5000 permits new requests while the cash balance is greater than -5000.
At the threshold and below it, admission returns HTTP 402. Both native inference
and queued generations use the same admission check. Other partners retain their
existing zero-credit policy. Provider capital requirements remain independent.

Charges remain actual-only. Admission does not reserve partner funds or cap the
cost of an already accepted request: confirmed charges and concurrent in-flight
requests can take the final balance below the threshold. Later requests are then
blocked. Raising a limit never credits the cash wallet, and lowering it never
rewrites previous charges or cancels accepted work. Account deletion still requires
repayment of negative cash balances. Credit is not cash or withdrawable capital.

Administrators can PUT `/api/v1/billing/partners/{partner_id}/credit-limit` with
`{"credit_limit_rub": "5000.00", "reason": "Approved individual credit limit"}`.
The operation locks the partner and writes old/new limits, actor and reason to
`partner_credit_limit_history` in the same transaction. Repeating the same desired
value has no additional effect. Non-administrators cannot change limits. The
balance API and Telegram balance screen expose cash, credit and available balance.

Apply migration `20261022_0032` before deploying new application code. It does not
change cash balances or ledger rows and remains compatible with the previous
application image. Take and verify a database backup first. A code rollback can
retain the additive schema; old code blocks debt spending. Migration downgrade is
permitted only before any limit history or active limit exists; audited financial
history is retained once used. To disable credit, set its limit to zero through
the same endpoint, rather than deleting history.
