# Individual ArgoLink keys for self-service registration

Every genuinely new self-service partner must receive a distinct, pre-issued **unrestricted prepaid** ArgoLink key.
Neironych does not create provider keys through ArgoLink's private browser-console API.

Normal bot registration claims the next free key automatically; invitations are not part of registration. Legacy invite
rows created by the previous release remain inert for audit compatibility and are never consulted, consumed or displayed.
Existing partners are never backfilled from the pool. A repeated registration for an existing legacy partner without an
individual credential fails closed for administrator review without consuming a key.

Automatic claims also have a global rolling anti-abuse budget. The defaults allow two new claims per 24 hours and can be
changed deliberately with `ARGOLINK_REGISTRATION_CLAIM_BUDGET` and
`ARGOLINK_REGISTRATION_CLAIM_WINDOW_SECONDS`. PostgreSQL serializes the budget check and claim in the same transaction;
budget exhaustion keeps the application pending and notifies the user and administrator without consuming a key.

## Production secret bootstrap

`ARGOLINK_REGISTRATION_KEY_POOL_JSON` is an optional one-time bootstrap secret. Its value is a JSON array:

```text
["<first pre-issued key>","<second pre-issued key>"]
```

Do not commit this value, paste it into logs, or put it in a command line. Store it in the production secret manager.
On the next new registration, keys whose hashes are not already known are validated using the existing non-billable
protected not-found sentinel and the runtime prepaid-capital contract, encrypted with `PROVIDER_CREDENTIALS_MASTER_KEY`,
and inserted into PostgreSQL. Quota/subscription/standard keys without an unrestricted USD/USDT balance are rejected. The environment import
is hash-idempotent. After the administrator screen confirms the expected free count, remove imported plaintext values from
the environment and redeploy; encrypted database rows remain available.

Updating the secret with additional pre-issued keys safely replenishes the pool. Removing values never deletes pool rows.
Duplicate, malformed or invalid values reject the entire missing batch.

## Telegram administration

`Администрирование` → `Ключи ArgoLink для регистраций` shows only counts of free and assigned keys. It never shows a key
or prefix. `Добавить ключи` accepts one key per line. The source Telegram message is deleted best-effort; all keys are
validated before any row is written, encrypted inside the durable confirmation action, and validated again when the
administrator confirms. Mixed valid/invalid and duplicate batches are all-or-nothing.

## Assignment guarantees

- PostgreSQL is the source of truth. A conditional `UPDATE ... RETURNING` claims one available row atomically.
- A transaction-scoped PostgreSQL advisory lock serializes the global rolling claim budget across different Telegram IDs.
- `key_hash`, and the assignment's application ID, are unique. Replaying the same registration returns its existing
  credential instead of consuming another key.
- Assignment, partner activation, encrypted `ProviderCredential`, consents and the internal `nrn_` key commit in the same
  transaction. A rollback returns the pool row and its encrypted secret to `available`.
- After assignment the pool copy of the ciphertext is erased; the partner credential retains the encrypted secret.
- Partners activated through this path are marked as requiring a dedicated ArgoLink credential. Revoking or losing it
  fails closed and never falls back to the shared `ARGOLINK_API_KEY`. Existing partners are unchanged.
- Existing active/disabled partners without an individual credential are never allocated a pool key. Deleted partners
  re-registering under the preserved current Telegram identity reuse their original credential instead of consuming another.
- `ARGOLINK_SHARED_FALLBACK_ENABLED=false` supports a dedicated-only deployment after operators confirm that every live
  legacy partner has an individual credential; while it is true, production still requires `ARGOLINK_API_KEY`.
- An empty or unavailable pool leaves the application pending, creates no partner/internal key, and enqueues one durable,
  deduplicated administrator notification per application. `/start` retries the same application after replenishment.

No validation or test performs a paid generation.
