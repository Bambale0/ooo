# Shared upstream key for automatic partner registration

Decision: 2026-10-28. New partners use the second added pool key; existing partners keep their routes.

## Configuration and rollout

1. Apply migration `20261028_0041`.
2. Identify the intended existing pool row using operator records; set
   `ARGOLINK_REGISTRATION_SHARED_POOL_ITEM_ID` to its stable UUID. Do not select
   a moving list index at request time or expose the upstream secret.
3. Restart the API, Telegram bot and workers through the normal release workflow.
4. On the first registration the selected `available` pool row becomes `shared`.
   There is one canonical encrypted credential; every new partner receives an
   explicit `partner_provider_credential_bindings` row in the same transaction
   as activation and its first internal API key. Operators may pre-promote the
   selected row using `provision_shared_registration_key` before opening onboarding.

The provider key is never returned to a partner. The unused pool key stays available.
The setting is opt-in: an unset selector preserves the legacy dedicated-key path.
A configured-but-missing, already assigned, inactive or undecryptable shared key
fails closed: the application stays pending; the bot reports a temporary connection
problem and notifies the administrator. `/start` retries the same application.

## Isolation and compatibility

Partner balances, credit limits, ledger rows, API keys, price snapshots and trial
eligibility are unchanged. The dedicated-pool claim budget does not apply to shared
registrations, because they do not consume additional funded keys. This does not
remove paid-generation balance checks or lifetime Telegram trial limits.

Existing partners are not backfilled. Later dedicated credentials take precedence
for new work. In-flight jobs remain pinned to the original credential, including
polling after rotation. Shared access is explicit per tenant, not global fallback;
an unrelated partner cannot use the credential by guessing its UUID. Deleted
partners retain historical bindings for reconciliation, but lose their API keys.

Migration is additive and does not mutate existing financial or routing records.
Downgrade refuses while shared keys or bindings exist. Prefer forward repair; do
not drop bindings or restore ciphertext while jobs can still use the credential.
Unsetting the selector changes future registrations only, not existing bindings.

## Verification

Regression coverage includes normal Telegram `/start`, more signups than the
legacy claim budget, duplicate `/start`, independent balances and API identities,
explicit tenant isolation, dedicated overrides, revocation, rollback, re-registration,
and concurrent registrations on an isolated PostgreSQL schema.
