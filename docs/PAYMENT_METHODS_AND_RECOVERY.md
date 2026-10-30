# Payment methods and interrupted invoice creation

Decision confirmed by the owner on 2026-10-30: Lava.top and Crypto Bot must be
available together in the Telegram cabinet; enabling one must not hide the other.

## Explicit selection

When both integrations have credentials, Balance -> Top up presents SBP / Lava.top
and Crypto Bot / USDT / TON. The selected provider is persisted in the SQL dialog
and confirmation payload. It does not change with the global PAYMENT_PROVIDER
setting or a process restart. New explicit confirmations use invoice_create_selected;
legacy confirmations and the existing partner API keep their compatibility path.
No silent provider fallback is allowed. Disabled methods fail without creating an
invoice. Existing actor ownership, confirmation, and invoice idempotency are retained.

The existing Lava.top checkout surcharge applies only to Lava.top. Crypto Bot uses
the existing RUB invoice amount and does not require the Lava buyer email. Balances
are still credited only after independently verified payment, never on invoice creation.

## Interrupted creation

A persisted creating claim without a provider reference is not proof that Lava.top
accepted or rejected the request. Refresh and the background reconciliation worker
move only expired, unpaid, uncredited Lava claims to creation_unknown, record safe
diagnostics and enqueue one deduplicated admin notice. There is no provider POST,
refund, balance change, or inference that an absent invoice-list match is definitive.
Live claims, known provider IDs, paid/credited records and Crypto Pay are untouched.
The partner sees that creation is unconfirmed, rather than an endless creating label.

Polling shutdown drains tracked cabinet updates before aiogram closes its Bot session.
The existing worker_shutdown_grace_seconds setting bounds this drain; the current
host Compose grants a 60-second stop grace. Forced exits are still covered by the
persisted-claim recovery path. No credentials or message content are logged.

## Verification

Regression coverage exercises both methods, fee isolation, email requirements,
provider pinning across restart/default changes, duplicate confirmation, tenant
isolation, expired/live claim distinctions, and bounded graceful shutdown.
