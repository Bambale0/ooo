# Treasury and working capital

The current model implements product brief §89: historical coverage can become
negative without independently blocking a funded partner. New requests require
retail credit, non-negative economics and current available working capital.
PostgreSQL serializes cash reservations across partners with a transaction lock.

`getBalance` from [Crypto Pay](https://help.send.tg/en/articles/10279948-crypto-pay-api)
provides actual available USDT. `onhold` is already excluded from that field; it is
not subtracted twice. Successful wallet snapshots persist. Safe-to-withdraw can
use the last known balance and explicitly reports its age and `stale` status;
new paid submissions require a fresh snapshot. There is no invented wallet balance.

The accounting cash ceiling is:

`opening capital + confirmed net payment receipts - completed procurement - recorded withdrawals`.

The liquid ceiling is the smaller of this amount and actual available USDT.
Subtract active generation holds, paid-but-uncredited obligations and configured
provider float. Safe-to-withdraw additionally subtracts current future procurement
for positive partner balances, using the worst current cost/retail ratio across
production configurations. A negative result remains negative.

`OPENING_WORKING_CAPITAL_USDT` is the explicitly reconciled opening contribution
from outside the recorded payment history. Do not include recorded receipts a
second time. `REQUIRED_PROVIDER_FLOAT_USDT` is the additional operational buffer.
Neither setting substitutes for a successful wallet API response.

Manual payment credits capture a historical cost coverage ratio and FX snapshot.
Partial refunds reverse that historical coverage proportionally; cumulative
rounding makes a full reversal exact. Later catalog/FX changes do not rewrite the
snapshot. All actual request charges and treasury arithmetic use Decimal.

Admin `POST /api/v1/billing/profit-withdrawals` records an external withdrawal; it
never sends cryptocurrency. Supply `partner_id` for attribution, `amount_usdt`,
`reason` and `idempotency_key`. Exceeding safe-to-withdraw, or an unavailable
calculation, requires `override_reason`. Corrections have a negative amount and
`correction_for_id`; cumulative corrections cannot exceed the original. Records
are append-only. Retail balances are unaffected by this treasury action.

Live Crypto Pay tests require its own testnet credentials, which were not supplied
in this session. Wallet parsing, stale fallback, current reserves, withdrawal
overrides, partial corrections and cross-partner cash contention are isolated tests.

## FX and persistent incidents

The automatic RUB/USDT rate comes from Crypto Pay `getExchangeRates`, only a valid
USDT → RUB quote. It is cached in PostgreSQL for 60 seconds. On API failure the
order is manual fallback, then last automatic rate. `RUB_PER_USDT` supplies the
initial operator-configured manual fallback; an explicit disabled DB setting
supersedes it. Telegram admin → Казначейство → Курс configures or disables fallback.
Automatic data takes precedence as soon as it recovers. Every accepted generation,
manual payment credit and retail price change records rate/source/automatic timestamp.

The Telegram process evaluates treasury/economic incidents once per minute.
Deficit notifications repeat every 15 minutes until manually acknowledged; recovery
alone does not acknowledge the current incident. A subsequent negative transition
starts a new episode and clears its old mute. Negative economics sends one alert
per episode. The PostgreSQL notification outbox is at-least-once: a crash after
Telegram accepted a message but before commit can repeat a notification, never a
financial mutation. Monitor the Telegram process and its logs as part of deployment.

An uncertain Crypto Pay create is `creation_unknown`: refresh only looks for the
already-created invoice. It cannot send another create automatically. If necessary,
admin `POST /api/v1/payments/invoices/{id}/reconcile` takes `provider_invoice_id`
and a reason; the service re-reads Crypto Pay and checks payment payload/amount.
